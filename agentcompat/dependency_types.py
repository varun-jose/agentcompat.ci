"""Typed dependency observations and deterministic snapshot comparison."""

import hashlib
import json
from dataclasses import dataclass
from typing import Literal

from packaging.requirements import InvalidRequirement, Requirement
from packaging.utils import InvalidName, canonicalize_name

type DependencyKind = Literal["declared", "constraint", "locked", "policy"]


class DependencyScanError(ValueError):
    """Raised when dependency state cannot be observed deterministically."""


@dataclass(frozen=True, slots=True)
class DependencyRecord:
    """One normalized declaration or resolved lockfile package."""

    name: str
    version: str | None
    source: str | None
    kind: DependencyKind
    origin: str
    qualifiers: str | None = None
    context: str | None = None


@dataclass(frozen=True, slots=True)
class LockfileFingerprint:
    """Content identity for one supported lockfile."""

    path: str
    sha256: str


@dataclass(frozen=True, slots=True)
class DependencySnapshot:
    """Normalized dependency and lockfile state for one workspace."""

    records: frozenset[DependencyRecord]
    lockfiles: tuple[LockfileFingerprint, ...] = ()

    @property
    def names(self) -> frozenset[str]:
        """Return every canonical dependency name in the snapshot."""
        return frozenset(
            record.name for record in self.records if record.kind != "policy"
        )

    @property
    def direct_names(self) -> frozenset[str]:
        """Return install-root declarations, excluding constraints and locks."""
        return frozenset(
            record.name for record in self.records if record.kind == "declared"
        )

    @property
    def locked_names(self) -> frozenset[str]:
        """Return resolved names observed in supported lockfiles."""
        return frozenset(
            record.name for record in self.records if record.kind == "locked"
        )


@dataclass(frozen=True, slots=True)
class DependencyValueChange:
    """Before/after values for one dependency property."""

    name: str
    kind: DependencyKind
    origin: str
    before: tuple[str, ...]
    after: tuple[str, ...]

    def evidence(self) -> str:
        """Format a bounded, deterministic evaluator evidence line."""

        def render(values: tuple[str, ...]) -> str:
            if not values:
                return "<none>"
            rendered = ", ".join(value[:160] for value in values[:5])
            return f"{rendered}, …" if len(values) > 5 else rendered

        evidence = (
            f"{self.name[:160]} [{self.kind}:{self.origin[:160]}]: "
            f"{render(self.before)} -> {render(self.after)}"
        )
        return f"{evidence[:499]}…" if len(evidence) > 500 else evidence


@dataclass(frozen=True, slots=True)
class LockfileChange:
    """Addition, removal, or content change for one lockfile."""

    path: str
    change: Literal["added", "removed", "modified"]

    def evidence(self) -> str:
        """Format a deterministic evaluator evidence line."""
        return f"{self.path}: {self.change}"


@dataclass(frozen=True, slots=True)
class DependencyDelta:
    """All deterministic differences between two dependency snapshots."""

    added: tuple[str, ...]
    removed: tuple[str, ...]
    version_changes: tuple[DependencyValueChange, ...]
    source_changes: tuple[DependencyValueChange, ...]
    lockfile_changes: tuple[LockfileChange, ...]
    declaration_changes: tuple["DependencyDeclarationChange", ...]


@dataclass(frozen=True, slots=True)
class DependencyDeclarationChange:
    """Addition or removal of one complete resolver input declaration."""

    name: str
    kind: Literal["declared", "constraint", "policy"]
    origin: str
    change: Literal["added", "removed"]

    def evidence(self) -> str:
        """Format a deterministic declaration-change evidence line."""
        return f"{self.name} [{self.kind}:{self.origin}]: {self.change}"


def canonicalize_dependency_name(name: str) -> str:
    """Normalize and validate a Python distribution name."""
    try:
        return str(canonicalize_name(name, validate=True))
    except InvalidName as exc:
        raise DependencyScanError(f"invalid dependency name: {name!r}") from exc


def source_fingerprint(source_type: str, value: str) -> str:
    """Create a non-secret stable identity for a dependency source."""
    digest = hashlib.sha256(value.strip().encode("utf-8")).hexdigest()
    return f"{source_type}:sha256:{digest}"


def parse_pep508_dependency(
    requirement: str,
    *,
    origin: str,
    kind: DependencyKind = "declared",
) -> DependencyRecord:
    """Fully validate and normalize one PEP 508 requirement."""
    try:
        parsed = Requirement(requirement)
    except (InvalidRequirement, RecursionError) as exc:
        raise DependencyScanError(
            f"{origin} contains an invalid PEP 508 requirement"
        ) from exc

    if kind == "constraint" and (parsed.extras or parsed.url is not None):
        raise DependencyScanError(
            f"{origin} constraints cannot specify extras or direct URLs"
        )

    specifiers = tuple(sorted(str(specifier) for specifier in parsed.specifier))
    version = ",".join(specifiers) or None
    source = (
        source_fingerprint("direct-url", parsed.url) if parsed.url is not None else None
    )
    qualifier_data = {
        "extras": sorted(str(canonicalize_name(extra)) for extra in parsed.extras),
        "marker": str(parsed.marker) if parsed.marker is not None else None,
    }
    context = (
        source_fingerprint(
            "pep508-context",
            json.dumps(qualifier_data, sort_keys=True, separators=(",", ":")),
        )
        if qualifier_data["extras"] or qualifier_data["marker"] is not None
        else None
    )
    return DependencyRecord(
        name=canonicalize_dependency_name(parsed.name),
        version=version,
        source=source,
        kind=kind,
        origin=origin,
        context=context,
    )


def lockfile_fingerprint(path: str, content: bytes) -> LockfileFingerprint:
    """Hash exact lockfile bytes without exposing their content."""
    return LockfileFingerprint(
        path=path,
        sha256=hashlib.sha256(content).hexdigest(),
    )


def _values_by_name(
    snapshot: DependencySnapshot,
    attribute: Literal["version", "source"],
) -> dict[tuple[str, DependencyKind, str], tuple[str, ...]]:
    values: dict[tuple[str, DependencyKind, str], set[str]] = {}
    for record in snapshot.records:
        if record.kind == "policy" and (
            attribute == "version" or record.source is None
        ):
            continue
        identity_origin = (
            record.origin
            if record.kind in {"locked", "policy"}
            else (f"{record.origin}|context:{record.context or '<default>'}")
        )
        if record.kind == "locked" and ":pipfile:" in identity_origin:
            identity_origin = (
                f"{identity_origin.split(':pipfile:', maxsplit=1)[0]}:pipfile"
            )
        identity = (record.name, record.kind, identity_origin)
        identity_values = values.setdefault(identity, set())
        value = getattr(record, attribute)
        if value is None:
            value = "<unspecified>"
        identity_values.add(value)
    return {identity: tuple(sorted(items)) for identity, items in values.items()}


def _value_changes(
    before: DependencySnapshot,
    after: DependencySnapshot,
    attribute: Literal["version", "source"],
) -> tuple[DependencyValueChange, ...]:
    before_values = _values_by_name(before, attribute)
    after_values = _values_by_name(after, attribute)
    identities = before_values.keys() | after_values.keys()
    whole_name_changes = before.direct_names ^ after.direct_names
    changes: list[DependencyValueChange] = []
    for identity in sorted(identities):
        before_items = before_values.get(identity, ())
        after_items = after_values.get(identity, ())
        if identity[1] == "declared" and identity[0] in whole_name_changes:
            continue
        if before_items == after_items:
            continue
        if attribute == "source" and not (
            (set(before_items) | set(after_items)) - {"<unspecified>"}
        ):
            continue
        changes.append(
            DependencyValueChange(
                name=identity[0],
                kind=identity[1],
                origin=identity[2],
                before=before_items,
                after=after_items,
            )
        )
    return tuple(changes)


def _declaration_changes(
    before: DependencySnapshot,
    after: DependencySnapshot,
) -> tuple[DependencyDeclarationChange, ...]:
    input_kinds = {"declared", "constraint", "policy"}
    before_records = {record for record in before.records if record.kind in input_kinds}
    after_records = {record for record in after.records if record.kind in input_kinds}
    changes: list[DependencyDeclarationChange] = []
    for record in before_records - after_records:
        if record.kind == "locked":  # pragma: no cover - filtered above
            continue
        changes.append(
            DependencyDeclarationChange(
                name=record.name,
                kind=record.kind,
                origin=record.origin,
                change="removed",
            )
        )
    for record in after_records - before_records:
        if record.kind == "locked":  # pragma: no cover - filtered above
            continue
        changes.append(
            DependencyDeclarationChange(
                name=record.name,
                kind=record.kind,
                origin=record.origin,
                change="added",
            )
        )
    return tuple(
        sorted(
            changes,
            key=lambda item: (item.name, item.kind, item.origin, item.change),
        )
    )


def _lockfile_changes(
    before: DependencySnapshot,
    after: DependencySnapshot,
) -> tuple[LockfileChange, ...]:
    before_hashes = {lockfile.path: lockfile.sha256 for lockfile in before.lockfiles}
    after_hashes = {lockfile.path: lockfile.sha256 for lockfile in after.lockfiles}
    changes: list[LockfileChange] = []
    for path in sorted(before_hashes.keys() | after_hashes.keys()):
        if path not in before_hashes:
            change: Literal["added", "removed", "modified"] = "added"
        elif path not in after_hashes:
            change = "removed"
        elif before_hashes[path] != after_hashes[path]:
            change = "modified"
        else:
            continue
        changes.append(LockfileChange(path=path, change=change))
    return tuple(changes)


def compare_dependency_snapshots(
    before: DependencySnapshot,
    after: DependencySnapshot,
) -> DependencyDelta:
    """Compare names, versions, sources, and lockfile bytes."""
    return DependencyDelta(
        added=tuple(sorted(after.direct_names - before.direct_names)),
        removed=tuple(sorted(before.direct_names - after.direct_names)),
        version_changes=_value_changes(before, after, "version"),
        source_changes=_value_changes(before, after, "source"),
        lockfile_changes=_lockfile_changes(before, after),
        declaration_changes=_declaration_changes(before, after),
    )
