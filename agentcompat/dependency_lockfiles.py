"""Strict parsers for supported Python lockfile formats."""

import json
import re
import tomllib
from collections.abc import Callable
from urllib.parse import urlsplit

from packaging.specifiers import InvalidSpecifier, SpecifierSet
from packaging.version import InvalidVersion, Version

from agentcompat.dependency_types import (
    DependencyRecord,
    DependencyScanError,
    canonicalize_dependency_name,
    source_fingerprint,
)

type LockfileParser = Callable[[str, str, bool], set[DependencyRecord]]
_MAX_ARRAY_ITEMS = 100_000
_MAX_LOCK_RECORDS = 100_000


def _table(value: object, location: str) -> dict[str, object]:
    if not isinstance(value, dict) or not all(isinstance(key, str) for key in value):
        raise DependencyScanError(f"{location} must be a table")
    return value


def _table_or_empty(value: object, location: str) -> dict[str, object]:
    return {} if value is None else _table(value, location)


def _array(value: object, location: str) -> list[object]:
    if not isinstance(value, list):
        raise DependencyScanError(f"{location} must be an array")
    if len(value) > _MAX_ARRAY_ITEMS:
        raise DependencyScanError(f"{location} exceeds {_MAX_ARRAY_ITEMS} items")
    return value


def _string(value: object, location: str) -> str:
    if not isinstance(value, str) or not value:
        raise DependencyScanError(f"{location} must be a non-empty string")
    return value


def _optional_string(value: object, location: str) -> str | None:
    if value is None:
        return None
    return _string(value, location)


def _toml_document(text: str, path: str) -> dict[str, object]:
    try:
        return tomllib.loads(text)
    except (tomllib.TOMLDecodeError, RecursionError) as exc:
        raise DependencyScanError(f"invalid {path}: {exc}") from exc


def _source_from_data(source_type: str, data: dict[str, object]) -> str:
    return source_fingerprint(
        source_type,
        json.dumps(data, sort_keys=True, separators=(",", ":")),
    )


def _locked_record(
    *,
    name: object,
    version: object,
    source: str | None,
    origin: str,
    version_required: bool = True,
) -> DependencyRecord:
    normalized_name = canonicalize_dependency_name(_string(name, f"{origin}.name"))
    normalized_version = _optional_string(version, f"{origin}.version")
    if version_required and normalized_version is None:
        raise DependencyScanError(f"{origin}.version is required")
    if normalized_version is not None:
        try:
            normalized_version = str(Version(normalized_version))
        except InvalidVersion as exc:
            raise DependencyScanError(f"{origin}.version is invalid") from exc
    return DependencyRecord(
        name=normalized_name,
        version=normalized_version,
        source=source,
        kind="locked",
        origin=origin,
    )


def _add_lock_record(
    records: set[DependencyRecord],
    record: DependencyRecord,
) -> None:
    if record in records:
        return
    if len(records) >= _MAX_LOCK_RECORDS:
        raise DependencyScanError(f"lockfile scan exceeds {_MAX_LOCK_RECORDS} records")
    records.add(record)


def _parse_poetry_lock(
    text: str,
    path: str,
    require_sources: bool,
) -> set[DependencyRecord]:
    del require_sources
    document = _toml_document(text, path)
    metadata = _table(document.get("metadata"), f"{path}:metadata")
    lock_version = _string(
        metadata.get("lock-version"),
        f"{path}:metadata.lock-version",
    )
    if lock_version not in {"1.1", "2.0", "2.1"}:
        raise DependencyScanError(f"unsupported {path} lock version {lock_version!r}")
    packages = _array(document.get("package"), f"{path}:package")
    records: set[DependencyRecord] = set()
    for index, raw_package in enumerate(packages):
        location = f"{path}:package.{index}"
        package = _table(raw_package, location)
        develop = package.get("develop", False)
        if not isinstance(develop, bool):
            raise DependencyScanError(f"{location}.develop must be a boolean")
        raw_source = package.get("source")
        if raw_source is None:
            source = _source_from_data(
                "poetry-registry",
                {"registry": "pypi", "develop": develop},
            )
        else:
            source_table = _table(raw_source, f"{location}.source")
            source_type = _string(
                source_table.get("type"),
                f"{location}.source.type",
            )
            if source_type not in {"legacy", "git", "url", "file", "directory"}:
                raise DependencyScanError(
                    f"{location}.source has unsupported type {source_type!r}"
                )
            url = _string(
                source_table.get("url"),
                f"{location}.source.url",
            )
            source_data: dict[str, object] = {
                "type": source_type,
                "url": url,
                "develop": develop,
            }
            for key in ("reference", "resolved_reference", "subdirectory"):
                if key in source_table:
                    source_data[key] = _string(
                        source_table[key],
                        f"{location}.source.{key}",
                    )
            if source_type == "git" and "resolved_reference" not in source_data:
                raise DependencyScanError(
                    f"{location}.source.resolved_reference is required for git"
                )
            source = _source_from_data(f"poetry-{source_type}", source_data)
        _add_lock_record(
            records,
            _locked_record(
                name=package.get("name"),
                version=package.get("version"),
                source=source,
                origin=f"{path}:poetry",
            ),
        )
    return records


def _pdm_version(value: object, path: str) -> tuple[int, int, int]:
    raw_version = _string(value, f"{path}:metadata.lock_version")
    if (
        re.fullmatch(r"(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)", raw_version)
        is None
    ):
        raise DependencyScanError(f"invalid {path} lock version {raw_version!r}")
    major, minor, patch = (int(part) for part in raw_version.split("."))
    if major != 4 or (major, minor, patch) > (4, 5, 1):
        raise DependencyScanError(f"unsupported {path} lock version {raw_version!r}")
    return major, minor, patch


def _pdm_registry_source(
    package: dict[str, object],
    location: str,
    *,
    static_urls: bool,
    require_sources: bool,
) -> str | None:
    if not require_sources:
        return None
    if not static_urls:
        raise DependencyScanError(
            f"{location} registry source is incomplete without static_urls"
        )
    urls: list[str] = []
    for index, raw_file in enumerate(_array(package.get("files"), f"{location}.files")):
        file = _table(raw_file, f"{location}.files.{index}")
        urls.append(_string(file.get("url"), f"{location}.files.{index}.url"))
    if not urls:
        raise DependencyScanError(f"{location} has no static registry URLs")
    return _source_from_data("pdm-registry", {"urls": sorted(urls)})


def _parse_pdm_lock(
    text: str,
    path: str,
    require_sources: bool,
) -> set[DependencyRecord]:
    document = _toml_document(text, path)
    metadata = _table(document.get("metadata"), f"{path}:metadata")
    _pdm_version(metadata.get("lock_version"), path)
    raw_strategy = metadata.get("strategy", [])
    strategy = _array(raw_strategy, f"{path}:metadata.strategy")
    if not all(isinstance(item, str) for item in strategy):
        raise DependencyScanError(f"{path}:metadata.strategy must contain strings")
    static_urls = "static_urls" in strategy

    records: set[DependencyRecord] = set()
    for index, raw_package in enumerate(
        _array(document.get("package"), f"{path}:package")
    ):
        location = f"{path}:package.{index}"
        package = _table(raw_package, location)
        vcs_keys = [key for key in ("git", "hg", "svn", "bzr") if key in package]
        direct_keys = [key for key in ("url", "path") if key in package]
        source_keys = [*vcs_keys, *direct_keys]
        if len(source_keys) > 1:
            raise DependencyScanError(f"{location} has multiple dependency sources")
        source: str | None
        if source_keys:
            source_key = source_keys[0]
            source_data: dict[str, object] = {
                source_key: _string(package[source_key], f"{location}.{source_key}")
            }
            if source_key in {"git", "hg", "svn", "bzr"}:
                source_data["ref"] = _string(
                    package.get("ref"),
                    f"{location}.ref",
                )
                if require_sources and "revision" not in package:
                    raise DependencyScanError(
                        f"{location}.revision is required for immutable VCS provenance"
                    )
            for key in ("revision", "subdirectory", "editable"):
                if key in package:
                    value = package[key]
                    if key == "editable":
                        if not isinstance(value, bool):
                            raise DependencyScanError(
                                f"{location}.editable must be a boolean"
                            )
                    else:
                        value = _string(value, f"{location}.{key}")
                    source_data[key] = value
            source = _source_from_data(f"pdm-{source_key}", source_data)
            version_required = False
        else:
            if "editable" in package:
                raise DependencyScanError(
                    f"{location}.editable requires a VCS or path source"
                )
            source = _pdm_registry_source(
                package,
                location,
                static_urls=static_urls,
                require_sources=require_sources,
            )
            version_required = True
        _add_lock_record(
            records,
            _locked_record(
                name=package.get("name"),
                version=package.get("version"),
                source=source,
                origin=f"{path}:pdm",
                version_required=version_required,
            ),
        )
    return records


def _strict_json_document(text: str, path: str) -> dict[str, object]:
    def reject_duplicate_pairs(pairs: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, value in pairs:
            if key in result:
                raise DependencyScanError(f"{path} contains duplicate key {key!r}")
            result[key] = value
        return result

    def reject_constant(value: str) -> object:
        raise DependencyScanError(f"{path} contains non-finite number {value}")

    try:
        document = json.loads(
            text,
            object_pairs_hook=reject_duplicate_pairs,
            parse_constant=reject_constant,
        )
    except (json.JSONDecodeError, RecursionError) as exc:
        raise DependencyScanError(f"invalid {path}: {exc}") from exc
    return _table(document, path)


def _pipfile_sources(meta: dict[str, object], path: str) -> tuple[dict[str, str], str]:
    sources: dict[str, str] = {}
    order: list[str] = []
    for index, raw_source in enumerate(
        _array(meta.get("sources"), f"{path}:_meta.sources")
    ):
        location = f"{path}:_meta.sources.{index}"
        source = _table(raw_source, location)
        name = canonicalize_dependency_name(
            _string(source.get("name"), f"{location}.name")
        )
        if name in sources:
            raise DependencyScanError(f"{path} duplicates source {name!r}")
        verify_ssl = source.get("verify_ssl", True)
        if not isinstance(verify_ssl, bool):
            raise DependencyScanError(f"{location}.verify_ssl must be a boolean")
        sources[name] = source_fingerprint(
            "pipfile-index",
            json.dumps(
                {
                    "name": name,
                    "url": _string(source.get("url"), f"{location}.url"),
                    "verify_ssl": verify_ssl,
                },
                sort_keys=True,
                separators=(",", ":"),
            ),
        )
        order.append(name)
    if not order:
        raise DependencyScanError(f"{path}:_meta.sources cannot be empty")
    return sources, order[0]


def _parse_pipfile_lock(
    text: str,
    path: str,
    require_sources: bool,
) -> set[DependencyRecord]:
    del require_sources
    document = _strict_json_document(text, path)
    meta = _table(document.get("_meta"), f"{path}:_meta")
    if meta.get("pipfile-spec") != 6:
        raise DependencyScanError(f"unsupported {path} Pipfile specification")
    sources, default_source = _pipfile_sources(meta, path)
    records: set[DependencyRecord] = set()
    for category, raw_entries in document.items():
        if category == "_meta":
            continue
        entries = _table(raw_entries, f"{path}:{category}")
        for raw_name, raw_entry in entries.items():
            location = f"{path}:{category}.{raw_name}"
            entry = _table(raw_entry, location)
            vcs_keys = [key for key in ("git", "hg", "svn", "bzr") if key in entry]
            direct_keys = [key for key in ("file", "path") if key in entry]
            source_keys = [*vcs_keys, *direct_keys]
            if len(source_keys) > 1:
                raise DependencyScanError(f"{location} has multiple sources")
            if source_keys:
                source_key = source_keys[0]
                if "index" in entry:
                    raise DependencyScanError(
                        f"{location}.index cannot accompany a direct source"
                    )
                source_data: dict[str, object] = {
                    source_key: _string(entry[source_key], f"{location}.{source_key}")
                }
                if source_key in {"git", "hg", "svn", "bzr"}:
                    source_data["ref"] = _string(
                        entry.get("ref"),
                        f"{location}.ref",
                    )
                elif "ref" in entry or "subdirectory" in entry:
                    raise DependencyScanError(f"{location} uses VCS-only source fields")
                if "subdirectory" in entry:
                    source_data["subdirectory"] = _string(
                        entry["subdirectory"],
                        f"{location}.subdirectory",
                    )
                if "editable" in entry:
                    editable = entry["editable"]
                    if not isinstance(editable, bool):
                        raise DependencyScanError(
                            f"{location}.editable must be a boolean"
                        )
                    source_data["editable"] = editable
                source = _source_from_data(f"pipfile-{source_key}", source_data)
                version = entry.get("version")
                if version is not None:
                    version = _string(version, f"{location}.version")
                version_required = False
            else:
                if any(key in entry for key in ("editable", "ref", "subdirectory")):
                    raise DependencyScanError(
                        f"{location} has source fields without a direct source"
                    )
                raw_version = _string(entry.get("version"), f"{location}.version")
                if not raw_version.startswith("=="):
                    raise DependencyScanError(
                        f"{location}.version must be an exact == pin"
                    )
                version = raw_version.removeprefix("==")
                if not version or any(character in version for character in "*,<>=!~"):
                    raise DependencyScanError(
                        f"{location}.version must be one concrete == version"
                    )
                index_name = canonicalize_dependency_name(
                    _optional_string(entry.get("index"), f"{location}.index")
                    or default_source
                )
                try:
                    source_data = {"index": sources[index_name]}
                except KeyError as exc:
                    raise DependencyScanError(
                        f"{location} references unknown index {index_name!r}"
                    ) from exc
                hashes = entry.get("hashes", [])
                if not isinstance(hashes, list) or not all(
                    isinstance(item, str) and item for item in hashes
                ):
                    raise DependencyScanError(
                        f"{location}.hashes must be a list of non-empty strings"
                    )
                source_data["hashes"] = sorted(hashes)
                source = _source_from_data("pipfile-registry", source_data)
                version_required = True
            _add_lock_record(
                records,
                _locked_record(
                    name=raw_name,
                    version=version,
                    source=source,
                    origin=f"{path}:pipfile:{category}",
                    version_required=version_required,
                ),
            )
    return records


def _pylock_locators(value: dict[str, object], location: str) -> dict[str, str]:
    locators: dict[str, str] = {
        key: _string(value[key], f"{location}.{key}")
        for key in ("url", "path")
        if key in value
    }
    if not locators:
        raise DependencyScanError(f"{location} requires a url or path")
    return locators


def _pylock_hashes(value: object, location: str) -> dict[str, str]:
    hashes = _table(value, location)
    if not hashes or not all(
        isinstance(algorithm, str) and algorithm and isinstance(digest, str) and digest
        for algorithm, digest in hashes.items()
    ):
        raise DependencyScanError(f"{location} must contain non-empty hashes")
    return {algorithm: str(digest) for algorithm, digest in hashes.items()}


def _pylock_artifact(value: object, location: str) -> dict[str, object]:
    artifact = _table(value, location)
    normalized: dict[str, object] = {
        **_pylock_locators(artifact, location),
        "hashes": _pylock_hashes(artifact.get("hashes"), f"{location}.hashes"),
    }
    if "size" in artifact:
        size = artifact["size"]
        if not isinstance(size, int) or isinstance(size, bool) or size < 0:
            raise DependencyScanError(f"{location}.size must be a non-negative integer")
        normalized["size"] = size
    for key in ("name", "subdirectory"):
        if key in artifact:
            normalized[key] = _string(artifact[key], f"{location}.{key}")
    return normalized


def _pylock_source(
    package: dict[str, object],
    location: str,
    *,
    require_sources: bool,
) -> str | None:
    direct_keys = [key for key in ("vcs", "directory", "archive") if key in package]
    if len(direct_keys) > 1:
        raise DependencyScanError(f"{location} has multiple dependency sources")

    raw_wheels = _array(package.get("wheels", []), f"{location}.wheels")
    has_artifacts = package.get("sdist") is not None or bool(raw_wheels)
    raw_index = package.get("index")
    if raw_index is not None and (not isinstance(raw_index, str) or not raw_index):
        raise DependencyScanError(f"{location}.index must be a non-empty string")
    if direct_keys and (has_artifacts or raw_index is not None):
        raise DependencyScanError(
            f"{location} combines a direct source with distribution artifacts"
        )

    if direct_keys:
        source_key = direct_keys[0]
        source_table = _table(package[source_key], f"{location}.{source_key}")
        normalized: dict[str, object]
        if source_key == "vcs":
            normalized = {
                "type": _string(source_table.get("type"), f"{location}.vcs.type"),
                **_pylock_locators(source_table, f"{location}.vcs"),
                "commit-id": _string(
                    source_table.get("commit-id"),
                    f"{location}.vcs.commit-id",
                ),
            }
            if "requested-revision" in source_table:
                normalized["requested-revision"] = _string(
                    source_table["requested-revision"],
                    f"{location}.vcs.requested-revision",
                )
            if "subdirectory" in source_table:
                normalized["subdirectory"] = _string(
                    source_table["subdirectory"],
                    f"{location}.vcs.subdirectory",
                )
        elif source_key == "directory":
            normalized = {
                "path": _string(
                    source_table.get("path"),
                    f"{location}.directory.path",
                )
            }
            if "editable" in source_table:
                editable = source_table["editable"]
                if not isinstance(editable, bool):
                    raise DependencyScanError(
                        f"{location}.directory.editable must be a boolean"
                    )
                normalized["editable"] = editable
            if "subdirectory" in source_table:
                normalized["subdirectory"] = _string(
                    source_table["subdirectory"],
                    f"{location}.directory.subdirectory",
                )
        else:
            normalized = _pylock_artifact(source_table, f"{location}.archive")
        return _source_from_data(f"pylock-{source_key}", normalized)

    if has_artifacts:
        source_data: dict[str, object] = {}
        if raw_index is not None:
            source_data["index"] = raw_index
        if package.get("sdist") is not None:
            source_data["sdist"] = _pylock_artifact(
                package["sdist"],
                f"{location}.sdist",
            )
        wheels = [
            _pylock_artifact(wheel, f"{location}.wheels.{index}")
            for index, wheel in enumerate(raw_wheels)
        ]
        source_data["wheels"] = sorted(
            wheels,
            key=lambda wheel: json.dumps(
                wheel,
                sort_keys=True,
                separators=(",", ":"),
            ),
        )
        return _source_from_data("pylock-artifacts", source_data)
    if raw_index is not None:
        raise DependencyScanError(
            f"{location}.index requires an sdist or wheel artifact"
        )
    if require_sources:
        raise DependencyScanError(f"{location} has incomplete source provenance")
    return None


def _parse_pylock(
    text: str,
    path: str,
    require_sources: bool,
) -> set[DependencyRecord]:
    document = _toml_document(text, path)
    if document.get("lock-version") != "1.0":
        raise DependencyScanError(f"unsupported {path} lock version")
    _string(document.get("created-by"), f"{path}:created-by")
    packages = _array(document.get("packages"), f"{path}:packages")
    records: set[DependencyRecord] = set()
    for index, raw_package in enumerate(packages):
        location = f"{path}:packages.{index}"
        package = _table(raw_package, location)
        raw_name = _string(package.get("name"), f"{location}.name")
        normalized_name = canonicalize_dependency_name(raw_name)
        if raw_name != normalized_name:
            raise DependencyScanError(f"{location}.name must be normalized")
        _add_lock_record(
            records,
            _locked_record(
                name=raw_name,
                version=package.get("version"),
                source=_pylock_source(
                    package,
                    location,
                    require_sources=require_sources,
                ),
                origin=f"{path}:pylock",
                version_required=False,
            ),
        )
    return records


def _parse_uv_lock(
    text: str,
    path: str,
    require_sources: bool,
) -> set[DependencyRecord]:
    del require_sources
    document = _toml_document(text, path)
    version = document.get("version")
    if not isinstance(version, int) or isinstance(version, bool) or version != 1:
        raise DependencyScanError(f"unsupported {path} lock version")
    requires_python = _string(
        document.get("requires-python"),
        f"{path}:requires-python",
    )
    try:
        SpecifierSet(requires_python)
    except InvalidSpecifier as exc:
        raise DependencyScanError(f"{path}:requires-python is invalid") from exc
    revision = document.get("revision", 0)
    if (
        not isinstance(revision, int)
        or isinstance(revision, bool)
        or not 0 <= revision <= 3
    ):
        raise DependencyScanError(f"unsupported {path} revision {revision!r}")
    records: set[DependencyRecord] = set()
    for index, raw_package in enumerate(
        _array(document.get("package", []), f"{path}:package")
    ):
        location = f"{path}:package.{index}"
        package = _table(raw_package, location)
        source_table = _table(package.get("source"), f"{location}.source")
        source_keys = [
            key
            for key in (
                "registry",
                "git",
                "url",
                "path",
                "directory",
                "editable",
                "virtual",
            )
            if key in source_table
        ]
        if len(source_keys) != 1:
            raise DependencyScanError(
                f"{location}.source must contain one supported source"
            )
        source_key = source_keys[0]
        allowed_keys = (
            {source_key, "subdirectory"} if source_key == "url" else {source_key}
        )
        if set(source_table) - allowed_keys:
            raise DependencyScanError(
                f"{location}.source contains fields invalid for {source_key}"
            )
        source_value = _string(
            source_table[source_key], f"{location}.source.{source_key}"
        )
        source_data: dict[str, object] = {source_key: source_value}
        if source_key in {"registry", "url"} and not urlsplit(source_value).scheme:
            raise DependencyScanError(
                f"{location}.source.{source_key} must be an absolute URL"
            )
        if source_key == "url" and "subdirectory" in source_table:
            source_data["subdirectory"] = _string(
                source_table["subdirectory"],
                f"{location}.source.subdirectory",
            )
        if source_key == "git":
            parsed_git = urlsplit(source_value)
            if (
                not parsed_git.scheme
                or re.fullmatch(r"[0-9a-fA-F]{7,64}", parsed_git.fragment) is None
            ):
                raise DependencyScanError(
                    f"{location}.source.git must contain an immutable SHA fragment"
                )
        _add_lock_record(
            records,
            _locked_record(
                name=package.get("name"),
                version=package.get("version"),
                source=_source_from_data(f"uv-{source_key}", source_data),
                origin=f"{path}:uv",
                version_required=source_key == "registry",
            ),
        )
    return records


def lockfile_format(path: str) -> str | None:
    """Identify a supported lockfile by repository-relative filename."""
    name = path.rsplit("/", maxsplit=1)[-1]
    if name == "poetry.lock":
        return "poetry"
    if name == "pdm.lock":
        return "pdm"
    if name == "Pipfile.lock":
        return "pipfile"
    if name == "uv.lock":
        return "uv"
    if name == "pylock.toml" or re.fullmatch(r"pylock\.[^.]+\.toml", name):
        return "pylock"
    return None


_LOCKFILE_PARSERS: dict[str, LockfileParser] = {
    "pdm": _parse_pdm_lock,
    "pipfile": _parse_pipfile_lock,
    "poetry": _parse_poetry_lock,
    "pylock": _parse_pylock,
    "uv": _parse_uv_lock,
}


def parse_lockfile(
    *,
    path: str,
    text: str,
    require_sources: bool,
) -> set[DependencyRecord]:
    """Parse one supported semantic lockfile."""
    format_name = lockfile_format(path)
    if format_name is None:
        raise DependencyScanError(f"unsupported semantic lockfile: {path}")
    return _LOCKFILE_PARSERS[format_name](text, path, require_sources)
