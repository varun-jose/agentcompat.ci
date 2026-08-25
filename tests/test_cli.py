"""Tests for the AgentCompat CI command-line interface."""

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

import agentcompat.cli as cli_module
from agentcompat import __version__
from agentcompat.adapters import KiroAdapter
from agentcompat.cli import app
from agentcompat.evaluators import build_compatibility_verdict
from agentcompat.models import (
    AgentContract,
    AgentExecutionResult,
    AgentRunResult,
    CompatibilityResult,
    EvaluationResult,
    TaskDefinition,
    WorkspaceChanges,
)

runner = CliRunner()

VALID_CONTRACT_YAML = """\
version: 1
project:
  name: example-project
baseline:
  agent: codex
candidates:
  - gemini
rules:
  tests_must_pass: true
  build_must_pass: true
  forbidden_paths: []
  required_paths:
    - src/
  max_changed_files: 10
  forbidden_dependencies: []
tasks:
  - add-pagination
"""


def test_cli_starts_and_displays_help() -> None:
    result = runner.invoke(app, ["--help"])

    assert result.exit_code == 0
    assert "validate" in result.output
    assert "run" in result.output


def test_cli_reports_version() -> None:
    result = runner.invoke(app, ["--version"])

    assert result.exit_code == 0
    assert result.output == f"agentcompat {__version__}\n"


def test_cli_supports_kiro_adapter() -> None:
    adapter = cli_module._create_adapter("kiro")

    assert isinstance(adapter, KiroAdapter)


def test_validate_displays_contract_summary(tmp_path: Path) -> None:
    contract_path = tmp_path / "contract.yaml"
    contract_path.write_text(VALID_CONTRACT_YAML, encoding="utf-8")

    result = runner.invoke(app, ["validate", str(contract_path)])

    assert result.exit_code == 0
    assert "Contract is valid" in result.output
    assert "example-project" in result.output
    assert "codex" in result.output
    assert "gemini" in result.output
    assert "Tests must pass" in result.output
    assert "Count Git-ignored files" in result.output
    assert "Changed-file exclusions" in result.output


def test_validate_reports_schema_errors(tmp_path: Path) -> None:
    contract_path = tmp_path / "contract.yaml"
    contract_path.write_text(
        VALID_CONTRACT_YAML.replace("candidates:\n  - gemini", "candidates: []"),
        encoding="utf-8",
    )

    result = runner.invoke(app, ["validate", str(contract_path)])

    assert result.exit_code == 1
    assert "Invalid contract" in result.output
    assert "candidates" in result.output


def execution_result(
    agent_name: str,
    exit_code: int = 0,
    stderr: str = "",
) -> AgentExecutionResult:
    """Build a deterministic execution result for CLI report tests."""
    now = datetime(2026, 1, 1, tzinfo=UTC)
    return AgentExecutionResult(
        agent_name=agent_name,
        started_at=now,
        completed_at=now,
        exit_code=exit_code,
        stdout=f"{agent_name} output",
        stderr=stderr,
        metadata={
            "tests_passed": True,
            "build_passed": True,
            "added_dependencies": [],
        },
    )


def evaluation(
    name: str,
    *,
    passed: bool = True,
    evidence: list[str] | None = None,
    metadata: dict[str, object] | None = None,
    summary: str | None = None,
) -> EvaluationResult:
    """Build one blocking finding for CLI report tests."""
    return EvaluationResult(
        name=name,
        passed=passed,
        blocking=True,
        summary=summary or f"{name} {'passed' if passed else 'failed'}.",
        evidence=evidence or [],
        metadata=metadata or {},
    )


def compatibility_result(
    *,
    forbidden_path_failure: bool = False,
    baseline_error: str | None = None,
    candidate_exit_code: int = 0,
    candidate_stderr: str = "",
) -> CompatibilityResult:
    """Build a complete runner result without executing real agents."""
    contract = AgentContract.model_validate(
        {
            "version": 1,
            "project": {"name": "example-project"},
            "baseline": {"agent": "codex"},
            "candidates": ["gemini"],
            "rules": {
                "tests_must_pass": True,
                "build_must_pass": True,
                "forbidden_paths": ["secrets/**"],
                "required_paths": ["src/**"],
                "max_changed_files": 5,
                "forbidden_dependencies": ["unsafe-package"],
            },
            "tasks": ["add-pagination"],
        }
    )
    baseline_execution = (
        None if baseline_error is not None else execution_result("codex")
    )
    baseline = AgentRunResult(
        agent_name="codex",
        workspace_path=Path("/tmp/baseline"),
        execution=baseline_execution,
        error=baseline_error,
        diff=WorkspaceChanges(added_files=["baseline-note.txt"]),
    )
    candidate = AgentRunResult(
        agent_name="gemini",
        workspace_path=Path("/tmp/candidate"),
        execution=execution_result(
            "gemini",
            exit_code=candidate_exit_code,
            stderr=candidate_stderr,
        ),
        diff=WorkspaceChanges(
            changed_files=["src/app.py"],
            ignored_files=[".pytest_cache/README.md"],
        ),
    )
    findings = [
        evaluation(
            "baseline_execution",
            passed=baseline_error is None,
            evidence=(
                [baseline_error] if baseline_error is not None else ["exit_code=0"]
            ),
            summary=(
                f"Baseline agent failed to execute: {baseline_error}"
                if baseline_error is not None
                else "Baseline agent executed successfully."
            ),
        ),
        evaluation(
            "candidate_execution",
            passed=candidate_exit_code == 0,
            evidence=[f"exit_code={candidate_exit_code}"],
            summary=(
                "Candidate agent executed successfully."
                if candidate_exit_code == 0
                else f"Candidate agent exited with code {candidate_exit_code}."
            ),
        ),
        evaluation(
            "tests_must_pass",
            evidence=["passed=True"],
            metadata={"required": True, "observed_passed": True},
        ),
        evaluation(
            "build_must_pass",
            evidence=["passed=True"],
            metadata={"required": True, "observed_passed": True},
        ),
        evaluation(
            "forbidden_paths",
            passed=not forbidden_path_failure,
            evidence=["secrets/token.txt"] if forbidden_path_failure else [],
            summary=(
                "Found 1 forbidden path change."
                if forbidden_path_failure
                else "No forbidden path changes."
            ),
        ),
        evaluation(
            "required_paths",
            evidence=["src/** -> src/app.py"],
            metadata={"missing_patterns": []},
        ),
        evaluation("max_changed_files", evidence=["src/app.py"]),
        evaluation("forbidden_dependencies"),
    ]
    return CompatibilityResult(
        contract=contract,
        task=TaskDefinition.model_validate(
            {"name": "add-pagination", "prompt": "Add pagination."}
        ),
        original_commit_sha="a" * 40,
        baseline=baseline,
        candidate=candidate,
        verdict=build_compatibility_verdict(findings),
    )


def run_arguments(tmp_path: Path, *, baseline: str = "codex") -> list[str]:
    """Create paths and return the required run command arguments."""
    repository = tmp_path / "repository"
    repository.mkdir()
    contract = tmp_path / "contract.yaml"
    contract.write_text(VALID_CONTRACT_YAML, encoding="utf-8")
    task = tmp_path / "task.yaml"
    task.write_text("prompt: Add pagination.\n", encoding="utf-8")
    return [
        "run",
        "--repo",
        str(repository),
        "--baseline",
        baseline,
        "--candidate",
        "gemini",
        "--contract",
        str(contract),
        "--task",
        str(task),
    ]


def install_run_result(
    monkeypatch: pytest.MonkeyPatch,
    report: CompatibilityResult,
) -> dict[str, Any]:
    """Replace compatibility execution and capture its keyword arguments."""
    captured: dict[str, Any] = {}

    async def fake_run_compatibility(**kwargs: Any) -> CompatibilityResult:
        captured.update(kwargs)
        return report

    monkeypatch.setattr(cli_module, "run_compatibility", fake_run_compatibility)
    return captured


def test_run_displays_passing_rich_report_and_writes_json(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    report = compatibility_result()
    captured = install_run_result(monkeypatch, report)
    json_path = tmp_path / "results.json"

    result = runner.invoke(
        app,
        [*run_arguments(tmp_path), "--json-output", str(json_path)],
    )

    assert result.exit_code == 0
    assert "AgentCompat CI compatibility report" in result.output
    assert "Agent runs" in result.output
    assert "Deterministic compatibility checks" in result.output
    assert "Final verdict" in result.output
    assert "Baseline" in result.output
    assert "codex" in result.output
    assert "Candidate" in result.output
    assert "gemini" in result.output
    assert "add-pagination" in result.output
    assert "Baseline execution" in result.output
    assert "Candidate execution" in result.output
    assert "Build status" in result.output
    assert "Test status" in result.output
    assert "Candidate changed files" in result.output
    assert "src/app.py" in result.output
    assert "ignored: .pytest_cache/README.md" in result.output
    assert "Forbidden path violations" in result.output
    assert "Required path compliance" in result.output
    assert "Compatibility percentage" in result.output
    assert "100.0%" in result.output
    assert "Critical failures" in result.output
    assert "✓ Final result: PASS" in result.output
    assert captured["baseline_name"] == "codex"
    assert captured["candidate_name"] == "gemini"
    assert callable(captured["progress_callback"])

    payload = json.loads(json_path.read_text(encoding="utf-8"))
    assert payload["baseline"]["agent_name"] == "codex"
    assert payload["candidate"]["agent_name"] == "gemini"
    assert payload["candidate"]["diff"]["changed_files"] == ["src/app.py"]
    assert payload["candidate"]["diff"]["added_files"] == []
    assert payload["candidate"]["diff"]["ignored_files"] == [
        ".pytest_cache/README.md"
    ]
    assert payload["compatibility_percentage"] == 100.0
    assert payload["critical_failures"] == []
    assert payload["final_status"] == "PASS"


def test_run_returns_one_and_reports_critical_compatibility_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    install_run_result(
        monkeypatch,
        compatibility_result(forbidden_path_failure=True),
    )

    result = runner.invoke(app, run_arguments(tmp_path))

    assert result.exit_code == 1
    assert "secrets/token.txt" in result.output
    assert "83.3%" in result.output
    assert "forbidden_paths" in result.output
    assert "✗ Final result: FAIL" in result.output


def test_run_returns_two_for_agent_execution_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    install_run_result(
        monkeypatch,
        compatibility_result(baseline_error="RuntimeError: baseline unavailable"),
    )

    result = runner.invoke(app, run_arguments(tmp_path))

    assert result.exit_code == 2
    assert "baseline unavailable" in result.output
    assert "Final result: FAIL" in result.output


def test_run_surfaces_failed_agent_stderr(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    install_run_result(
        monkeypatch,
        compatibility_result(
            candidate_exit_code=41,
            candidate_stderr="Gemini authentication failed.",
        ),
    )

    result = runner.invoke(app, run_arguments(tmp_path))

    assert result.exit_code == 2
    assert "Candidate execution" in result.output
    assert "FAIL (exit 41)" in result.output
    assert "Gemini authentication failed." in result.output


def test_run_returns_two_for_unsupported_agent(tmp_path: Path) -> None:
    result = runner.invoke(app, run_arguments(tmp_path, baseline="unknown"))

    assert result.exit_code == 2
    assert "Run configuration error" in result.output
    assert "unsupported agent: unknown" in result.output


def test_run_returns_two_for_unexpected_runner_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fail_run(**kwargs: Any) -> CompatibilityResult:
        raise RuntimeError("workspace creation failed")

    monkeypatch.setattr(cli_module, "run_compatibility", fail_run)

    result = runner.invoke(app, run_arguments(tmp_path))

    assert result.exit_code == 2
    assert "Run execution error" in result.output
    assert "workspace creation failed" in result.output
