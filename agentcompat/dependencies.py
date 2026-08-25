"""Trusted, bounded dependency snapshots derived from repository manifests."""

import json
import re
import shlex
import stat
import tomllib
from collections.abc import Iterator, Sequence
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Literal

from agentcompat.dependency_lockfiles import lockfile_format, parse_lockfile
from agentcompat.dependency_pyproject import parse_pyproject_dependencies
from agentcompat.dependency_types import (
    DependencyRecord,
    DependencySnapshot,
    LockfileFingerprint,
    lockfile_fingerprint,
    parse_pep508_dependency,
    source_fingerprint,
)
from agentcompat.dependency_types import DependencyScanError as DependencyScanError
from agentcompat.dependency_types import (
    canonicalize_dependency_name as canonicalize_dependency_name,
)

type ManifestRole = Literal["requirement", "constraint"]
type RequirementPolicy = tuple[Literal["source", "qualifier"], str, str | None]

_INLINE_COMMENT = re.compile(r"\s+#.*$")
_HASH_OPTION = re.compile(
    r"(?<!\S)--hash(?:=|\s+)([A-Za-z0-9][A-Za-z0-9_-]*:[A-Fa-f0-9]+)(?=\s|$)"
)
_LEGACY_REQUIREMENT_URL = re.compile(r"^[A-Za-z][A-Za-z0-9+.-]*://")
_MAX_MANIFEST_BYTES = 5 * 1024 * 1024
_MAX_LOCKFILE_BYTES = 25 * 1024 * 1024
_MAX_LOCKFILES = 20
_MAX_LOCKFILES_TOTAL_BYTES = 50 * 1024 * 1024
_MAX_REQUIREMENTS_FILES = 100
_MAX_REQUIREMENTS_TOTAL_BYTES = 25 * 1024 * 1024
_MAX_REQUIREMENT_LINES = 100_000
_MAX_REQUIREMENT_POLICY_EVENTS = 100_000
_MAX_REQUIREMENT_CACHED_POLICY_EVENTS = 500_000
_MAX_DEPENDENCY_NAMES = 50_000
_MAX_DEPENDENCY_RECORDS = 100_000
_IGNORED_REQUIREMENT_FLAG_OPTIONS = {
    "--no-index",
    "--pre",
    "--prefer-binary",
    "--require-hashes",
}
_IGNORED_REQUIREMENT_VALUE_OPTIONS = {
    "--extra-index-url",
    "--find-links",
    "--index-url",
    "--no-binary",
    "--only-binary",
    "--trusted-host",
    "--use-feature",
    "-f",
    "-i",
}


@dataclass(slots=True)
class _RequirementsScanState:
    records: set[DependencyRecord]
    visited: set[tuple[Path, bool]] = field(default_factory=set)
    scanned_paths: set[Path] = field(default_factory=set)
    active_paths: set[Path] = field(default_factory=set)
    documents: dict[Path, str] = field(default_factory=dict)
    traces: dict[tuple[Path, bool], tuple[RequirementPolicy, ...]] = field(
        default_factory=dict
    )
    total_bytes: int = 0
    logical_lines: int = 0
    cached_policy_events: int = 0


def _safe_manifest_path(workspace: Path, path: Path) -> Path:
    try:
        resolved_parent = path.parent.resolve()
    except (OSError, RuntimeError) as exc:
        raise DependencyScanError(
            f"could not resolve dependency manifest path {path}: {exc}"
        ) from exc
    resolved_path = resolved_parent / path.name
    if not resolved_path.is_relative_to(workspace):
        raise DependencyScanError(f"dependency manifest escapes workspace: {path}")
    return resolved_path


def _read_manifest(
    workspace: Path,
    path: Path,
    *,
    required: bool,
    max_bytes: int = _MAX_MANIFEST_BYTES,
) -> tuple[Path, str, bytes] | None:
    manifest = _safe_manifest_path(workspace, path)
    try:
        manifest_stat = manifest.lstat()
    except FileNotFoundError as exc:
        if required:
            raise DependencyScanError(
                f"dependency manifest does not exist: {path}"
            ) from exc
        return None
    except OSError as exc:
        raise DependencyScanError(f"could not inspect {manifest.name}: {exc}") from exc

    if not stat.S_ISREG(manifest_stat.st_mode):
        raise DependencyScanError(f"{manifest.name} must be a regular file")
    if manifest_stat.st_size > max_bytes:
        raise DependencyScanError(
            f"{manifest.name} exceeds the {max_bytes}-byte scan limit"
        )
    try:
        raw_content = manifest.read_bytes()
    except OSError as exc:
        raise DependencyScanError(f"could not read {manifest.name}: {exc}") from exc
    if len(raw_content) > max_bytes:
        raise DependencyScanError(
            f"{manifest.name} exceeds the {max_bytes}-byte scan limit"
        )
    try:
        text = raw_content.decode("utf-8")
    except UnicodeError as exc:
        raise DependencyScanError(f"invalid UTF-8 in {manifest.name}: {exc}") from exc
    return manifest, text, raw_content


def _add_record(records: set[DependencyRecord], record: DependencyRecord) -> None:
    if record in records:
        return
    if len(records) >= _MAX_DEPENDENCY_RECORDS:
        raise DependencyScanError(
            f"dependency scan exceeds {_MAX_DEPENDENCY_RECORDS} records"
        )
    records.add(record)


def _add_records(
    records: set[DependencyRecord],
    additions: Sequence[DependencyRecord] | set[DependencyRecord],
) -> None:
    for record in additions:
        _add_record(records, record)


def _scan_pyproject(workspace: Path, records: set[DependencyRecord]) -> None:
    manifest = _read_manifest(
        workspace,
        workspace / "pyproject.toml",
        required=False,
    )
    if manifest is None:
        return
    path, text, _ = manifest
    try:
        document = tomllib.loads(text)
    except (tomllib.TOMLDecodeError, RecursionError) as exc:
        raise DependencyScanError(f"invalid {path.name}: {exc}") from exc
    _add_records(records, parse_pyproject_dependencies(document))


def _logical_requirement_lines(text: str, location: str) -> Iterator[tuple[int, str]]:
    buffer = ""
    start_line = 0
    for line_number, raw_line in enumerate(text.splitlines(), start=1):
        stripped_line = raw_line.rstrip()
        if stripped_line.lstrip().startswith("#"):
            if buffer:
                yield start_line, f"{buffer}{stripped_line}"
                buffer = ""
            else:
                yield line_number, stripped_line
            continue
        if not buffer:
            start_line = line_number
        if stripped_line.endswith("\\"):
            buffer += f"{stripped_line[:-1]} "
            continue
        yield start_line, f"{buffer}{stripped_line}"
        buffer = ""
    if buffer:
        raise DependencyScanError(
            f"{location}:{start_line} has a dangling continuation"
        )


def _directive_target(
    tokens: list[str],
    short_option: str,
    long_option: str,
    location: str,
) -> str | None:
    first = tokens[0]
    if first in {short_option, long_option}:
        if len(tokens) != 2 or not tokens[1]:
            raise DependencyScanError(f"{location} requires exactly one path")
        return tokens[1]
    long_prefix = f"{long_option}="
    if first.startswith(long_prefix):
        target = first.removeprefix(long_prefix)
        if len(tokens) != 1 or not target:
            raise DependencyScanError(f"{location} requires exactly one path")
        return target
    if first.startswith(short_option) and first != short_option:
        target = first.removeprefix(short_option)
        if len(tokens) != 1 or not target:
            raise DependencyScanError(f"{location} requires exactly one path")
        return target
    return None


def _requirement_record(
    requirement: str,
    *,
    origin: str,
    constraint: bool,
) -> DependencyRecord:
    try:
        record = parse_pep508_dependency(
            requirement,
            origin=origin,
            kind="constraint" if constraint else "declared",
        )
    except DependencyScanError:
        if "#egg=" not in requirement or not _LEGACY_REQUIREMENT_URL.match(requirement):
            raise
        egg_name = requirement.split("#egg=", maxsplit=1)[1].split("&", maxsplit=1)[0]
        if constraint:
            raise
        record = parse_pep508_dependency(egg_name, origin=origin)
        record = replace(
            record,
            source=source_fingerprint("legacy-url", requirement),
        )
    return record


def _editable_record(
    tokens: list[str],
    *,
    origin: str,
    location: str,
    constraint: bool,
) -> tuple[bool, DependencyRecord | None]:
    target = _directive_target(tokens, "-e", "--editable", location)
    if target is None:
        return False, None
    if constraint:
        raise DependencyScanError(f"{location} constraints cannot be editable")
    if target in {".", "./"}:
        return True, None
    try:
        return True, _requirement_record(target, origin=origin, constraint=False)
    except DependencyScanError as exc:
        raise DependencyScanError(
            f"{location} contains an unsupported editable requirement"
        ) from exc


def _is_ignored_requirement_option(
    tokens: list[str],
    location: str,
) -> tuple[bool, RequirementPolicy | None]:
    policy: RequirementPolicy | None
    first = tokens[0]
    if first in _IGNORED_REQUIREMENT_FLAG_OPTIONS:
        if len(tokens) != 1:
            raise DependencyScanError(
                f"{location} option {first!r} does not accept a value"
            )
        policy_kind: Literal["source", "qualifier"] = (
            "source" if first == "--no-index" else "qualifier"
        )
        policy = (policy_kind, first, None)
        return True, policy
    option = first.split("=", maxsplit=1)[0]
    if option not in _IGNORED_REQUIREMENT_VALUE_OPTIONS:
        return False, None
    if first == option:
        valid = len(tokens) == 2 and bool(tokens[1])
    else:
        valid = (
            option.startswith("--")
            and len(tokens) == 1
            and bool(first.removeprefix(f"{option}="))
        )
    if not valid:
        raise DependencyScanError(
            f"{location} option {option!r} requires exactly one value"
        )
    value = tokens[1] if first == option else first.removeprefix(f"{option}=")
    canonical_option = {
        "-f": "--find-links",
        "-i": "--index-url",
    }.get(option, option)
    source_options = {
        "--extra-index-url",
        "--find-links",
        "--index-url",
        "--trusted-host",
        "-f",
        "-i",
    }
    policy = (
        "source" if option in source_options else "qualifier",
        canonical_option,
        value,
    )
    return True, policy


def _scan_requirement_line(
    line: str,
    *,
    origin: str,
    location: str,
    constraint: bool,
) -> tuple[
    DependencyRecord | None,
    str | None,
    bool,
    RequirementPolicy | None,
]:
    uncommented = _INLINE_COMMENT.sub("", line).strip()
    if not uncommented or uncommented.startswith("#"):
        return None, None, False, None
    try:
        tokens = shlex.split(uncommented, comments=False, posix=True)
    except ValueError as exc:
        raise DependencyScanError(f"invalid requirement at {location}: {exc}") from exc
    if not tokens:
        return None, None, False, None
    include_target = _directive_target(tokens, "-r", "--requirement", location)
    if include_target is not None:
        return None, include_target, False, None
    constraint_target = _directive_target(tokens, "-c", "--constraint", location)
    if constraint_target is not None:
        return None, constraint_target, True, None
    editable_found, editable = _editable_record(
        tokens,
        origin=origin,
        location=location,
        constraint=constraint,
    )
    if editable_found:
        return editable, None, False, None
    ignored, source_policy = _is_ignored_requirement_option(tokens, location)
    if ignored:
        return None, None, False, source_policy
    if tokens[0].startswith("-"):
        raise DependencyScanError(
            f"{location} contains unsupported option: {tokens[0]!r}"
        )
    hashes = tuple(
        sorted(match.group(1).lower() for match in _HASH_OPTION.finditer(uncommented))
    )
    requirement = _HASH_OPTION.sub("", uncommented).strip()
    if any(
        token == "--hash" or token.startswith("--hash=")
        for token in shlex.split(requirement)
    ):
        raise DependencyScanError(f"{location} contains an invalid hash option")
    record = _requirement_record(
        requirement,
        origin=origin,
        constraint=constraint,
    )
    if hashes:
        record = replace(
            record,
            qualifiers=source_fingerprint(
                "pip-requirement-qualifiers",
                json.dumps(
                    {"qualifiers": record.qualifiers, "hashes": hashes},
                    sort_keys=True,
                    separators=(",", ":"),
                ),
            ),
        )
    return (
        record,
        None,
        False,
        None,
    )


def _scan_requirements_file(
    workspace: Path,
    path: Path,
    state: _RequirementsScanState,
    *,
    constraint: bool,
) -> tuple[RequirementPolicy, ...]:
    resolved_path = _safe_manifest_path(workspace, path)
    if resolved_path in state.active_paths:
        raise DependencyScanError(
            f"requirements file recursively references itself: {resolved_path}"
        )
    scan_state = (resolved_path, constraint)
    if scan_state in state.visited:
        return state.traces[scan_state]
    if resolved_path not in state.scanned_paths:
        if len(state.scanned_paths) >= _MAX_REQUIREMENTS_FILES:
            raise DependencyScanError(
                f"requirements scan exceeds {_MAX_REQUIREMENTS_FILES} files"
            )
        state.scanned_paths.add(resolved_path)
    state.visited.add(scan_state)

    text = state.documents.get(resolved_path)
    if text is None:
        manifest = _read_manifest(workspace, resolved_path, required=True)
        if manifest is None:  # pragma: no cover - required never returns None
            return ()
        _, text, raw_content = manifest
        total_bytes = state.total_bytes + len(raw_content)
        if total_bytes > _MAX_REQUIREMENTS_TOTAL_BYTES:
            raise DependencyScanError(
                "requirements scan exceeds the "
                f"{_MAX_REQUIREMENTS_TOTAL_BYTES}-byte total limit"
            )
        state.total_bytes = total_bytes
        state.documents[resolved_path] = text
    state.active_paths.add(resolved_path)

    relative_path = resolved_path.relative_to(workspace).as_posix()
    origin = f"{relative_path}:{'constraint' if constraint else 'requirement'}"
    trace: list[RequirementPolicy] = []
    try:
        for line_number, line in _logical_requirement_lines(text, relative_path):
            state.logical_lines += 1
            if state.logical_lines > _MAX_REQUIREMENT_LINES:
                raise DependencyScanError(
                    f"requirements scan exceeds {_MAX_REQUIREMENT_LINES} lines"
                )
            record, include_target, include_constraint, source_policy = (
                _scan_requirement_line(
                    line,
                    origin=origin,
                    location=f"{relative_path}:{line_number}",
                    constraint=constraint,
                )
            )
            if record is not None:
                _add_record(state.records, record)
            if source_policy is not None:
                if len(trace) >= _MAX_REQUIREMENT_POLICY_EVENTS:
                    raise DependencyScanError(
                        "requirements resolver-policy trace exceeds "
                        f"{_MAX_REQUIREMENT_POLICY_EVENTS} events"
                    )
                trace.append(source_policy)
            if include_target is not None:
                included_trace = _scan_requirements_file(
                    workspace,
                    resolved_path.parent / include_target,
                    state,
                    constraint=include_constraint,
                )
                if len(trace) + len(included_trace) > _MAX_REQUIREMENT_POLICY_EVENTS:
                    raise DependencyScanError(
                        "requirements resolver-policy trace exceeds "
                        f"{_MAX_REQUIREMENT_POLICY_EVENTS} events"
                    )
                trace.extend(included_trace)
    finally:
        state.active_paths.remove(resolved_path)
    state.cached_policy_events += len(trace)
    if state.cached_policy_events > _MAX_REQUIREMENT_CACHED_POLICY_EVENTS:
        raise DependencyScanError(
            "requirements cached resolver-policy traces exceed "
            f"{_MAX_REQUIREMENT_CACHED_POLICY_EVENTS} events"
        )
    state.traces[scan_state] = tuple(trace)
    return state.traces[scan_state]


def _requirement_manifests(
    workspace: Path,
    manifest_roles: Sequence[tuple[str, ManifestRole]],
) -> list[tuple[Path, bool]]:
    manifests: dict[Path, bool] = {}
    for manifest in workspace.glob("requirements*.txt"):
        if len(manifests) >= _MAX_REQUIREMENTS_FILES:
            raise DependencyScanError(
                f"requirements scan exceeds {_MAX_REQUIREMENTS_FILES} files"
            )
        manifests[manifest] = False
    explicit_roles: dict[Path, bool] = {}
    for path_value, role in manifest_roles:
        path = _safe_manifest_path(workspace, workspace / path_value)
        constraint = role == "constraint"
        previous = explicit_roles.setdefault(path, constraint)
        if previous != constraint:
            raise DependencyScanError(
                f"dependency manifest {path_value!r} has conflicting roles"
            )
        manifests[path] = constraint
    return sorted(manifests.items(), key=lambda item: item[0].as_posix())


def _scan_requirements(
    workspace: Path,
    records: set[DependencyRecord],
    manifest_roles: Sequence[tuple[str, ManifestRole]],
) -> None:
    state = _RequirementsScanState(records=records)
    for manifest, constraint in _requirement_manifests(workspace, manifest_roles):
        trace = _scan_requirements_file(
            workspace,
            manifest,
            state,
            constraint=constraint,
        )
        relative_path = manifest.relative_to(workspace).as_posix()
        source_options = [
            (option, value)
            for policy_kind, option, value in trace
            if policy_kind == "source"
        ]
        qualifier_options = [
            (option, value)
            for policy_kind, option, value in trace
            if policy_kind == "qualifier"
        ]
        if source_options:
            _add_record(
                records,
                DependencyRecord(
                    name="pip-resolver-policy",
                    version=None,
                    source=source_fingerprint(
                        "pip-resolver-policy",
                        json.dumps(source_options, separators=(",", ":")),
                    ),
                    kind="policy",
                    origin=f"{relative_path}:resolver-policy",
                ),
            )
        if qualifier_options:
            _add_record(
                records,
                DependencyRecord(
                    name="pip-resolver-policy",
                    version=None,
                    source=None,
                    kind="policy",
                    origin=f"{relative_path}:resolver-policy",
                    qualifiers=source_fingerprint(
                        "pip-resolver-qualifiers",
                        json.dumps(qualifier_options, separators=(",", ":")),
                    ),
                ),
            )


def _automatic_lockfiles(workspace: Path) -> list[Path]:
    conventional_paths = [
        workspace / "poetry.lock",
        workspace / "pdm.lock",
        workspace / "Pipfile.lock",
        workspace / "uv.lock",
        workspace / "pylock.toml",
    ]
    paths: list[Path] = []
    for path in conventional_paths:
        try:
            path.lstat()
        except FileNotFoundError:
            continue
        except OSError as exc:
            raise DependencyScanError(
                f"could not inspect lockfile {path.name}: {exc}"
            ) from exc
        paths.append(path)
    for path in workspace.glob("pylock.*.toml"):
        if lockfile_format(path.name) is None:
            continue
        if len(paths) >= _MAX_LOCKFILES:
            raise DependencyScanError(f"lockfile scan exceeds {_MAX_LOCKFILES} files")
        paths.append(path)
    return paths


def _lockfile_paths(
    workspace: Path,
    configured_lockfiles: Sequence[str],
) -> list[tuple[Path, bool]]:
    paths: dict[Path, bool] = {}
    for path in _automatic_lockfiles(workspace):
        paths[_safe_manifest_path(workspace, path)] = False
    for path_value in configured_lockfiles:
        path = _safe_manifest_path(workspace, workspace / path_value)
        paths[path] = True
    if len(paths) > _MAX_LOCKFILES:
        raise DependencyScanError(f"lockfile scan exceeds {_MAX_LOCKFILES} files")
    return sorted(paths.items(), key=lambda item: item[0].as_posix())


def _scan_lockfiles(
    workspace: Path,
    records: set[DependencyRecord],
    *,
    configured_lockfiles: Sequence[str],
    parse_semantics: bool,
    require_sources: bool,
) -> tuple[LockfileFingerprint, ...]:
    fingerprints: list[LockfileFingerprint] = []
    total_bytes = 0
    for path, configured in _lockfile_paths(workspace, configured_lockfiles):
        manifest = _read_manifest(
            workspace,
            path,
            required=configured,
            max_bytes=_MAX_LOCKFILE_BYTES,
        )
        if manifest is None:
            continue
        resolved_path, text, raw_content = manifest
        total_bytes += len(raw_content)
        if total_bytes > _MAX_LOCKFILES_TOTAL_BYTES:
            raise DependencyScanError(
                "lockfile scan exceeds the "
                f"{_MAX_LOCKFILES_TOTAL_BYTES}-byte total limit"
            )
        relative_path = resolved_path.relative_to(workspace).as_posix()
        fingerprints.append(lockfile_fingerprint(relative_path, raw_content))
        if parse_semantics:
            if lockfile_format(relative_path) is None and configured:
                raise DependencyScanError(
                    f"unsupported semantic lockfile: {relative_path}"
                )
            _add_records(
                records,
                parse_lockfile(
                    path=relative_path,
                    text=text,
                    require_sources=require_sources,
                ),
            )
    return tuple(sorted(fingerprints, key=lambda item: item.path))


def scan_python_dependency_snapshot(
    workspace: Path,
    *,
    manifest_roles: Sequence[tuple[str, ManifestRole]] = (),
    lockfiles: Sequence[str] = (),
    include_lockfiles: bool = True,
    parse_lockfile_semantics: bool = True,
    require_lock_sources: bool = False,
) -> DependencySnapshot:
    """Return a typed deterministic dependency snapshot for one workspace."""
    try:
        resolved_workspace = workspace.resolve()
    except (OSError, RuntimeError) as exc:
        raise DependencyScanError(f"could not resolve workspace: {exc}") from exc
    records: set[DependencyRecord] = set()
    _scan_pyproject(resolved_workspace, records)
    _scan_requirements(resolved_workspace, records, manifest_roles)
    lockfile_fingerprints: tuple[LockfileFingerprint, ...] = ()
    if include_lockfiles:
        lockfile_fingerprints = _scan_lockfiles(
            resolved_workspace,
            records,
            configured_lockfiles=lockfiles,
            parse_semantics=parse_lockfile_semantics,
            require_sources=require_lock_sources,
        )
    names = {record.name for record in records}
    if len(names) > _MAX_DEPENDENCY_NAMES:
        raise DependencyScanError(
            f"dependency scan exceeds {_MAX_DEPENDENCY_NAMES} distinct names"
        )
    return DependencySnapshot(
        records=frozenset(records),
        lockfiles=lockfile_fingerprints,
    )


def scan_python_dependencies(
    workspace: Path,
    *,
    manifest_roles: Sequence[tuple[str, ManifestRole]] = (),
) -> frozenset[str]:
    """Return direct canonical names for backward-compatible policy checks."""
    return scan_python_dependency_snapshot(
        workspace,
        manifest_roles=manifest_roles,
        include_lockfiles=False,
    ).direct_names
