"""Integration tests for compatibility run orchestration."""

import shlex
import sys
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

import pytest
import yaml
from git import Actor, Repo

from agentcompat.adapters.base import AgentAdapter
from agentcompat.models import AgentExecutionResult, CompatibilityResult, TaskDefinition
from agentcompat.runner import CompatibilityRunner, TaskValidationError, load_task

WorkspaceAction = Callable[[Path], None]


@pytest.fixture
def source_repository(tmp_path: Path) -> Path:
    """Create a committed source repository for compatibility runs."""
    source = tmp_path / "source"
    (source / "src").mkdir(parents=True)
    (source / "src" / "app.py").write_text("VALUE = 1\n", encoding="utf-8")
    (source / "README.md").write_text("source\n", encoding="utf-8")
    repository = Repo.init(source)
    repository.index.add(["README.md", "src/app.py"])
    author = Actor("AgentCompat Tests", "tests@agentcompat.local")
    repository.index.commit("Initial commit", author=author, committer=author)
    repository.close()
    return source


def write_contract(tmp_path: Path) -> Path:
    """Write the complete contract used by runner tests."""
    contract = {
        "version": 1,
        "project": {"name": "runner-fixture"},
        "baseline": {"agent": "baseline"},
        "candidates": ["candidate"],
        "rules": {
            "tests_must_pass": True,
            "build_must_pass": True,
            "forbidden_paths": ["secrets/**"],
            "required_paths": ["src/**"],
            "max_changed_files": 2,
            "forbidden_dependencies": ["unsafe-package"],
        },
        "tasks": ["task.yaml"],
    }
    path = tmp_path / "contract.yaml"
    path.write_text(yaml.safe_dump(contract), encoding="utf-8")
    return path


def write_task(tmp_path: Path, prompt: str = "Update the application.") -> Path:
    """Write a task definition with one extensible metadata field."""
    path = tmp_path / "task.yaml"
    path.write_text(
        yaml.safe_dump({"name": "update-app", "prompt": prompt}),
        encoding="utf-8",
    )
    return path


class FakeAdapter(AgentAdapter):
    """In-process adapter that records and modifies its assigned workspace."""

    def __init__(
        self,
        agent_name: str,
        *,
        action: WorkspaceAction | None = None,
        error: Exception | None = None,
        exit_code: int = 0,
    ) -> None:
        self.agent_name = agent_name
        self.action = action
        self.error = error
        self.exit_code = exit_code
        self.prepared_workspaces: list[Path] = []
        self.executed_workspaces: list[Path] = []
        self.tasks: list[TaskDefinition] = []

    async def prepare(self, workspace: Path) -> None:
        self.prepared_workspaces.append(workspace)

    async def execute(
        self,
        task: TaskDefinition,
        workspace: Path,
    ) -> AgentExecutionResult:
        self.executed_workspaces.append(workspace)
        self.tasks.append(task)
        if self.action is not None:
            self.action(workspace)
        if self.error is not None:
            raise self.error
        now = datetime.now(UTC)
        return AgentExecutionResult(
            agent_name=self.agent_name,
            started_at=now,
            completed_at=now,
            exit_code=self.exit_code,
            stdout=f"{self.agent_name} output",
            stderr="",
            metadata={
                "tests_passed": True,
                "build_passed": True,
                "added_dependencies": [],
            },
        )


def add_baseline_file(workspace: Path) -> None:
    """Make a baseline-only untracked change."""
    (workspace / "baseline.txt").write_text("baseline\n", encoding="utf-8")


def update_candidate_source(workspace: Path) -> None:
    """Make a candidate-only tracked source change."""
    (workspace / "src" / "app.py").write_text("VALUE = 2\n", encoding="utf-8")


@pytest.mark.asyncio
async def test_runner_executes_isolated_agents_and_collects_results(
    tmp_path: Path,
    source_repository: Path,
) -> None:
    baseline = FakeAdapter("baseline", action=add_baseline_file)
    candidate = FakeAdapter("candidate", action=update_candidate_source)
    source_sha = Repo(source_repository).head.commit.hexsha
    progress_messages: list[str] = []
    runner = CompatibilityRunner(
        {"baseline": baseline, "candidate": candidate}
    )

    result = await runner.run(
        contract_path=write_contract(tmp_path),
        task_path=write_task(tmp_path),
        source_repository=source_repository,
        progress_callback=progress_messages.append,
    )

    assert isinstance(result, CompatibilityResult)
    assert result.original_commit_sha == source_sha
    assert result.task.prompt == "Update the application."
    assert result.baseline.execution is not None
    assert result.candidate.execution is not None
    assert result.baseline.diff.added_files == ["baseline.txt"]
    assert result.candidate.diff.changed_files == ["src/app.py"]
    assert result.baseline.workspace_path != result.candidate.workspace_path
    assert baseline.prepared_workspaces == baseline.executed_workspaces
    assert candidate.prepared_workspaces == candidate.executed_workspaces
    assert baseline.tasks[0] == candidate.tasks[0]
    assert baseline.executed_workspaces[0] != candidate.executed_workspaces[0]
    assert baseline.executed_workspaces[0] != source_repository
    assert candidate.executed_workspaces[0] != source_repository
    assert not result.baseline.workspace_path.exists()
    assert not result.candidate.workspace_path.exists()
    assert (source_repository / "src" / "app.py").read_text(
        encoding="utf-8"
    ) == "VALUE = 1\n"
    assert not (source_repository / "baseline.txt").exists()
    assert result.passed is True
    assert result.verdict.blocking_failures == []
    assert progress_messages[0] == "Loading contract and task"
    assert "Running baseline agent: baseline" in progress_messages
    assert "Running candidate agent: candidate" in progress_messages
    assert progress_messages.index("Running baseline agent: baseline") < (
        progress_messages.index("Running candidate agent: candidate")
    )
    assert progress_messages[-2:] == [
        "Cleaning up disposable workspaces",
        "Compatibility run complete",
    ]
    assert [finding.name for finding in result.verdict.results] == [
        "baseline_execution",
        "candidate_execution",
        "tests_must_pass",
        "build_must_pass",
        "forbidden_paths",
        "required_paths",
        "max_changed_files",
        "forbidden_dependencies",
    ]


@pytest.mark.asyncio
async def test_runner_reports_baseline_exception_and_continues_candidate(
    tmp_path: Path,
    source_repository: Path,
) -> None:
    baseline = FakeAdapter(
        "baseline",
        action=add_baseline_file,
        error=RuntimeError("baseline unavailable"),
    )
    candidate = FakeAdapter("candidate", action=update_candidate_source)

    result = await CompatibilityRunner(
        {"baseline": baseline, "candidate": candidate}
    ).run(
        contract_path=write_contract(tmp_path),
        task_path=write_task(tmp_path),
        source_repository=source_repository,
    )

    assert result.baseline.execution is None
    assert result.baseline.error == "RuntimeError: baseline unavailable"
    assert result.baseline.diff.added_files == ["baseline.txt"]
    assert result.candidate.execution is not None
    assert candidate.executed_workspaces
    assert result.passed is False
    assert result.verdict.blocking_failures == ["baseline_execution"]
    baseline_finding = result.verdict.results[0]
    assert baseline_finding.passed is False
    assert "baseline unavailable" in baseline_finding.summary
    assert not result.baseline.workspace_path.exists()
    assert not result.candidate.workspace_path.exists()


@pytest.mark.asyncio
async def test_runner_reports_candidate_nonzero_exit(
    tmp_path: Path,
    source_repository: Path,
) -> None:
    baseline = FakeAdapter("baseline")
    candidate = FakeAdapter(
        "candidate",
        action=update_candidate_source,
        exit_code=7,
    )

    result = await CompatibilityRunner(
        {"baseline": baseline, "candidate": candidate}
    ).run(
        contract_path=write_contract(tmp_path),
        task_path=write_task(tmp_path),
        source_repository=source_repository,
    )

    assert result.candidate.error is None
    assert result.candidate.execution is not None
    assert result.candidate.execution.exit_code == 7
    assert result.candidate.succeeded is False
    assert result.passed is False
    assert result.verdict.blocking_failures == ["candidate_execution"]
    assert result.verdict.results[1].summary == "Candidate agent exited with code 7."


def test_load_task_rejects_missing_prompt(tmp_path: Path) -> None:
    task_path = tmp_path / "task.yaml"
    task_path.write_text("name: incomplete\n", encoding="utf-8")

    with pytest.raises(TaskValidationError, match="prompt: Field required"):
        load_task(task_path)


@pytest.mark.asyncio
async def test_runner_executes_task_verification_in_each_workspace(
    tmp_path: Path,
    source_repository: Path,
) -> None:
    source_check = (
        "from pathlib import Path; "
        "raise SystemExit(Path('src/app.py').read_text() != 'VALUE = 2\\n')"
    )
    build_check = (
        "import ast, pathlib; "
        "ast.parse(pathlib.Path('src/app.py').read_text())"
    )
    task_path = tmp_path / "verified-task.yaml"
    task_path.write_text(
        yaml.safe_dump(
            {
                "name": "verified-task",
                "prompt": "Update the application.",
                "verification": {
                    "test_command": shlex.join(
                        [sys.executable, "-c", source_check]
                    ),
                    "build_command": shlex.join(
                        [sys.executable, "-c", build_check]
                    ),
                },
            }
        ),
        encoding="utf-8",
    )
    baseline = FakeAdapter("baseline")
    candidate = FakeAdapter("candidate", action=update_candidate_source)
    progress_messages: list[str] = []

    result = await CompatibilityRunner(
        {"baseline": baseline, "candidate": candidate}
    ).run(
        contract_path=write_contract(tmp_path),
        task_path=task_path,
        source_repository=source_repository,
        progress_callback=progress_messages.append,
    )

    assert result.baseline.execution is not None
    assert result.candidate.execution is not None
    assert result.baseline.execution.metadata["tests_passed"] is False
    assert result.baseline.execution.metadata["build_passed"] is True
    assert result.candidate.execution.metadata["tests_passed"] is True
    assert result.candidate.execution.metadata["build_passed"] is True
    assert result.candidate.execution.metadata["test_exit_code"] == 0
    assert result.candidate.execution.metadata["build_exit_code"] == 0
    assert len(result.candidate.execution.commands_executed) == 2
    assert result.passed is True
    assert "Running baseline test verification" in progress_messages
    assert "Running baseline build verification" in progress_messages
    assert "Running candidate test verification" in progress_messages
    assert "Running candidate build verification" in progress_messages


def test_runner_rejects_nonpositive_verification_timeout() -> None:
    with pytest.raises(ValueError, match="verification_timeout_seconds"):
        CompatibilityRunner({}, verification_timeout_seconds=0)
