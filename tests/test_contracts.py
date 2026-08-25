"""Tests for AgentCompat contract loading and schema validation."""

from pathlib import Path

import pytest
import yaml

from agentcompat.contracts import ContractValidationError, load_contract
from agentcompat.models import AgentContract


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
    assert contract.tasks == ["add-pagination"]


def test_max_changed_files_is_optional(tmp_path: Path) -> None:
    data = valid_contract_data()
    rules = data["rules"]
    assert isinstance(rules, dict)
    rules.pop("max_changed_files")

    contract = load_contract(write_contract(tmp_path, data))

    assert contract.rules.max_changed_files is None


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
