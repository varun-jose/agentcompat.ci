"""Pure parsers for Python dependency declarations in ``pyproject.toml``."""

import json
import re
from collections.abc import Iterable

from packaging.specifiers import InvalidSpecifier, SpecifierSet

from agentcompat.dependency_types import (
    DependencyRecord,
    DependencyScanError,
    canonicalize_dependency_name,
    parse_pep508_dependency,
    source_fingerprint,
)

_MAX_GROUPS = 1_000
_MAX_GROUP_DEPTH = 100
_MAX_GROUP_ITEMS = 50_000
_POETRY_DEPENDENCY_FAMILIES: tuple[tuple[set[str], set[str]], ...] = (
    (
        {"version"},
        {
            "version",
            "python",
            "platform",
            "markers",
            "allow-prereleases",
            "allows-prereleases",
            "optional",
            "extras",
            "source",
        },
    ),
    (
        {"git"},
        {
            "git",
            "branch",
            "tag",
            "rev",
            "subdirectory",
            "python",
            "platform",
            "markers",
            "allow-prereleases",
            "allows-prereleases",
            "optional",
            "extras",
            "develop",
        },
    ),
    (
        {"file"},
        {"file", "subdirectory", "python", "platform", "markers", "optional", "extras"},
    ),
    (
        {"path"},
        {
            "path",
            "subdirectory",
            "python",
            "platform",
            "markers",
            "optional",
            "extras",
            "develop",
        },
    ),
    (
        {"url"},
        {"url", "subdirectory", "python", "platform", "markers", "optional", "extras"},
    ),
    (
        set(),
        {
            "python",
            "platform",
            "markers",
            "allow-prereleases",
            "source",
            "develop",
        },
    ),
)
_POETRY_STRING_KEYS = {
    "branch",
    "file",
    "git",
    "markers",
    "path",
    "platform",
    "python",
    "rev",
    "source",
    "subdirectory",
    "tag",
    "url",
    "version",
}
_POETRY_BOOL_KEYS = {
    "allow-prereleases",
    "allows-prereleases",
    "develop",
    "optional",
}
_POETRY_CONTEXT_KEYS = {
    "allow-prereleases",
    "allows-prereleases",
    "develop",
    "extras",
    "markers",
    "optional",
    "platform",
    "python",
}
_POETRY_CONSTRAINT_COMMAS = re.compile(r"\s*,\s*")
_POETRY_CONSTRAINT_SYNTAX = re.compile(r"[0-9A-Za-z.*+!<>=^~|,_ -]+")
_MAX_POETRY_CONSTRAINT_CHARS = 4_096


def _table(value: object, location: str) -> dict[str, object]:
    if value is None:
        return {}
    if not isinstance(value, dict) or not all(isinstance(key, str) for key in value):
        raise DependencyScanError(f"{location} must be a TOML table")
    return value


def _string_list(value: object, location: str) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise DependencyScanError(f"{location} must be a list of strings")
    if len(value) > _MAX_GROUP_ITEMS:
        raise DependencyScanError(f"{location} exceeds {_MAX_GROUP_ITEMS} items")
    return value


def _pep508_records(
    requirements: Iterable[str],
    origin: str,
) -> set[DependencyRecord]:
    return {
        parse_pep508_dependency(requirement, origin=origin)
        for requirement in requirements
    }


def _dependency_group_data(
    groups: dict[str, object],
) -> tuple[
    dict[str, str],
    dict[str, list[str]],
    dict[str, list[tuple[str, str]]],
    set[DependencyRecord],
]:
    if len(groups) > _MAX_GROUPS:
        raise DependencyScanError(f"dependency-groups exceeds {_MAX_GROUPS} groups")
    names: dict[str, str] = {}
    edges: dict[str, list[str]] = {}
    sequences: dict[str, list[tuple[str, str]]] = {}
    records: set[DependencyRecord] = set()
    item_count = 0
    for raw_name, raw_items in groups.items():
        normalized_name = canonicalize_dependency_name(raw_name)
        previous = names.setdefault(normalized_name, raw_name)
        if previous != raw_name:
            raise DependencyScanError(
                f"dependency-groups has duplicate normalized group {normalized_name!r}"
            )
        if not isinstance(raw_items, list):
            raise DependencyScanError(f"dependency-groups.{raw_name} must be a list")
        edges[normalized_name] = []
        sequences[normalized_name] = []
        origin = f"pyproject.toml:dependency-groups.{raw_name}"
        for item in raw_items:
            item_count += 1
            if item_count > _MAX_GROUP_ITEMS:
                raise DependencyScanError(
                    f"dependency-groups exceed {_MAX_GROUP_ITEMS} items"
                )
            if isinstance(item, str):
                record = parse_pep508_dependency(item, origin=origin)
                records.add(record)
                sequences[normalized_name].append(
                    (
                        "requirement",
                        json.dumps(
                            {
                                "name": record.name,
                                "version": record.version,
                                "source": record.source,
                                "qualifiers": record.qualifiers,
                                "context": record.context,
                            },
                            sort_keys=True,
                            separators=(",", ":"),
                        ),
                    )
                )
                continue
            if (
                isinstance(item, dict)
                and set(item) == {"include-group"}
                and isinstance(item["include-group"], str)
            ):
                included = canonicalize_dependency_name(item["include-group"])
                edges[normalized_name].append(included)
                sequences[normalized_name].append(("include", included))
                continue
            raise DependencyScanError(
                f"dependency-groups.{raw_name} contains an invalid item"
            )
    return names, edges, sequences, records


def _poetry_sources(
    poetry: dict[str, object],
) -> tuple[dict[str, str], DependencyRecord | None]:
    raw_sources = poetry.get("source")
    if raw_sources is None:
        return {}, None
    if not isinstance(raw_sources, list):
        raise DependencyScanError("tool.poetry.source must be an array of tables")
    sources: dict[str, str] = {}
    policy_data: list[dict[str, object]] = []
    for index, raw_source in enumerate(raw_sources):
        location = f"tool.poetry.source.{index}"
        source = _table(raw_source, location)
        unknown_keys = set(source) - {
            "indexed",
            "links",
            "name",
            "priority",
            "url",
        }
        if unknown_keys:
            raise DependencyScanError(
                f"{location} contains unsupported keys: {sorted(unknown_keys)}"
            )
        name = source.get("name")
        url = source.get("url")
        if not isinstance(name, str) or not name:
            raise DependencyScanError(f"{location}.name must be a non-empty string")
        normalized_name = name.casefold()
        if normalized_name in sources:
            raise DependencyScanError(
                f"tool.poetry.source duplicates source {normalized_name!r}"
            )
        if normalized_name == "pypi":
            if url is not None:
                raise DependencyScanError(
                    f"{location}.url is not allowed for the built-in PyPI source"
                )
            normalized_url = "<built-in-pypi>"
        elif not isinstance(url, str) or not url:
            raise DependencyScanError(
                f"{location}.url is required for a non-PyPI source"
            )
        else:
            normalized_url = url
        priority = source.get("priority", "primary")
        if priority not in {"primary", "supplemental", "explicit"}:
            raise DependencyScanError(f"{location}.priority is invalid")
        for key in ("indexed", "links"):
            if key in source and not isinstance(source[key], bool):
                raise DependencyScanError(f"{location}.{key} must be a boolean")
        normalized_source: dict[str, object] = {
            "name": normalized_name,
            "url": normalized_url,
            "priority": priority,
            "links": source.get("links", False),
            "indexed": source.get("indexed", True),
        }
        fingerprint = source_fingerprint(
            "poetry-index",
            json.dumps(normalized_source, sort_keys=True, separators=(",", ":")),
        )
        sources[normalized_name] = fingerprint
        policy_data.append(normalized_source)
    policy = DependencyRecord(
        name="poetry-source-policy",
        version=None,
        source=source_fingerprint(
            "poetry-source-policy",
            json.dumps(policy_data, sort_keys=True, separators=(",", ":")),
        ),
        kind="policy",
        origin="pyproject.toml:tool.poetry.source",
    )
    return sources, policy


def _poetry_source(
    value: dict[str, object],
    sources: dict[str, str],
    location: str,
) -> str | None:
    origin_keys = [key for key in ("git", "url", "path", "file") if key in value]
    if len(origin_keys) > 1:
        raise DependencyScanError(f"{location} has multiple dependency sources")
    revisions = [key for key in ("branch", "tag", "rev") if key in value]
    if len(revisions) > 1:
        raise DependencyScanError(f"{location} has multiple VCS revisions")
    if revisions and origin_keys != ["git"]:
        raise DependencyScanError(f"{location} has a revision without git")
    if origin_keys and "source" in value:
        raise DependencyScanError(f"{location} combines direct and named sources")

    if origin_keys:
        source_data = {
            key: value[key]
            for key in (
                *origin_keys,
                "branch",
                "tag",
                "rev",
                "subdirectory",
                "develop",
            )
            if key in value
        }
        return source_fingerprint(
            f"poetry-{origin_keys[0]}",
            json.dumps(source_data, sort_keys=True, separators=(",", ":")),
        )
    source_name = value.get("source")
    if source_name is None:
        return None
    assert isinstance(source_name, str)
    normalized_name = source_name.casefold()
    if normalized_name == "pypi":
        return source_fingerprint("poetry-index-name", "pypi")
    try:
        return sources[normalized_name]
    except KeyError as exc:
        raise DependencyScanError(
            f"{location} references unknown Poetry source {source_name!r}"
        ) from exc


def _validate_poetry_table(value: dict[str, object], location: str) -> None:
    keys = set(value)
    if not any(
        required <= keys <= allowed for required, allowed in _POETRY_DEPENDENCY_FAMILIES
    ):
        raise DependencyScanError(
            f"{location} does not match a supported Poetry dependency form"
        )
    for key in _POETRY_STRING_KEYS & value.keys():
        if not isinstance(value[key], str):
            raise DependencyScanError(f"{location}.{key} must be a string")
    for key in _POETRY_BOOL_KEYS & value.keys():
        if not isinstance(value[key], bool):
            raise DependencyScanError(f"{location}.{key} must be a boolean")
    if "extras" in value and (
        not isinstance(value["extras"], list)
        or not all(isinstance(extra, str) for extra in value["extras"])
    ):
        raise DependencyScanError(f"{location}.extras must be a list of strings")
    for key in _POETRY_STRING_KEYS & value.keys():
        if not value[key]:
            raise DependencyScanError(f"{location}.{key} cannot be empty")


def _poetry_constraint(value: str) -> str | None:
    normalized = _POETRY_CONSTRAINT_COMMAS.sub(",", " ".join(value.split()))
    if (
        not normalized
        or len(normalized) > _MAX_POETRY_CONSTRAINT_CHARS
        or _POETRY_CONSTRAINT_SYNTAX.fullmatch(normalized) is None
    ):
        raise DependencyScanError(
            "Poetry dependency contains an unsupported version constraint"
        )
    return None if normalized == "*" else normalized


def _poetry_input_fingerprint(
    value: dict[str, object],
    keys: set[str],
    fingerprint_type: str,
) -> str | None:
    data = {key: value[key] for key in sorted(keys & value.keys())}
    if not data:
        return None
    return source_fingerprint(
        fingerprint_type,
        json.dumps(data, sort_keys=True, separators=(",", ":")),
    )


def _poetry_dependency_records(
    name: str,
    raw_value: object,
    *,
    origin: str,
    sources: dict[str, str],
) -> set[DependencyRecord]:
    canonical_name = canonicalize_dependency_name(name)
    values = raw_value if isinstance(raw_value, list) else [raw_value]
    if not values:
        raise DependencyScanError(f"{origin}.{name} cannot be an empty list")
    if len(values) > _MAX_GROUP_ITEMS:
        raise DependencyScanError(f"{origin}.{name} exceeds {_MAX_GROUP_ITEMS} items")
    records: set[DependencyRecord] = set()
    for index, value in enumerate(values):
        location = f"{origin}.{name}.{index}" if len(values) > 1 else f"{origin}.{name}"
        if isinstance(value, str):
            version = _poetry_constraint(value)
            source = None
            qualifiers = None
            context = None
        elif isinstance(value, dict) and all(isinstance(key, str) for key in value):
            _validate_poetry_table(value, location)
            raw_version = value.get("version")
            version = (
                None
                if raw_version in {None, "*"}
                else _poetry_constraint(str(raw_version))
            )
            source = _poetry_source(value, sources, location)
            context = _poetry_input_fingerprint(
                value,
                _POETRY_CONTEXT_KEYS,
                "poetry-context",
            )
            qualifiers = None
        else:
            raise DependencyScanError(
                f"{location} must be a string or dependency table"
            )
        records.add(
            DependencyRecord(
                name=canonical_name,
                version=version,
                source=source,
                kind="declared",
                origin=origin,
                qualifiers=qualifiers,
                context=context,
            )
        )
    return records


def _poetry_dependency_table_records(
    value: object,
    *,
    origin: str,
    sources: dict[str, str],
) -> set[DependencyRecord]:
    dependencies = _table(value, origin)
    if len(dependencies) > _MAX_GROUP_ITEMS:
        raise DependencyScanError(f"{origin} exceeds {_MAX_GROUP_ITEMS} dependencies")
    records: set[DependencyRecord] = set()
    for name, dependency in dependencies.items():
        if name.casefold() == "python":
            continue
        records.update(
            _poetry_dependency_records(
                name,
                dependency,
                origin=origin,
                sources=sources,
            )
        )
    return records


def _poetry_python_policy(
    dependencies: dict[str, object],
    sources: dict[str, str],
) -> DependencyRecord | None:
    python_entry = next(
        (value for name, value in dependencies.items() if name.casefold() == "python"),
        None,
    )
    if python_entry is None:
        return None
    parsed = _poetry_dependency_records(
        "python",
        python_entry,
        origin="pyproject.toml:tool.poetry.dependencies.python",
        sources=sources,
    )
    data = sorted(
        [
            [
                record.version,
                record.source,
                record.context,
                record.qualifiers,
            ]
            for record in parsed
        ],
        key=lambda item: json.dumps(item, separators=(",", ":")),
    )
    return DependencyRecord(
        name="python-compatibility-policy",
        version=None,
        source=None,
        kind="policy",
        origin="pyproject.toml:tool.poetry.dependencies.python",
        qualifiers=source_fingerprint(
            "poetry-python-compatibility",
            json.dumps(data, separators=(",", ":")),
        ),
    )


def _poetry_extras_policy(poetry: dict[str, object]) -> DependencyRecord | None:
    if "extras" not in poetry:
        return None
    extras = _table(poetry.get("extras"), "tool.poetry.extras")
    if len(extras) > _MAX_GROUPS:
        raise DependencyScanError(f"tool.poetry.extras exceeds {_MAX_GROUPS} extras")
    normalized_extras: dict[str, list[str]] = {}
    for raw_extra, raw_dependencies in extras.items():
        normalized_extra = canonicalize_dependency_name(raw_extra)
        if normalized_extra in normalized_extras:
            raise DependencyScanError(
                "tool.poetry.extras contains duplicate normalized extras"
            )
        normalized_extras[normalized_extra] = [
            canonicalize_dependency_name(dependency)
            for dependency in _string_list(
                raw_dependencies,
                f"tool.poetry.extras.{raw_extra}",
            )
        ]
    return DependencyRecord(
        name="poetry-extras-policy",
        version=None,
        source=None,
        kind="policy",
        origin="pyproject.toml:tool.poetry.extras",
        qualifiers=source_fingerprint(
            "poetry-extras-policy",
            json.dumps(normalized_extras, sort_keys=True, separators=(",", ":")),
        ),
    )


def _poetry_records(
    tool: dict[str, object],
) -> tuple[
    set[DependencyRecord],
    bool,
    dict[str, str],
    dict[str, list[str]],
]:
    poetry = _table(tool.get("poetry"), "tool.poetry")
    if not poetry:
        return set(), False, {}, {}
    sources, source_policy = _poetry_sources(poetry)
    poetry_dependencies = _table(
        poetry.get("dependencies"),
        "tool.poetry.dependencies",
    )
    records = _poetry_dependency_table_records(
        poetry_dependencies,
        origin="pyproject.toml:tool.poetry.dependencies",
        sources=sources,
    )
    if source_policy is not None:
        records.add(source_policy)
    python_policy = _poetry_python_policy(poetry_dependencies, sources)
    if python_policy is not None:
        records.add(python_policy)
    extras_policy = _poetry_extras_policy(poetry)
    if extras_policy is not None:
        records.add(extras_policy)
    supplies_dynamic = "dependencies" in poetry
    group_names: dict[str, str] = {}
    group_edges: dict[str, list[str]] = {}

    groups = _table(poetry.get("group"), "tool.poetry.group")
    if len(groups) > _MAX_GROUPS:
        raise DependencyScanError(f"tool.poetry.group exceeds {_MAX_GROUPS} groups")
    for raw_group, raw_group_value in groups.items():
        normalized_group = canonicalize_dependency_name(raw_group)
        previous_group = group_names.setdefault(normalized_group, raw_group)
        if previous_group != raw_group:
            raise DependencyScanError(
                f"tool.poetry.group duplicates normalized group {normalized_group!r}"
            )
        group = _table(raw_group_value, f"tool.poetry.group.{raw_group}")
        unknown_keys = set(group) - {"dependencies", "include-groups", "optional"}
        if not group or unknown_keys:
            raise DependencyScanError(
                f"tool.poetry.group.{raw_group} must contain only dependencies, "
                "include-groups, or optional"
            )
        if "optional" in group and not isinstance(group["optional"], bool):
            raise DependencyScanError(
                f"tool.poetry.group.{raw_group}.optional must be a boolean"
            )
        if group.get("optional") is True:
            records.add(
                DependencyRecord(
                    name="dependency-group-policy",
                    version=None,
                    source=None,
                    kind="policy",
                    origin=f"pyproject.toml:tool.poetry.group.{raw_group}.optional",
                    qualifiers=source_fingerprint("group-optional", "true"),
                )
            )
        records.update(
            _poetry_dependency_table_records(
                group.get("dependencies"),
                origin=f"pyproject.toml:tool.poetry.group.{raw_group}.dependencies",
                sources=sources,
            )
        )
        if len(records) > _MAX_GROUP_ITEMS:
            raise DependencyScanError(
                f"tool.poetry declarations exceed {_MAX_GROUP_ITEMS} records"
            )
        includes = _string_list(
            group.get("include-groups"),
            f"tool.poetry.group.{raw_group}.include-groups",
        )
        group_edges[normalized_group] = [
            canonicalize_dependency_name(included) for included in includes
        ]

    records.update(
        _poetry_dependency_table_records(
            poetry.get("dev-dependencies"),
            origin="pyproject.toml:tool.poetry.dev-dependencies",
            sources=sources,
        )
    )
    if "dev-dependencies" in poetry:
        group_names.setdefault("dev", "dev")
        group_edges.setdefault("dev", [])
    return records, supplies_dynamic, group_names, group_edges


def _pdm_editable_record(requirement: str, origin: str) -> DependencyRecord:
    stripped = requirement.lstrip()
    if stripped.startswith("-e "):
        target = stripped.removeprefix("-e ").strip()
    elif stripped.startswith("--editable "):
        target = stripped.removeprefix("--editable ").strip()
    else:
        raise DependencyScanError(
            f"{origin} contains an unsupported editable requirement"
        )
    if "#egg=" not in target or "://" not in target:
        raise DependencyScanError(
            f"{origin} contains an unsupported editable requirement"
        )
    name = target.split("#egg=", maxsplit=1)[1].split("&", maxsplit=1)[0]
    return DependencyRecord(
        name=canonicalize_dependency_name(name),
        version=None,
        source=source_fingerprint("pdm-editable", target),
        kind="declared",
        origin=origin,
    )


def _pdm_source_policy(pdm: dict[str, object]) -> DependencyRecord | None:
    resolution = _table(pdm.get("resolution"), "tool.pdm.resolution")
    respect_order = resolution.get("respect-source-order", False)
    if not isinstance(respect_order, bool):
        raise DependencyScanError(
            "tool.pdm.resolution.respect-source-order must be a boolean"
        )
    raw_sources = pdm.get("source")
    if raw_sources is None:
        if not respect_order:
            return None
        raw_sources = []
    if not isinstance(raw_sources, list):
        raise DependencyScanError("tool.pdm.source must be an array of tables")
    policy_sources: list[dict[str, object]] = []
    allowed_keys = {
        "ca_certs",
        "client_cert",
        "client_key",
        "exclude_packages",
        "include_packages",
        "name",
        "password",
        "type",
        "url",
        "username",
        "verify_ssl",
    }
    source_names: set[str] = set()
    for index, raw_source in enumerate(raw_sources):
        location = f"tool.pdm.source.{index}"
        source = _table(raw_source, location)
        unknown_keys = set(source) - allowed_keys
        if unknown_keys:
            raise DependencyScanError(
                f"{location} contains unsupported keys: {sorted(unknown_keys)}"
            )
        name = source.get("name")
        url = source.get("url")
        if not isinstance(name, str) or not name:
            raise DependencyScanError(f"{location}.name must be a non-empty string")
        if name in source_names:
            raise DependencyScanError("tool.pdm.source duplicates source name")
        source_names.add(name)
        if not isinstance(url, str) or not url:
            raise DependencyScanError(
                f"{location}.url is required for deterministic source scanning"
            )
        source_type = source.get("type", "index")
        if source_type not in {"index", "find_links"}:
            raise DependencyScanError(f"{location}.type is invalid")
        if "verify_ssl" in source and not isinstance(source["verify_ssl"], bool):
            raise DependencyScanError(f"{location}.verify_ssl must be a boolean")
        for key in ("include_packages", "exclude_packages"):
            if key in source:
                _string_list(source[key], f"{location}.{key}")
        for key in (
            "ca_certs",
            "client_cert",
            "client_key",
            "password",
            "username",
        ):
            if key in source and not isinstance(source[key], str):
                raise DependencyScanError(f"{location}.{key} must be a string")
        policy_sources.append(
            {
                **source,
                "name": name,
                "type": source_type,
                "verify_ssl": source.get("verify_ssl", True),
            }
        )
    return DependencyRecord(
        name="pdm-source-policy",
        version=None,
        source=source_fingerprint(
            "pdm-source-policy",
            json.dumps(
                {
                    "sources": policy_sources,
                    "respect-source-order": respect_order,
                },
                sort_keys=True,
                separators=(",", ":"),
            ),
        ),
        kind="policy",
        origin="pyproject.toml:tool.pdm.source-policy",
    )


def _pdm_records(
    tool: dict[str, object],
) -> tuple[set[DependencyRecord], set[str], bool]:
    pdm_present = "pdm" in tool
    pdm = _table(tool.get("pdm"), "tool.pdm")
    if not pdm:
        return set(), set(), pdm_present
    groups = _table(pdm.get("dev-dependencies"), "tool.pdm.dev-dependencies")
    if len(groups) > _MAX_GROUPS:
        raise DependencyScanError(
            f"tool.pdm.dev-dependencies exceeds {_MAX_GROUPS} groups"
        )
    records: set[DependencyRecord] = set()
    source_policy = _pdm_source_policy(pdm)
    if source_policy is not None:
        records.add(source_policy)
    normalized_groups: set[str] = set()
    item_count = 0
    for raw_group, raw_requirements in groups.items():
        normalized_group = canonicalize_dependency_name(raw_group)
        if normalized_group in normalized_groups:
            raise DependencyScanError(
                "tool.pdm.dev-dependencies contains duplicate normalized groups"
            )
        normalized_groups.add(normalized_group)
        origin = f"pyproject.toml:tool.pdm.dev-dependencies.{raw_group}"
        for requirement in _string_list(raw_requirements, origin):
            item_count += 1
            if item_count > _MAX_GROUP_ITEMS:
                raise DependencyScanError(
                    f"tool.pdm.dev-dependencies exceeds {_MAX_GROUP_ITEMS} items"
                )
            stripped = requirement.lstrip()
            record = (
                _pdm_editable_record(requirement, origin)
                if stripped.startswith(("-e ", "--editable "))
                else parse_pep508_dependency(requirement, origin=origin)
            )
            records.add(record)
    return records, normalized_groups, pdm_present


def _combined_group_policy(
    pep_names: dict[str, str],
    pep_edges: dict[str, list[str]],
    pep_sequences: dict[str, list[tuple[str, str]]],
    poetry_names: dict[str, str],
    poetry_edges: dict[str, list[str]],
) -> DependencyRecord | None:
    names = dict(pep_names)
    for normalized_name, raw_name in poetry_names.items():
        previous = names.setdefault(normalized_name, raw_name)
        if previous != raw_name:
            raise DependencyScanError(
                f"dependency group spelling collision: {previous!r}, {raw_name!r}"
            )

    edges = {name: list(includes) for name, includes in pep_edges.items()}
    for name, includes in poetry_edges.items():
        edges.setdefault(name, []).extend(includes)
    for name in names:
        edges.setdefault(name, [])
    for group, includes in edges.items():
        for included in includes:
            if included not in names:
                raise DependencyScanError(
                    f"dependency-group {names[group]!r} includes missing group "
                    f"{included!r}"
                )

    visited: set[str] = set()
    active: list[str] = []

    def visit(group: str) -> None:
        if group in visited:
            return
        if group in active:
            cycle = " -> ".join([*active, group])
            raise DependencyScanError(f"dependency-group include cycle: {cycle}")
        if len(active) >= _MAX_GROUP_DEPTH:
            raise DependencyScanError(
                f"dependency-group nesting exceeds {_MAX_GROUP_DEPTH}"
            )
        active.append(group)
        for included in edges[group]:
            visit(included)
        active.pop()
        visited.add(group)

    for group in sorted(names):
        visit(group)
    if not names:
        return None
    graph = {
        "pep735-items": {
            group: pep_sequences[group] for group in sorted(pep_sequences)
        },
        "poetry-includes": {
            group: poetry_edges[group] for group in sorted(poetry_edges)
        },
    }
    return DependencyRecord(
        name="dependency-group-policy",
        version=None,
        source=None,
        qualifiers=source_fingerprint(
            "dependency-group-policy",
            json.dumps(graph, sort_keys=True, separators=(",", ":")),
        ),
        kind="policy",
        origin="pyproject.toml:dependency-groups",
    )


def parse_pyproject_dependencies(document: dict[str, object]) -> set[DependencyRecord]:
    """Parse supported PEP 621/735, Poetry, and PDM declarations."""
    records: set[DependencyRecord] = set()
    tool = _table(document.get("tool"), "tool")
    dependency_groups = _table(document.get("dependency-groups"), "dependency-groups")
    pep_names, pep_edges, pep_sequences, group_records = _dependency_group_data(
        dependency_groups
    )
    poetry_records, poetry_supplies_dynamic, poetry_groups, poetry_edges = (
        _poetry_records(tool)
    )
    pdm_records, pdm_groups, pdm_present = _pdm_records(tool)
    records.update(poetry_records)
    records.update(pdm_records)
    records.update(group_records)
    group_policy = _combined_group_policy(
        pep_names,
        pep_edges,
        pep_sequences,
        poetry_groups,
        poetry_edges,
    )
    if group_policy is not None:
        records.add(group_policy)

    project = _table(document.get("project"), "project")
    dynamic = set(_string_list(project.get("dynamic"), "project.dynamic"))
    if "dependencies" in dynamic and "dependencies" in project:
        raise DependencyScanError(
            "project.dependencies cannot be both static and dynamic"
        )
    if "optional-dependencies" in dynamic and "optional-dependencies" in project:
        raise DependencyScanError(
            "project.optional-dependencies cannot be both static and dynamic"
        )
    if "dependencies" in dynamic and not poetry_supplies_dynamic:
        raise DependencyScanError(
            "dynamic dependency fields cannot be scanned deterministically: "
            "dependencies"
        )
    if "optional-dependencies" in dynamic:
        raise DependencyScanError(
            "dynamic dependency fields cannot be scanned deterministically: "
            "optional-dependencies"
        )
    if "requires-python" in dynamic:
        raise DependencyScanError(
            "dynamic dependency fields cannot be scanned deterministically: "
            "requires-python"
        )
    requires_python = project.get("requires-python")
    if requires_python is not None:
        if not isinstance(requires_python, str) or not requires_python:
            raise DependencyScanError(
                "project.requires-python must be a non-empty string"
            )
        try:
            normalized_requires_python = str(SpecifierSet(requires_python))
        except InvalidSpecifier as exc:
            raise DependencyScanError("project.requires-python is invalid") from exc
        records.add(
            DependencyRecord(
                name="python-compatibility-policy",
                version=None,
                source=None,
                kind="policy",
                origin="pyproject.toml:project.requires-python",
                qualifiers=source_fingerprint(
                    "project-requires-python",
                    normalized_requires_python,
                ),
            )
        )
    records.update(
        _pep508_records(
            _string_list(project.get("dependencies"), "project.dependencies"),
            "pyproject.toml:project.dependencies",
        )
    )
    optional = _table(
        project.get("optional-dependencies"),
        "project.optional-dependencies",
    )
    optional_groups: set[str] = set()
    for group, requirements in optional.items():
        normalized_group = canonicalize_dependency_name(group)
        if normalized_group in optional_groups:
            raise DependencyScanError(
                "project.optional-dependencies contains duplicate normalized groups"
            )
        optional_groups.add(normalized_group)
        origin = f"pyproject.toml:project.optional-dependencies.{group}"
        records.update(_pep508_records(_string_list(requirements, origin), origin))

    pdm_development_groups = pdm_groups | (set(pep_names) if pdm_present else set())
    if optional_groups & pdm_development_groups:
        overlap = sorted(optional_groups & pdm_development_groups)
        raise DependencyScanError(
            f"optional and development dependency groups overlap: {overlap}"
        )

    build_system = _table(document.get("build-system"), "build-system")
    records.update(
        _pep508_records(
            _string_list(build_system.get("requires"), "build-system.requires"),
            "pyproject.toml:build-system.requires",
        )
    )
    return records
