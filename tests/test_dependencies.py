"""Tests for deterministic project dependency observations."""

from pathlib import Path

import pytest

import agentcompat.dependencies as dependency_module
from agentcompat.dependencies import DependencyScanError, scan_python_dependencies


def test_scan_python_dependencies_reads_pep621_dependency_sections(
    tmp_path: Path,
) -> None:
    (tmp_path / "pyproject.toml").write_text(
        """
[build-system]
requires = ["Setuptools>=69", "wheel"]

[project]
name = "example"
version = "0.1.0"
dependencies = [
    "Requests[socks]>=2",
    "My_Package @ https://example.invalid/package.whl",
]

[project.optional-dependencies]
test = ["PyTest>=8"]
""".strip(),
        encoding="utf-8",
    )

    dependencies = scan_python_dependencies(tmp_path)

    assert dependencies == frozenset(
        {"my-package", "pytest", "requests", "setuptools", "wheel"}
    )


def test_scan_python_dependencies_returns_empty_without_manifest(
    tmp_path: Path,
) -> None:
    assert scan_python_dependencies(tmp_path) == frozenset()


def test_scan_python_dependencies_rejects_dynamic_dependency_fields(
    tmp_path: Path,
) -> None:
    (tmp_path / "pyproject.toml").write_text(
        """
[project]
name = "example"
version = "0.1.0"
dynamic = ["dependencies"]
""".strip(),
        encoding="utf-8",
    )

    with pytest.raises(DependencyScanError, match="dynamic dependency fields"):
        scan_python_dependencies(tmp_path)


def test_scan_python_dependencies_rejects_invalid_toml(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text(
        "dependencies = [unterminated\n",
        encoding="utf-8",
    )

    with pytest.raises(DependencyScanError, match="invalid pyproject.toml"):
        scan_python_dependencies(tmp_path)


def test_scan_python_dependencies_rejects_invalid_utf8(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_bytes(b'[project]\nname = "\xff"\n')

    with pytest.raises(DependencyScanError, match="invalid UTF-8"):
        scan_python_dependencies(tmp_path)


def test_scan_python_dependencies_rejects_manifest_symlink(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    outside_manifest = tmp_path / "outside.toml"
    outside_manifest.write_text(
        '[project]\nname = "outside"\nversion = "0.1.0"\n',
        encoding="utf-8",
    )
    (workspace / "pyproject.toml").symlink_to(outside_manifest)

    with pytest.raises(DependencyScanError, match="must be a regular file"):
        scan_python_dependencies(workspace)


def test_scan_python_dependencies_reads_root_requirements_files(
    tmp_path: Path,
) -> None:
    (tmp_path / "requirements.txt").write_text(
        """
# Repository package sources do not describe dependencies themselves.
--index-url https://packages.example.invalid/simple
--extra-index-url=https://mirror.example.invalid/simple
--require-hashes

Requests[socks]>=2 ; python_version >= "3.12"  # runtime client
My_Package @ https://packages.example.invalid/my-package.whl
wheel==0.45 \\
    --hash=sha256:deadbeef
git+https://example.invalid/legacy.git#egg=Legacy_Dep
-e git+ssh://example.invalid/editable.git#egg=Editable_Dep
-e .
""".strip(),
        encoding="utf-8",
    )

    dependencies = scan_python_dependencies(tmp_path)

    assert dependencies == frozenset(
        {
            "editable-dep",
            "legacy-dep",
            "my-package",
            "requests",
            "wheel",
        }
    )


def test_scan_python_dependencies_reads_nested_and_recursive_requirements(
    tmp_path: Path,
) -> None:
    requirements_directory = tmp_path / "requirements"
    requirements_directory.mkdir()
    (tmp_path / "requirements-dev.txt").write_text(
        "Dev.Tool>=1\n-r requirements/base.txt\n",
        encoding="utf-8",
    )
    (requirements_directory / "base.txt").write_text(
        "-r ../shared.in\nNested_Package==2\n",
        encoding="utf-8",
    )
    (tmp_path / "shared.in").write_text(
        "Shared-Package>=3\n",
        encoding="utf-8",
    )

    dependencies = scan_python_dependencies(tmp_path)

    assert dependencies == frozenset({"dev-tool", "nested-package", "shared-package"})


def test_scan_python_dependencies_does_not_continue_full_line_comments(
    tmp_path: Path,
) -> None:
    (tmp_path / "requirements.txt").write_text(
        "# This comment ends in a continuation marker \\\nunsafe_package>=1\n",
        encoding="utf-8",
    )

    assert scan_python_dependencies(tmp_path) == frozenset({"unsafe-package"})


def test_scan_python_dependencies_comment_terminates_pending_continuation(
    tmp_path: Path,
) -> None:
    (tmp_path / "requirements.txt").write_text(
        "allowed-package \\\n# full-line comment \\\nunsafe-package\n",
        encoding="utf-8",
    )

    assert scan_python_dependencies(tmp_path) == frozenset(
        {"allowed-package", "unsafe-package"}
    )


def test_scan_python_dependencies_prefers_direct_reference_declared_name(
    tmp_path: Path,
) -> None:
    (tmp_path / "requirements.txt").write_text(
        "Declared_Name @ git+https://example.invalid/repo.git#egg=misleading\n",
        encoding="utf-8",
    )

    assert scan_python_dependencies(tmp_path) == frozenset({"declared-name"})


def test_scan_python_dependencies_scans_requirements_nested_in_constraints(
    tmp_path: Path,
) -> None:
    requirements_directory = tmp_path / "requirements"
    requirements_directory.mkdir()
    (tmp_path / "requirements.txt").write_text(
        "-c requirements/constraints.txt\n",
        encoding="utf-8",
    )
    (requirements_directory / "constraints.txt").write_text(
        "Pinned_Only==1\n-r ../hidden.in\n",
        encoding="utf-8",
    )
    (tmp_path / "hidden.in").write_text(
        "Unsafe_Package>=1\n",
        encoding="utf-8",
    )

    dependencies = scan_python_dependencies(tmp_path)

    assert dependencies == frozenset({"unsafe-package"})


def test_scan_python_dependencies_rejects_competing_attached_directives(
    tmp_path: Path,
) -> None:
    (tmp_path / "requirements.txt").write_text(
        "--constraint=constraints.in --requirement hidden.in\n",
        encoding="utf-8",
    )
    (tmp_path / "constraints.in").write_text("safe-package\n", encoding="utf-8")
    (tmp_path / "hidden.in").write_text("unsafe-package\n", encoding="utf-8")

    with pytest.raises(DependencyScanError, match="requires exactly one path"):
        scan_python_dependencies(tmp_path)


def test_scan_python_dependencies_rejects_recursive_requirement_include(
    tmp_path: Path,
) -> None:
    (tmp_path / "requirements.txt").write_text(
        "-r included.in\n",
        encoding="utf-8",
    )
    (tmp_path / "included.in").write_text(
        "-r requirements.txt\n",
        encoding="utf-8",
    )

    with pytest.raises(DependencyScanError, match="recursively references itself"):
        scan_python_dependencies(tmp_path)


def test_scan_python_dependencies_rejects_requirement_include_escape(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (tmp_path / "outside.in").write_text("unsafe-package\n", encoding="utf-8")
    (workspace / "requirements.txt").write_text(
        "-r ../outside.in\n",
        encoding="utf-8",
    )

    with pytest.raises(DependencyScanError, match="escapes workspace"):
        scan_python_dependencies(workspace)


def test_scan_python_dependencies_rejects_symlink_loop_in_include_path(
    tmp_path: Path,
) -> None:
    (tmp_path / "loop").symlink_to("loop", target_is_directory=True)
    (tmp_path / "requirements.txt").write_text(
        "-r loop/included.in\n",
        encoding="utf-8",
    )

    with pytest.raises(DependencyScanError, match="could not resolve"):
        scan_python_dependencies(tmp_path)


def test_scan_python_dependencies_rejects_nested_requirements_symlink(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    requirements_directory = workspace / "requirements"
    requirements_directory.mkdir(parents=True)
    outside_directory = tmp_path / "outside"
    outside_directory.mkdir()
    (outside_directory / "unsafe.txt").write_text(
        "unsafe-package\n",
        encoding="utf-8",
    )
    (requirements_directory / "linked").symlink_to(
        outside_directory,
        target_is_directory=True,
    )
    (workspace / "requirements.txt").write_text(
        "-r requirements/linked/unsafe.txt\n",
        encoding="utf-8",
    )

    with pytest.raises(DependencyScanError, match="escapes workspace"):
        scan_python_dependencies(workspace)


def test_scan_python_dependencies_rejects_missing_requirement_include(
    tmp_path: Path,
) -> None:
    (tmp_path / "requirements.txt").write_text(
        "-r missing.in\n",
        encoding="utf-8",
    )

    with pytest.raises(DependencyScanError, match="does not exist"):
        scan_python_dependencies(tmp_path)


def test_scan_python_dependencies_rejects_missing_constraint_include(
    tmp_path: Path,
) -> None:
    (tmp_path / "requirements.txt").write_text(
        "-c missing-constraints.in\n",
        encoding="utf-8",
    )

    with pytest.raises(DependencyScanError, match="does not exist"):
        scan_python_dependencies(tmp_path)


def test_scan_python_dependencies_rejects_invalid_requirements_utf8(
    tmp_path: Path,
) -> None:
    (tmp_path / "requirements.txt").write_bytes(b"requests\n\xff\n")

    with pytest.raises(DependencyScanError, match="invalid UTF-8"):
        scan_python_dependencies(tmp_path)


@pytest.mark.parametrize(
    "line",
    [
        "--unknown-option value",
        "--index-url",
        "--index-url https://example.invalid unexpected",
        "--no-index unexpected",
        "-e ../local-package",
    ],
)
def test_scan_python_dependencies_rejects_ambiguous_requirement_lines(
    tmp_path: Path,
    line: str,
) -> None:
    (tmp_path / "requirements.txt").write_text(
        f"{line}\n",
        encoding="utf-8",
    )

    with pytest.raises(DependencyScanError):
        scan_python_dependencies(tmp_path)


def test_scan_python_dependencies_limits_recursive_requirements_files(
    tmp_path: Path,
) -> None:
    (tmp_path / "requirements.txt").write_text(
        "-r include-0.in\n",
        encoding="utf-8",
    )
    for index in range(100):
        next_line = f"-r include-{index + 1}.in\n" if index < 99 else "requests\n"
        (tmp_path / f"include-{index}.in").write_text(
            next_line,
            encoding="utf-8",
        )

    with pytest.raises(DependencyScanError, match="exceeds 100 files"):
        scan_python_dependencies(tmp_path)


def test_scan_python_dependencies_reads_repeated_include_once(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (tmp_path / "requirements.txt").write_text(
        "-c shared.in\n-r shared.in\n" + ("-r shared.in\n" * 1_000),
        encoding="utf-8",
    )
    shared_path = tmp_path / "shared.in"
    shared_path.write_text("Shared_Package>=1\n", encoding="utf-8")
    original_read_bytes = Path.read_bytes
    shared_reads = 0

    def counting_read_bytes(path: Path) -> bytes:
        nonlocal shared_reads
        if path == shared_path:
            shared_reads += 1
        return original_read_bytes(path)

    monkeypatch.setattr(Path, "read_bytes", counting_read_bytes)

    assert scan_python_dependencies(tmp_path) == frozenset({"shared-package"})
    assert shared_reads == 1


def test_scan_python_dependencies_bounds_flattened_policy_trace(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (tmp_path / "requirements.txt").write_text(
        "-r shared.in\n-r shared.in\npackage\n",
        encoding="utf-8",
    )
    (tmp_path / "shared.in").write_text(
        "--index-url https://one.example/simple\n"
        "--extra-index-url https://two.example/simple\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(dependency_module, "_MAX_REQUIREMENT_POLICY_EVENTS", 3)

    with pytest.raises(DependencyScanError, match="trace exceeds 3 events"):
        scan_python_dependencies(tmp_path)


def test_scan_python_dependencies_limits_total_requirements_bytes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (tmp_path / "requirements.txt").write_text(
        "-r shared.in\n",
        encoding="utf-8",
    )
    (tmp_path / "shared.in").write_text("package\n", encoding="utf-8")
    monkeypatch.setattr(
        dependency_module,
        "_MAX_REQUIREMENTS_TOTAL_BYTES",
        20,
    )

    with pytest.raises(DependencyScanError, match="20-byte total limit"):
        scan_python_dependencies(tmp_path)


def test_scan_python_dependencies_limits_logical_lines(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (tmp_path / "requirements.txt").write_text(
        "first\nsecond\nthird\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(dependency_module, "_MAX_REQUIREMENT_LINES", 2)

    with pytest.raises(DependencyScanError, match="exceeds 2 lines"):
        scan_python_dependencies(tmp_path)


def test_scan_python_dependencies_limits_distinct_names(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (tmp_path / "requirements.txt").write_text(
        "first\nsecond\nthird\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(dependency_module, "_MAX_DEPENDENCY_NAMES", 2)

    with pytest.raises(DependencyScanError, match="exceeds 2 distinct names"):
        scan_python_dependencies(tmp_path)


@pytest.mark.parametrize(
    "requirement",
    ["requests nonsense", "requests$garbage", "invalid-name-"],
)
def test_scan_python_dependencies_rejects_invalid_requirement_tails(
    tmp_path: Path,
    requirement: str,
) -> None:
    (tmp_path / "pyproject.toml").write_text(
        f"""
[project]
name = "example"
version = "0.1.0"
dependencies = [{requirement!r}]
""".strip(),
        encoding="utf-8",
    )

    with pytest.raises(DependencyScanError, match="invalid PEP 508 requirement"):
        scan_python_dependencies(tmp_path)
