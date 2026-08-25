"""Tests for typed declaration, constraint, and lockfile snapshots."""

import json
from pathlib import Path

import pytest

from agentcompat.dependencies import (
    DependencyScanError,
    scan_python_dependencies,
    scan_python_dependency_snapshot,
)
from agentcompat.dependency_types import compare_dependency_snapshots


def test_snapshot_reads_pep508_and_dependency_groups(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text(
        """
[build-system]
requires = ["Setuptools>=69"]

[project]
name = "example"
version = "1"
dependencies = [
  "Requests[socks]>=2,<3,!=2.5; python_version >= '3.10'",
  "Widget @ https://example.invalid/widget.whl#sha256=abc",
]

[project.optional-dependencies]
test = ["PyTest~=8.2"]

[dependency-groups]
lint = ["Ruff==0.11.*"]
shared = ["typing-extensions; python_version < '3.13'"]
dev = [
  {include-group = "lint"},
  {include-group = "shared"},
  "tox>=4",
]
""".strip(),
        encoding="utf-8",
    )

    snapshot = scan_python_dependency_snapshot(tmp_path, include_lockfiles=False)

    assert snapshot.direct_names == frozenset(
        {
            "pytest",
            "requests",
            "ruff",
            "setuptools",
            "tox",
            "typing-extensions",
            "widget",
        }
    )
    request_record = next(
        record
        for record in snapshot.records
        if record.name == "requests" and "project.dependencies" in record.origin
    )
    assert request_record.version == "!=2.5,<3,>=2"
    widget_record = next(
        record for record in snapshot.records if record.name == "widget"
    )
    assert widget_record.source is not None
    assert "example.invalid" not in widget_record.source


@pytest.mark.parametrize(
    "groups, message",
    [
        ("foo_bar = []\nfoo-bar = []", "duplicate normalized group"),
        ('dev = [{include-group = "missing"}]', "missing group"),
        (
            'one = [{include-group = "two"}]\ntwo = [{include-group = "one"}]',
            "include cycle",
        ),
        ('dev = [{include-group = "other", extra = true}]', "invalid item"),
        ('dev = ["requests ^2"]', "invalid PEP 508"),
    ],
)
def test_snapshot_rejects_invalid_dependency_groups(
    tmp_path: Path,
    groups: str,
    message: str,
) -> None:
    (tmp_path / "pyproject.toml").write_text(
        f"[dependency-groups]\n{groups}\n",
        encoding="utf-8",
    )

    with pytest.raises(DependencyScanError, match=message):
        scan_python_dependency_snapshot(tmp_path, include_lockfiles=False)


def test_snapshot_reads_poetry_and_pdm_declarations(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text(
        """
[project]
name = "example"
version = "1"
dependencies = ["Requests>=2"]

[[tool.poetry.source]]
name = "corp"
url = "https://user:secret@packages.example.invalid/simple"
priority = "explicit"

[tool.poetry.dependencies]
python = "^3.12"
private-package = {version = ">=1,<2", source = "corp"}
vcs-pkg = {git = "https://example.invalid/repo.git", tag = "v1"}
local-pkg = {path = "../local", develop = true}

[tool.poetry.group.test.dependencies]
coverage = "^7"

[tool.poetry.dev-dependencies]
tox = "*"

[tool.pdm.dev-dependencies]
lint = ["Ruff>=0.11"]
dev = ["-e git+https://example.invalid/plugin.git#egg=My_Plugin"]
""".strip(),
        encoding="utf-8",
    )

    snapshot = scan_python_dependency_snapshot(tmp_path, include_lockfiles=False)

    assert snapshot.direct_names == frozenset(
        {
            "coverage",
            "local-pkg",
            "my-plugin",
            "private-package",
            "requests",
            "ruff",
            "tox",
            "vcs-pkg",
        }
    )
    assert "python" not in snapshot.direct_names
    serialized_sources = " ".join(record.source or "" for record in snapshot.records)
    assert "secret" not in serialized_sources
    private = next(
        record for record in snapshot.records if record.name == "private-package"
    )
    assert private.version == ">=1,<2"
    assert private.source is not None


def test_explicit_manifest_roles_replace_nested_filename_heuristics(
    tmp_path: Path,
) -> None:
    requirements = tmp_path / "requirements"
    requirements.mkdir()
    (requirements / "notes.txt").write_text("ignored-package\n", encoding="utf-8")
    (requirements / "base.in").write_text("direct-package>=1\n", encoding="utf-8")
    constraints = tmp_path / "constraints"
    constraints.mkdir()
    (constraints / "pins.txt").write_text(
        "direct-package==1.2\nconstraint-only==9\n",
        encoding="utf-8",
    )

    default_snapshot = scan_python_dependency_snapshot(
        tmp_path,
        include_lockfiles=False,
    )
    explicit_snapshot = scan_python_dependency_snapshot(
        tmp_path,
        manifest_roles=[
            ("requirements/base.in", "requirement"),
            ("constraints/pins.txt", "constraint"),
        ],
        include_lockfiles=False,
    )

    assert default_snapshot.direct_names == frozenset()
    assert explicit_snapshot.direct_names == frozenset({"direct-package"})
    assert {record.name for record in explicit_snapshot.records} == {
        "constraint-only",
        "direct-package",
    }
    assert any(record.kind == "constraint" for record in explicit_snapshot.records)


def test_poetry_lock_packages_stay_transitive(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text(
        """
[project]
name = "example"
version = "1"
dependencies = ["requests>=2"]
""".strip(),
        encoding="utf-8",
    )
    (tmp_path / "poetry.lock").write_text(
        """
[[package]]
name = "requests"
version = "2.32.5"

[[package]]
name = "urllib3"
version = "2.5.0"

[metadata]
lock-version = "2.1"
""".strip(),
        encoding="utf-8",
    )

    snapshot = scan_python_dependency_snapshot(tmp_path)

    assert scan_python_dependencies(tmp_path) == frozenset({"requests"})
    assert snapshot.direct_names == frozenset({"requests"})
    assert snapshot.locked_names == frozenset({"requests", "urllib3"})


def test_snapshot_parses_supported_lockfile_formats(tmp_path: Path) -> None:
    (tmp_path / "pdm.lock").write_text(
        """
[metadata]
lock_version = "4.5.0"
strategy = []

[[package]]
name = "PDM_Package"
version = "1.2.3"
""".strip(),
        encoding="utf-8",
    )
    (tmp_path / "Pipfile.lock").write_text(
        json.dumps(
            {
                "_meta": {
                    "pipfile-spec": 6,
                    "sources": [{"name": "pypi", "url": "https://pypi.org/simple"}],
                },
                "default": {"Pip_File": {"version": "==2.0"}},
            }
        ),
        encoding="utf-8",
    )
    (tmp_path / "pylock.toml").write_text(
        """
lock-version = "1.0"
created-by = "tests"

[[packages]]
name = "pylock-package"
version = "3.0"
index = "https://pypi.org/simple"
wheels = [{url = "https://files.example/pylock-package.whl", hashes = {sha256 = "abc"}}]
""".strip(),
        encoding="utf-8",
    )
    (tmp_path / "uv.lock").write_text(
        """
version = 1
revision = 3
requires-python = ">=3.12"

[[package]]
name = "uv-package"
version = "4.0"
source = { registry = "https://pypi.org/simple" }
""".strip(),
        encoding="utf-8",
    )

    snapshot = scan_python_dependency_snapshot(tmp_path)

    assert snapshot.locked_names == frozenset(
        {"pdm-package", "pip-file", "pylock-package", "uv-package"}
    )
    assert {lockfile.path for lockfile in snapshot.lockfiles} == {
        "Pipfile.lock",
        "pdm.lock",
        "pylock.toml",
        "uv.lock",
    }


def test_pdm_source_rule_fails_closed_without_static_urls(tmp_path: Path) -> None:
    (tmp_path / "pdm.lock").write_text(
        """
[metadata]
lock_version = "4.5.0"
strategy = []

[[package]]
name = "package"
version = "1"
files = [{file = "package.whl", hash = "sha256:abc"}]
""".strip(),
        encoding="utf-8",
    )

    with pytest.raises(DependencyScanError, match="incomplete without static_urls"):
        scan_python_dependency_snapshot(tmp_path, require_lock_sources=True)


@pytest.mark.parametrize(
    "filename, content, message",
    [
        (
            "poetry.lock",
            '[metadata]\nlock-version = "3.0"\npackage = []\n',
            "unsupported",
        ),
        (
            "pdm.lock",
            '[metadata]\nlock_version = "5.0.0"\npackage = []\n',
            "unsupported",
        ),
        (
            "pylock.toml",
            'lock-version = "2.0"\ncreated-by = "tests"\npackages = []\n',
            "unsupported",
        ),
        (
            "uv.lock",
            'version = 1\nrevision = 4\nrequires-python = ">=3.12"\npackage = []\n',
            "revision",
        ),
    ],
)
def test_snapshot_rejects_unsupported_lockfile_versions(
    tmp_path: Path,
    filename: str,
    content: str,
    message: str,
) -> None:
    (tmp_path / filename).write_text(content, encoding="utf-8")

    with pytest.raises(DependencyScanError, match=message):
        scan_python_dependency_snapshot(tmp_path)


def test_snapshot_comparison_reports_version_source_and_lock_drift(
    tmp_path: Path,
) -> None:
    pyproject = tmp_path / "pyproject.toml"
    pyproject.write_text(
        """
[project]
name = "example"
version = "1"
dependencies = ["package>=1"]
""".strip(),
        encoding="utf-8",
    )
    lockfile = tmp_path / "pylock.toml"
    lockfile.write_text(
        """
lock-version = "1.0"
created-by = "tests"
[[packages]]
name = "package"
version = "1.5"
index = "https://one.example/simple"
wheels = [{url = "https://one.example/package.whl", hashes = {sha256 = "abc"}}]
""".strip(),
        encoding="utf-8",
    )
    before = scan_python_dependency_snapshot(tmp_path)

    pyproject.write_text(
        """
[project]
name = "example"
version = "1"
dependencies = ["package>=2"]
""".strip(),
        encoding="utf-8",
    )
    lockfile.write_text(
        """
lock-version = "1.0"
created-by = "tests"
[[packages]]
name = "package"
version = "2.1"
index = "https://two.example/simple"
wheels = [{url = "https://two.example/package.whl", hashes = {sha256 = "def"}}]
""".strip(),
        encoding="utf-8",
    )
    after = scan_python_dependency_snapshot(tmp_path)

    delta = compare_dependency_snapshots(before, after)

    assert delta.added == ()
    assert delta.removed == ()
    assert len(delta.version_changes) == 2
    assert len(delta.source_changes) == 1
    assert [change.evidence() for change in delta.lockfile_changes] == [
        "pylock.toml: modified"
    ]


def test_snapshot_comparison_detects_origin_moves_and_complete_inputs(
    tmp_path: Path,
) -> None:
    pyproject = tmp_path / "pyproject.toml"
    pyproject.write_text(
        """
[project]
name = "example"
version = "1"
dependencies = ["package[one]>=1; python_version >= '3.11'"]
""".strip(),
        encoding="utf-8",
    )
    before = scan_python_dependency_snapshot(tmp_path, include_lockfiles=False)

    pyproject.write_text(
        """
[project]
name = "example"
version = "1"
[project.optional-dependencies]
runtime = ["package[two]>=2; python_version >= '3.12'"]
""".strip(),
        encoding="utf-8",
    )
    after = scan_python_dependency_snapshot(tmp_path, include_lockfiles=False)
    delta = compare_dependency_snapshots(before, after)

    assert delta.added == ()
    assert delta.removed == ()
    assert len(delta.version_changes) == 2
    assert len(delta.declaration_changes) == 2


def test_pip_source_policy_is_hashed_and_aliases_are_normalized(
    tmp_path: Path,
) -> None:
    requirements = tmp_path / "requirements.txt"
    requirements.write_text(
        "-i https://user:old-secret@packages.example/simple\npackage\n",
        encoding="utf-8",
    )
    before = scan_python_dependency_snapshot(tmp_path, include_lockfiles=False)

    requirements.write_text(
        "--index-url https://user:old-secret@packages.example/simple\npackage\n",
        encoding="utf-8",
    )
    alias_only = scan_python_dependency_snapshot(tmp_path, include_lockfiles=False)
    assert compare_dependency_snapshots(before, alias_only).source_changes == ()

    requirements.write_text(
        "--index-url https://user:new-secret@other.example/simple\npackage\n",
        encoding="utf-8",
    )
    after = scan_python_dependency_snapshot(tmp_path, include_lockfiles=False)
    delta = compare_dependency_snapshots(alias_only, after)

    assert len(delta.source_changes) == 1
    assert "old-secret" not in delta.source_changes[0].evidence()
    assert "new-secret" not in delta.source_changes[0].evidence()


def test_pip_include_order_preserves_effective_source_policy(
    tmp_path: Path,
) -> None:
    (tmp_path / "a.in").write_text(
        "--index-url https://a.example/simple\n",
        encoding="utf-8",
    )
    (tmp_path / "b.in").write_text(
        "--index-url https://b.example/simple\n",
        encoding="utf-8",
    )
    requirements = tmp_path / "requirements.txt"
    requirements.write_text("-r a.in\n-r b.in\npackage\n", encoding="utf-8")
    before = scan_python_dependency_snapshot(tmp_path, include_lockfiles=False)
    requirements.write_text("-r b.in\n-r a.in\npackage\n", encoding="utf-8")
    after = scan_python_dependency_snapshot(tmp_path, include_lockfiles=False)

    assert len(compare_dependency_snapshots(before, after).source_changes) == 1


def test_conditional_version_swaps_are_not_hidden_by_origin_aggregation(
    tmp_path: Path,
) -> None:
    pyproject = tmp_path / "pyproject.toml"
    pyproject.write_text(
        """
[project]
name = "example"
version = "1"
dependencies = [
  "package==1; sys_platform == 'linux'",
  "package==2; sys_platform == 'win32'",
]
""".strip(),
        encoding="utf-8",
    )
    before = scan_python_dependency_snapshot(tmp_path, include_lockfiles=False)
    pyproject.write_text(
        """
[project]
name = "example"
version = "1"
dependencies = [
  "package==2; sys_platform == 'linux'",
  "package==1; sys_platform == 'win32'",
]
""".strip(),
        encoding="utf-8",
    )
    after = scan_python_dependency_snapshot(tmp_path, include_lockfiles=False)

    assert len(compare_dependency_snapshots(before, after).version_changes) == 2


def test_marker_only_default_source_change_is_not_source_drift(
    tmp_path: Path,
) -> None:
    pyproject = tmp_path / "pyproject.toml"
    pyproject.write_text(
        """
[project]
name = "example"
version = "1"
dependencies = ["package>=1; sys_platform == 'linux'"]
""".strip(),
        encoding="utf-8",
    )
    before = scan_python_dependency_snapshot(tmp_path, include_lockfiles=False)
    pyproject.write_text(
        """
[project]
name = "example"
version = "1"
dependencies = ["package>=1; sys_platform == 'win32'"]
""".strip(),
        encoding="utf-8",
    )
    after = scan_python_dependency_snapshot(tmp_path, include_lockfiles=False)

    assert compare_dependency_snapshots(before, after).source_changes == ()


def test_conditional_direct_source_swaps_are_detected(tmp_path: Path) -> None:
    pyproject = tmp_path / "pyproject.toml"
    pyproject.write_text(
        """
[project]
name = "example"
version = "1"
dependencies = [
  "package @ https://one.example/package.whl ; sys_platform == 'linux'",
  "package @ https://two.example/package.whl ; sys_platform == 'win32'",
]
""".strip(),
        encoding="utf-8",
    )
    before = scan_python_dependency_snapshot(tmp_path, include_lockfiles=False)
    pyproject.write_text(
        """
[project]
name = "example"
version = "1"
dependencies = [
  "package @ https://two.example/package.whl ; sys_platform == 'linux'",
  "package @ https://one.example/package.whl ; sys_platform == 'win32'",
]
""".strip(),
        encoding="utf-8",
    )
    after = scan_python_dependency_snapshot(tmp_path, include_lockfiles=False)

    assert len(compare_dependency_snapshots(before, after).source_changes) == 2


def test_origin_correlated_version_and_source_swaps_are_detected(
    tmp_path: Path,
) -> None:
    pyproject = tmp_path / "pyproject.toml"
    pyproject.write_text(
        """
[project]
name = "example"
version = "1"
dependencies = ["versioned==1", "sourced @ https://one.example/package.whl"]
[project.optional-dependencies]
runtime = ["versioned==2", "sourced @ https://two.example/package.whl"]
""".strip(),
        encoding="utf-8",
    )
    before = scan_python_dependency_snapshot(tmp_path, include_lockfiles=False)
    pyproject.write_text(
        """
[project]
name = "example"
version = "1"
dependencies = ["versioned==2", "sourced @ https://two.example/package.whl"]
[project.optional-dependencies]
runtime = ["versioned==1", "sourced @ https://one.example/package.whl"]
""".strip(),
        encoding="utf-8",
    )
    after = scan_python_dependency_snapshot(tmp_path, include_lockfiles=False)
    delta = compare_dependency_snapshots(before, after)

    assert len(delta.version_changes) == 2
    assert len(delta.source_changes) == 2


def test_deep_pyproject_fails_closed(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text(
        f"value = {'[' * 500}0{']' * 500}\n",
        encoding="utf-8",
    )
    with pytest.raises(DependencyScanError, match="invalid pyproject.toml"):
        scan_python_dependency_snapshot(tmp_path, include_lockfiles=False)


def test_deep_pep508_marker_fails_closed(tmp_path: Path) -> None:
    marker = f"{'(' * 500}python_version == '3.12'{')' * 500}"
    (tmp_path / "requirements.txt").write_text(
        f"package; {marker}\n",
        encoding="utf-8",
    )
    with pytest.raises(DependencyScanError, match="invalid PEP 508"):
        scan_python_dependency_snapshot(tmp_path, include_lockfiles=False)


def test_poetry_and_pdm_registry_policy_changes_are_source_drift(
    tmp_path: Path,
) -> None:
    pyproject = tmp_path / "pyproject.toml"
    pyproject.write_text(
        """
[[tool.poetry.source]]
name = "corp"
url = "https://user:old-secret@one.example/simple"
priority = "primary"

[[tool.pdm.source]]
name = "mirror"
url = "https://user:old-secret@one.example/pdm"
verify_ssl = true
""".strip(),
        encoding="utf-8",
    )
    before = scan_python_dependency_snapshot(tmp_path, include_lockfiles=False)
    pyproject.write_text(
        """
[[tool.poetry.source]]
name = "corp"
url = "https://user:new-secret@two.example/simple"
priority = "explicit"

[[tool.pdm.source]]
name = "mirror"
url = "https://user:new-secret@two.example/pdm"
verify_ssl = false
""".strip(),
        encoding="utf-8",
    )
    after = scan_python_dependency_snapshot(tmp_path, include_lockfiles=False)
    delta = compare_dependency_snapshots(before, after)

    assert len(delta.source_changes) == 2
    evidence = " ".join(change.evidence() for change in delta.source_changes)
    assert "old-secret" not in evidence
    assert "new-secret" not in evidence


def test_pdm_respect_source_order_is_observed_without_local_sources(
    tmp_path: Path,
) -> None:
    pyproject = tmp_path / "pyproject.toml"
    pyproject.write_text(
        "[tool.pdm.resolution]\nrespect-source-order = false\n",
        encoding="utf-8",
    )
    before = scan_python_dependency_snapshot(tmp_path, include_lockfiles=False)
    pyproject.write_text(
        "[tool.pdm.resolution]\nrespect-source-order = true\n",
        encoding="utf-8",
    )
    after = scan_python_dependency_snapshot(tmp_path, include_lockfiles=False)

    assert len(compare_dependency_snapshots(before, after).source_changes) == 1


def test_python_compatibility_and_poetry_extras_are_resolver_inputs(
    tmp_path: Path,
) -> None:
    pyproject = tmp_path / "pyproject.toml"
    pyproject.write_text(
        """
[project]
name = "example"
version = "1"
requires-python = ">=3.10"

[tool.poetry.dependencies]
python = "^3.10"
feature-package = {version = "1", optional = true}

[tool.poetry.extras]
feature = ["feature-package"]
""".strip(),
        encoding="utf-8",
    )
    before = scan_python_dependency_snapshot(tmp_path, include_lockfiles=False)
    pyproject.write_text(
        """
[project]
name = "example"
version = "1"
requires-python = ">=3.12"

[tool.poetry.dependencies]
python = "^3.12"
feature-package = {version = "1", optional = true}

[tool.poetry.extras]
feature = []
""".strip(),
        encoding="utf-8",
    )
    after = scan_python_dependency_snapshot(tmp_path, include_lockfiles=False)
    delta = compare_dependency_snapshots(before, after)

    assert before.direct_names == frozenset({"feature-package"})
    assert len(delta.declaration_changes) == 6


def test_requirement_hash_changes_are_inputs_not_source_drift(
    tmp_path: Path,
) -> None:
    requirements = tmp_path / "requirements.txt"
    requirements.write_text(
        "package==1 --hash=sha256:aaa\n",
        encoding="utf-8",
    )
    before = scan_python_dependency_snapshot(tmp_path, include_lockfiles=False)
    requirements.write_text(
        "package==1 --hash=sha256:bbb\n",
        encoding="utf-8",
    )
    after = scan_python_dependency_snapshot(tmp_path, include_lockfiles=False)
    delta = compare_dependency_snapshots(before, after)

    assert delta.source_changes == ()
    assert len(delta.declaration_changes) == 2


def test_group_include_order_and_multiplicity_are_resolver_inputs(
    tmp_path: Path,
) -> None:
    pyproject = tmp_path / "pyproject.toml"
    pyproject.write_text(
        """
[dependency-groups]
one = ["one"]
two = ["two"]
dev = [{include-group = "one"}, {include-group = "two"}]
""".strip(),
        encoding="utf-8",
    )
    before = scan_python_dependency_snapshot(tmp_path, include_lockfiles=False)
    pyproject.write_text(
        """
[dependency-groups]
one = ["one"]
two = ["two"]
dev = [{include-group = "two"}, {include-group = "one"}, {include-group = "one"}]
""".strip(),
        encoding="utf-8",
    )
    after = scan_python_dependency_snapshot(tmp_path, include_lockfiles=False)
    delta = compare_dependency_snapshots(before, after)

    assert delta.source_changes == ()
    assert delta.version_changes == ()
    assert len(delta.declaration_changes) == 2


@pytest.mark.parametrize(
    "poetry_fragment",
    [
        "[tool.poetry.group.empty]",
        "[tool.poetry.group.dev]\nunknown = true",
        '[tool.poetry.group.dev]\ninclude-groups = ["missing"]',
        (
            '[tool.poetry.dependencies]\npackage = {git = "https://example/repo", '
            'version = "^1"}'
        ),
    ],
)
def test_snapshot_rejects_invalid_poetry_schema(
    tmp_path: Path,
    poetry_fragment: str,
) -> None:
    (tmp_path / "pyproject.toml").write_text(poetry_fragment, encoding="utf-8")

    with pytest.raises(DependencyScanError):
        scan_python_dependency_snapshot(tmp_path, include_lockfiles=False)


def test_poetry_version_constraint_rejects_source_shaped_secret(
    tmp_path: Path,
) -> None:
    secret = "https://user:secret@example.invalid/package"
    (tmp_path / "pyproject.toml").write_text(
        f'[tool.poetry.dependencies]\npackage = "{secret}"\n',
        encoding="utf-8",
    )

    with pytest.raises(DependencyScanError) as error:
        scan_python_dependency_snapshot(tmp_path, include_lockfiles=False)
    assert "secret" not in str(error.value)


@pytest.mark.parametrize(
    "constraint",
    [
        "package[extra]==1",
        "package @ https://example.invalid/package.whl",
    ],
)
def test_constraint_files_reject_non_constraint_requirements(
    tmp_path: Path,
    constraint: str,
) -> None:
    (tmp_path / "constraints.txt").write_text(f"{constraint}\n", encoding="utf-8")

    with pytest.raises(DependencyScanError, match="constraints cannot"):
        scan_python_dependency_snapshot(
            tmp_path,
            manifest_roles=[("constraints.txt", "constraint")],
            include_lockfiles=False,
        )


def test_configured_lockfile_must_exist(tmp_path: Path) -> None:
    with pytest.raises(DependencyScanError, match="does not exist"):
        scan_python_dependency_snapshot(
            tmp_path,
            lockfiles=["missing.lock"],
            parse_lockfile_semantics=False,
        )


def test_lockfile_fingerprint_uses_exact_bytes(tmp_path: Path) -> None:
    lockfile = tmp_path / "poetry.lock"
    content = 'package = []\n[metadata]\nlock-version = "2.1"\n'
    lockfile.write_bytes(content.encode())
    before = scan_python_dependency_snapshot(
        tmp_path,
        parse_lockfile_semantics=False,
    )
    lockfile.write_bytes(content.replace("\n", "\r\n").encode())
    after = scan_python_dependency_snapshot(
        tmp_path,
        parse_lockfile_semantics=False,
    )

    assert [
        change.evidence()
        for change in compare_dependency_snapshots(before, after).lockfile_changes
    ] == ["poetry.lock: modified"]


def test_automatic_lockfile_deletion_is_reported(tmp_path: Path) -> None:
    lockfile = tmp_path / "poetry.lock"
    lockfile.write_text(
        'package = []\n[metadata]\nlock-version = "2.1"\n',
        encoding="utf-8",
    )
    before = scan_python_dependency_snapshot(
        tmp_path,
        parse_lockfile_semantics=False,
    )
    lockfile.unlink()
    after = scan_python_dependency_snapshot(
        tmp_path,
        parse_lockfile_semantics=False,
    )

    assert [
        change.evidence()
        for change in compare_dependency_snapshots(before, after).lockfile_changes
    ] == ["poetry.lock: removed"]


def test_uv_lock_accepts_url_subdirectory_and_requires_pinned_git(
    tmp_path: Path,
) -> None:
    uv_lock = tmp_path / "uv.lock"
    uv_lock.write_text(
        """
version = 1
requires-python = ">=3.12"
[[package]]
name = "package"
source = {url = "https://example.invalid/package.tar.gz", subdirectory = "src"}
""".strip(),
        encoding="utf-8",
    )
    snapshot = scan_python_dependency_snapshot(tmp_path)
    assert snapshot.locked_names == frozenset({"package"})

    uv_lock.write_text(
        """
version = 1
requires-python = ">=3.12"
[[package]]
name = "package"
source = {git = "https://example.invalid/repo.git#main"}
""".strip(),
        encoding="utf-8",
    )
    with pytest.raises(DependencyScanError, match="immutable SHA"):
        scan_python_dependency_snapshot(tmp_path)


def test_pylock_wheel_order_is_semantically_stable(tmp_path: Path) -> None:
    lockfile = tmp_path / "pylock.toml"
    header = (
        'lock-version = "1.0"\ncreated-by = "tests"\n'
        '[[packages]]\nname = "package"\nversion = "1"\n'
    )
    first = (
        '{name = "one.whl", url = "https://example/one.whl", hashes = {sha256 = "one"}}'
    )
    second = (
        '{name = "two.whl", url = "https://example/two.whl", hashes = {sha256 = "two"}}'
    )
    lockfile.write_text(f"{header}wheels = [{first}, {second}]\n", encoding="utf-8")
    before = scan_python_dependency_snapshot(tmp_path)
    lockfile.write_text(f"{header}wheels = [{second}, {first}]\n", encoding="utf-8")
    after = scan_python_dependency_snapshot(tmp_path)

    delta = compare_dependency_snapshots(before, after)
    assert delta.source_changes == ()
    assert len(delta.lockfile_changes) == 1


def test_pylock_archive_subdirectory_and_poetry_develop_are_source_inputs(
    tmp_path: Path,
) -> None:
    pylock = tmp_path / "pylock.toml"
    pylock.write_text(
        (
            'lock-version = "1.0"\ncreated-by = "tests"\n'
            '[[packages]]\nname = "archive-package"\nversion = "1"\n'
            'archive = {url = "https://example/package.tar.gz", '
            'subdirectory = "one", hashes = {sha256 = "abc"}}'
        ),
        encoding="utf-8",
    )
    poetry_lock = tmp_path / "poetry.lock"
    poetry_lock.write_text(
        """
[[package]]
name = "local-package"
version = "1"
develop = false
source = {type = "directory", url = "../package"}
[metadata]
lock-version = "2.1"
""".strip(),
        encoding="utf-8",
    )
    before = scan_python_dependency_snapshot(tmp_path)
    pylock.write_text(
        pylock.read_text(encoding="utf-8").replace(
            'subdirectory = "one"',
            'subdirectory = "two"',
        ),
        encoding="utf-8",
    )
    poetry_lock.write_text(
        poetry_lock.read_text(encoding="utf-8").replace(
            "develop = false",
            "develop = true",
        ),
        encoding="utf-8",
    )
    after = scan_python_dependency_snapshot(tmp_path)

    assert len(compare_dependency_snapshots(before, after).source_changes) == 2


def test_pdm_static_url_source_rejects_incomplete_file_entries(
    tmp_path: Path,
) -> None:
    (tmp_path / "pdm.lock").write_text(
        """
[metadata]
lock_version = "4.5.1"
strategy = ["static_urls"]
[[package]]
name = "package"
version = "1"
files = [
  {url = "https://example/package.whl"},
  {file = "package.tar.gz"},
]
""".strip(),
        encoding="utf-8",
    )

    with pytest.raises(DependencyScanError, match="url must be a non-empty string"):
        scan_python_dependency_snapshot(tmp_path, require_lock_sources=True)


@pytest.mark.parametrize(
    "filename, content",
    [
        (
            "poetry.lock",
            '[[package]]\nname = "package"\nversion = "not a version"\n'
            '[metadata]\nlock-version = "2.1"\n',
        ),
        (
            "pdm.lock",
            '[metadata]\nlock_version = "4.5.1"\nstrategy = []\n'
            '[[package]]\nname = "package"\nversion = "not a version"\n',
        ),
        (
            "uv.lock",
            'version = 1\nrequires-python = ">=3.12"\n'
            '[[package]]\nname = "package"\nversion = "not a version"\n'
            'source = {registry = "https://pypi.org/simple"}\n',
        ),
    ],
)
def test_lockfiles_reject_invalid_package_versions(
    tmp_path: Path,
    filename: str,
    content: str,
) -> None:
    (tmp_path / filename).write_text(content, encoding="utf-8")
    with pytest.raises(DependencyScanError, match="version is invalid"):
        scan_python_dependency_snapshot(tmp_path)


@pytest.mark.parametrize("version", ["==", "==1,>=2", "==1.*", "==not-a-version"])
def test_pipfile_lock_requires_one_concrete_version(
    tmp_path: Path,
    version: str,
) -> None:
    (tmp_path / "Pipfile.lock").write_text(
        json.dumps(
            {
                "_meta": {
                    "pipfile-spec": 6,
                    "sources": [{"name": "pypi", "url": "https://pypi.org/simple"}],
                },
                "default": {"package": {"version": version}},
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(DependencyScanError, match="version"):
        scan_python_dependency_snapshot(tmp_path)


def test_nonstandard_multi_dot_pylock_name_is_not_auto_discovered(
    tmp_path: Path,
) -> None:
    (tmp_path / "pylock.foo.bar.toml").write_text("not valid TOML =", encoding="utf-8")
    snapshot = scan_python_dependency_snapshot(tmp_path)
    assert snapshot.lockfiles == ()

    with pytest.raises(DependencyScanError, match="unsupported semantic lockfile"):
        scan_python_dependency_snapshot(
            tmp_path,
            lockfiles=["pylock.foo.bar.toml"],
        )
