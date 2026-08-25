"""Tests for AgentCompat contract loading and schema validation."""

from pathlib import Path

import pytest
import yaml

from agentcompat.contracts import (
    ContractValidationError,
    load_contract,
    merge_task_rules,
)
from agentcompat.models import (
    AgentContract,
    ChangedFileRules,
    ContractRules,
    DependencyDriftRules,
    DependencyManifestRule,
    TaskChangedFileRules,
    TaskDependencyDriftRules,
    TaskRules,
)


def valid_contract_data() -> dict[str, object]:
    """Return a fresh complete contract mapping."""
    return {
        "version": 1,
        "project": {"name": "example-project"},
        "baseline": {"agent": "codex"},
        "candidates": ["gemini"],
        "rules": {
            "tests_must_pass": True,
            "build_must_pass": True,
            "forbidden_paths": ["secrets/"],
            "required_paths": ["src/"],
            "max_changed_files": 10,
            "forbidden_dependencies": ["unsafe-package"],
        },
        "tasks": ["add-pagination"],
    }


def write_contract(tmp_path: Path, data: object) -> Path:
    """Serialize contract test data to a temporary YAML file."""
    path = tmp_path / "contract.yaml"
    path.write_text(yaml.safe_dump(data), encoding="utf-8")
    return path


def test_load_contract_returns_agent_contract(tmp_path: Path) -> None:
    contract = load_contract(write_contract(tmp_path, valid_contract_data()))

    assert isinstance(contract, AgentContract)
    assert contract.version == 1
    assert contract.project.name == "example-project"
    assert contract.baseline.agent == "codex"
    assert contract.candidates == ["gemini"]
    assert contract.rules.tests_must_pass is True
    assert contract.rules.build_must_pass is True
    assert contract.rules.forbidden_paths == ["secrets/"]
    assert contract.rules.required_paths == ["src/"]
    assert contract.rules.max_changed_files == 10
    assert contract.rules.forbidden_dependencies == ["unsafe-package"]
    assert contract.rules.dependency_manifests == []
    assert contract.rules.dependency_drift == DependencyDriftRules()
    assert contract.tasks == ["add-pagination"]


def test_max_changed_files_is_optional(tmp_path: Path) -> None:
    data = valid_contract_data()
    rules = data["rules"]
    assert isinstance(rules, dict)
    rules.pop("max_changed_files")

    contract = load_contract(write_contract(tmp_path, data))

    assert contract.rules.max_changed_files is None
    assert contract.rules.changed_files == ChangedFileRules()


def test_load_contract_parses_nested_changed_file_rules(tmp_path: Path) -> None:
    data = valid_contract_data()
    rules = data["rules"]
    assert isinstance(rules, dict)
    rules.pop("max_changed_files")
    rules["changed_files"] = {
        "max": 8,
        "include_ignored": True,
        "exclude": [".pytest_cache/**", "**/__pycache__/**", "**/*.pyc"],
    }

    contract = load_contract(write_contract(tmp_path, data))

    assert contract.rules.changed_files == ChangedFileRules(
        max=8,
        include_ignored=True,
        exclude=[".pytest_cache/**", "**/__pycache__/**", "**/*.pyc"],
    )
    assert contract.rules.max_changed_files == 8


def test_legacy_max_changed_files_populates_nested_policy(tmp_path: Path) -> None:
    contract = load_contract(write_contract(tmp_path, valid_contract_data()))

    assert contract.rules.max_changed_files == 10
    assert contract.rules.changed_files.max == 10
    assert contract.rules.changed_files.include_ignored is False
    assert contract.rules.changed_files.exclude == []


def test_matching_legacy_and_nested_limits_are_accepted(tmp_path: Path) -> None:
    data = valid_contract_data()
    rules = data["rules"]
    assert isinstance(rules, dict)
    rules["changed_files"] = {"max": 10}

    contract = load_contract(write_contract(tmp_path, data))

    assert contract.rules.max_changed_files == 10
    assert contract.rules.changed_files.max == 10


def test_conflicting_legacy_and_nested_limits_are_rejected(tmp_path: Path) -> None:
    data = valid_contract_data()
    rules = data["rules"]
    assert isinstance(rules, dict)
    rules["changed_files"] = {"max": 8}

    with pytest.raises(
        ContractValidationError,
        match=r"max_changed_files and changed_files\.max must be equal",
    ):
        load_contract(write_contract(tmp_path, data))


def test_changed_file_rules_reject_unknown_fields(tmp_path: Path) -> None:
    data = valid_contract_data()
    rules = data["rules"]
    assert isinstance(rules, dict)
    rules.pop("max_changed_files")
    rules["changed_files"] = {"max": 8, "ignore_cache": True}

    with pytest.raises(
        ContractValidationError,
        match=r"rules\.changed_files\.ignore_cache: Extra inputs",
    ):
        load_contract(write_contract(tmp_path, data))


def test_changed_file_rule_values_remain_strict(tmp_path: Path) -> None:
    data = valid_contract_data()
    rules = data["rules"]
    assert isinstance(rules, dict)
    rules.pop("max_changed_files")
    rules["changed_files"] = {"include_ignored": "false"}

    with pytest.raises(
        ContractValidationError,
        match=r"rules\.changed_files\.include_ignored: Input should be a valid boolean",
    ):
        load_contract(write_contract(tmp_path, data))


def test_task_changed_file_rule_defaults_are_unspecified() -> None:
    rules = TaskRules(changed_files=TaskChangedFileRules())

    assert rules.max_changed_files is None
    assert rules.changed_files.max is None
    assert rules.changed_files.include_ignored is None
    assert rules.changed_files.exclude is None


def test_task_legacy_limit_populates_nested_policy() -> None:
    rules = TaskRules(max_changed_files=4)

    assert rules.max_changed_files == 4
    assert rules.changed_files.max == 4


def test_task_conflicting_legacy_and_nested_limits_are_rejected() -> None:
    with pytest.raises(
        ValueError,
        match=r"max_changed_files and changed_files\.max must be equal",
    ):
        TaskRules(
            max_changed_files=4,
            changed_files=TaskChangedFileRules(max=3),
        )


def test_changed_file_policy_merge_tightens_limit_and_ignored_counting() -> None:
    contract_rules = ContractRules(
        tests_must_pass=True,
        build_must_pass=True,
        forbidden_paths=[],
        required_paths=[],
        changed_files=ChangedFileRules(
            max=10,
            include_ignored=False,
            exclude=[".pytest_cache/**", "**/__pycache__/**", "**/*.pyc"],
        ),
        forbidden_dependencies=[],
    )
    task_rules = TaskRules(
        changed_files=TaskChangedFileRules(
            max=6,
            include_ignored=True,
            exclude=["**/__pycache__/**", "**/*.pyc"],
        )
    )

    merged = merge_task_rules(contract_rules, task_rules)

    assert merged.max_changed_files == 6
    assert merged.changed_files == ChangedFileRules(
        max=6,
        include_ignored=True,
        exclude=["**/__pycache__/**", "**/*.pyc"],
    )


def test_task_changed_file_exclusions_inherit_when_unspecified() -> None:
    contract_rules = ContractRules(
        tests_must_pass=True,
        build_must_pass=True,
        forbidden_paths=[],
        required_paths=[],
        changed_files=ChangedFileRules(
            max=8,
            exclude=[".pytest_cache/**", "**/*.pyc"],
        ),
        forbidden_dependencies=[],
    )

    merged = merge_task_rules(contract_rules, TaskRules())

    assert merged.changed_files.exclude == [".pytest_cache/**", "**/*.pyc"]


def test_task_cannot_add_changed_file_exclusions() -> None:
    contract_rules = ContractRules(
        tests_must_pass=True,
        build_must_pass=True,
        forbidden_paths=[],
        required_paths=[],
        changed_files=ChangedFileRules(exclude=[".pytest_cache/**"]),
        forbidden_dependencies=[],
    )
    task_rules = TaskRules(
        changed_files=TaskChangedFileRules(
            exclude=[".pytest_cache/**", "generated/**"]
        )
    )

    with pytest.raises(ValueError, match="cannot add exclusions"):
        merge_task_rules(contract_rules, task_rules)


def test_forbidden_dependencies_must_be_bare_package_names(tmp_path: Path) -> None:
    data = valid_contract_data()
    rules = data["rules"]
    assert isinstance(rules, dict)
    rules["forbidden_dependencies"] = ["requests>=2"]

    with pytest.raises(
        ContractValidationError,
        match=r"rules\.forbidden_dependencies\.0:",
    ):
        load_contract(write_contract(tmp_path, data))


def test_task_rules_tighten_without_weakening_contract_rules() -> None:
    contract_rules = ContractRules(
        tests_must_pass=False,
        build_must_pass=True,
        forbidden_paths=["contract/**"],
        required_paths=["src/**"],
        max_changed_files=10,
        forbidden_dependencies=["unsafe-package"],
        dependency_manifests=[
            DependencyManifestRule(path="requirements.txt", role="requirement")
        ],
        dependency_drift=DependencyDriftRules(
            versions_must_not_change=True,
            lockfiles=["pylock.toml"],
        ),
    )
    task_rules = TaskRules(
        tests_must_pass=True,
        build_must_pass=False,
        forbidden_paths=["task/**", "contract/**"],
        required_paths=["tests/**"],
        max_changed_files=5,
        forbidden_dependencies=["task-package", "unsafe-package"],
        dependency_manifests=[
            DependencyManifestRule(
                path="constraints/pins.txt",
                role="constraint",
            )
        ],
        dependency_drift=TaskDependencyDriftRules(
            removals_forbidden=True,
            versions_must_not_change=False,
            sources_must_not_change=True,
            lockfiles=["poetry.lock"],
        ),
    )

    merged = merge_task_rules(contract_rules, task_rules)

    assert merged.tests_must_pass is True
    assert merged.build_must_pass is True
    assert merged.forbidden_paths == ["contract/**", "task/**"]
    assert merged.required_paths == ["src/**", "tests/**"]
    assert merged.max_changed_files == 5
    assert merged.forbidden_dependencies == ["unsafe-package", "task-package"]
    assert [(item.path, item.role) for item in merged.dependency_manifests] == [
        ("requirements.txt", "requirement"),
        ("constraints/pins.txt", "constraint"),
    ]
    assert merged.dependency_drift.removals_forbidden is True
    assert merged.dependency_drift.versions_must_not_change is True
    assert merged.dependency_drift.sources_must_not_change is True
    assert merged.dependency_drift.lockfiles == ["pylock.toml", "poetry.lock"]


def test_load_contract_parses_dependency_manifest_and_drift_rules(
    tmp_path: Path,
) -> None:
    data = valid_contract_data()
    rules = data["rules"]
    assert isinstance(rules, dict)
    rules["dependency_manifests"] = [
        {"path": "requirements/base.in", "role": "requirement"},
        {"path": "constraints/pins.txt", "role": "constraint"},
    ]
    rules["dependency_drift"] = {
        "removals_forbidden": True,
        "versions_must_not_change": True,
        "sources_must_not_change": True,
        "lockfiles_must_not_change": True,
        "lockfiles_must_be_updated_for_dependency_changes": True,
        "lockfiles": ["pylock.toml"],
    }

    contract = load_contract(write_contract(tmp_path, data))

    assert contract.rules.dependency_manifests[1].role == "constraint"
    assert contract.rules.dependency_drift.removals_forbidden is True
    assert contract.rules.dependency_drift.lockfiles == ["pylock.toml"]


@pytest.mark.parametrize(
    "path",
    [
        "/absolute.lock",
        "../outside.lock",
        "nested/../lock",
        "lock*.toml",
        "a\\b",
        "bad\x00.lock",
    ],
)
def test_dependency_files_must_be_normalized_repository_paths(
    tmp_path: Path,
    path: str,
) -> None:
    data = valid_contract_data()
    rules = data["rules"]
    assert isinstance(rules, dict)
    rules["dependency_drift"] = {"lockfiles": [path]}

    with pytest.raises(ContractValidationError, match="repository-relative"):
        load_contract(write_contract(tmp_path, data))


def test_lockfile_update_rule_requires_explicit_lockfiles(tmp_path: Path) -> None:
    data = valid_contract_data()
    rules = data["rules"]
    assert isinstance(rules, dict)
    rules["dependency_drift"] = {
        "lockfiles_must_be_updated_for_dependency_changes": True
    }

    with pytest.raises(ContractValidationError, match="lockfiles must be configured"):
        load_contract(write_contract(tmp_path, data))


def test_dependency_manifest_cannot_have_conflicting_roles(tmp_path: Path) -> None:
    data = valid_contract_data()
    rules = data["rules"]
    assert isinstance(rules, dict)
    rules["dependency_manifests"] = [
        {"path": "shared.in", "role": "requirement"},
        {"path": "shared.in", "role": "constraint"},
    ]

    with pytest.raises(ContractValidationError, match="conflicting roles"):
        load_contract(write_contract(tmp_path, data))


def test_baseline_agent_is_required(tmp_path: Path) -> None:
    data = valid_contract_data()
    data["baseline"] = {}

    with pytest.raises(
        ContractValidationError,
        match=r"baseline\.agent: Field required",
    ):
        load_contract(write_contract(tmp_path, data))


def test_project_name_cannot_be_empty(tmp_path: Path) -> None:
    data = valid_contract_data()
    data["project"] = {"name": "  "}

    with pytest.raises(ContractValidationError, match=r"project\.name:"):
        load_contract(write_contract(tmp_path, data))


def test_at_least_one_candidate_is_required(tmp_path: Path) -> None:
    data = valid_contract_data()
    data["candidates"] = []

    with pytest.raises(ContractValidationError, match="candidates:"):
        load_contract(write_contract(tmp_path, data))


@pytest.mark.parametrize("max_changed_files", [0, -1])
def test_max_changed_files_must_be_positive(
    tmp_path: Path,
    max_changed_files: int,
) -> None:
    data = valid_contract_data()
    rules = data["rules"]
    assert isinstance(rules, dict)
    rules["max_changed_files"] = max_changed_files

    with pytest.raises(
        ContractValidationError,
        match=r"rules\.max_changed_files: Input should be greater than 0",
    ):
        load_contract(write_contract(tmp_path, data))


def test_tasks_cannot_be_empty(tmp_path: Path) -> None:
    data = valid_contract_data()
    data["tasks"] = []

    with pytest.raises(ContractValidationError, match="tasks:"):
        load_contract(write_contract(tmp_path, data))


def test_version_must_be_an_integer(tmp_path: Path) -> None:
    data = valid_contract_data()
    data["version"] = "1"

    with pytest.raises(ContractValidationError, match="version: Input should be"):
        load_contract(write_contract(tmp_path, data))


def test_required_rule_fields_cannot_be_omitted(tmp_path: Path) -> None:
    data = valid_contract_data()
    rules = data["rules"]
    assert isinstance(rules, dict)
    rules.pop("tests_must_pass")

    with pytest.raises(
        ContractValidationError,
        match=r"rules\.tests_must_pass: Field required",
    ):
        load_contract(write_contract(tmp_path, data))


def test_unknown_contract_fields_are_rejected(tmp_path: Path) -> None:
    data = valid_contract_data()
    data["unexpected"] = True

    with pytest.raises(ContractValidationError, match="unexpected: Extra inputs"):
        load_contract(write_contract(tmp_path, data))


def test_invalid_yaml_is_rejected(tmp_path: Path) -> None:
    path = tmp_path / "contract.yaml"
    path.write_text("rules: [unterminated\n", encoding="utf-8")

    with pytest.raises(ContractValidationError, match="invalid YAML"):
        load_contract(path)
