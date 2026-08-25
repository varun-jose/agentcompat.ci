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
from agentcompat.runner import (
    CompatibilityRunner,
    RunnerConfigurationError,
    TaskValidationError,
    load_task,
)

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


def write_contract(
    tmp_path: Path,
    tasks: list[str] | None = None,
    forbidden_dependencies: list[str] | None = None,
    dependency_drift: dict[str, object] | None = None,
    dependency_manifests: list[dict[str, str]] | None = None,
    max_changed_files: int = 2,
) -> Path:
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
            "max_changed_files": max_changed_files,
            "forbidden_dependencies": (
                forbidden_dependencies
                if forbidden_dependencies is not None
                else ["unsafe-package"]
            ),
            "dependency_drift": dependency_drift or {},
            "dependency_manifests": dependency_manifests or [],
        },
        "tasks": tasks if tasks is not None else ["task.yaml"],
    }
    path = tmp_path / "contract.yaml"
    path.write_text(yaml.safe_dump(contract), encoding="utf-8")
    return path


def write_task(
    tmp_path: Path,
    prompt: str = "Update the application.",
    rules: dict[str, object] | None = None,
    filename: str = "task.yaml",
) -> Path:
    """Write a task definition with one extensible metadata field."""
    path = tmp_path / filename
    task: dict[str, object] = {"name": "update-app", "prompt": prompt}
    if rules is not None:
        task["rules"] = rules
    path.write_text(
        yaml.safe_dump(task),
        encoding="utf-8",
    )
    return path


def commit_python_manifest(
    source_repository: Path,
    content: str,
    message: str,
) -> None:
    """Commit a source dependency manifest for runner integration tests."""
    (source_repository / "pyproject.toml").write_text(content, encoding="utf-8")
    repository = Repo(source_repository)
    repository.index.add(["pyproject.toml"])
    author = Actor("AgentCompat Tests", "tests@agentcompat.local")
    repository.index.commit(message, author=author, committer=author)
    repository.close()


def commit_repository_file(
    source_repository: Path,
    relative_path: str,
    content: str,
    message: str,
) -> None:
    """Commit one arbitrary dependency fixture into the source repository."""
    path = source_repository / relative_path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    repository = Repo(source_repository)
    repository.index.add([relative_path])
    author = Actor("AgentCompat Tests", "tests@agentcompat.local")
    repository.index.commit(message, author=author, committer=author)
    repository.close()


class FakeAdapter(AgentAdapter):
    """In-process adapter that records and modifies its assigned workspace."""

    def __init__(
        self,
        agent_name: str,
        *,
        action: WorkspaceAction | None = None,
        error: Exception | None = None,
        exit_code: int = 0,
        metadata: dict[str, object] | None = None,
    ) -> None:
        self.agent_name = agent_name
        self.action = action
        self.error = error
        self.exit_code = exit_code
        self.metadata = metadata or {}
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
                **self.metadata,
            },
        )


def add_baseline_file(workspace: Path) -> None:
    """Make a baseline-only untracked change."""
    (workspace / "baseline.txt").write_text("baseline\n", encoding="utf-8")


def update_candidate_source(workspace: Path) -> None:
    """Make a candidate-only tracked source change."""
    (workspace / "src" / "app.py").write_text("VALUE = 2\n", encoding="utf-8")


def delete_candidate_source(workspace: Path) -> None:
    """Delete the candidate's only required source file."""
    (workspace / "src" / "app.py").unlink()


def update_candidate_source_and_readme(workspace: Path) -> None:
    """Make two candidate changes that only a task-level limit rejects."""
    update_candidate_source(workspace)
    (workspace / "README.md").write_text("candidate\n", encoding="utf-8")


def update_candidate_source_and_cache(workspace: Path) -> None:
    """Change source and leave one untracked Git-ignored cache artifact."""
    update_candidate_source(workspace)
    cache_directory = workspace / ".pytest_cache"
    cache_directory.mkdir()
    (cache_directory / "README.md").write_text("cache\n", encoding="utf-8")


def update_candidate_source_and_ignored_secret(workspace: Path) -> None:
    """Change source and create a forbidden path hidden by Git ignore rules."""
    update_candidate_source(workspace)
    secret_directory = workspace / "secrets"
    secret_directory.mkdir()
    (secret_directory / "token.txt").write_text("secret\n", encoding="utf-8")


def add_forbidden_python_dependency(workspace: Path) -> None:
    """Add a forbidden dependency while the adapter reports no additions."""
    update_candidate_source(workspace)
    (workspace / "pyproject.toml").write_text(
        """
[project]
name = "runner-fixture"
version = "0.1.0"
dependencies = ["unsafe_package>=1"]
""".strip(),
        encoding="utf-8",
    )


def add_forbidden_requirements_dependency(workspace: Path) -> None:
    """Declare a forbidden dependency in a newly added requirements file."""
    update_candidate_source(workspace)
    (workspace / "requirements.txt").write_text(
        "unsafe_package>=1\n",
        encoding="utf-8",
    )


def add_malformed_python_manifest(workspace: Path) -> None:
    """Make the final dependency state impossible to observe."""
    update_candidate_source(workspace)
    (workspace / "pyproject.toml").write_text(
        "dependencies = [unterminated\n",
        encoding="utf-8",
    )


def change_python_dependency_version(workspace: Path) -> None:
    """Change an existing direct dependency constraint."""
    update_candidate_source(workspace)
    (workspace / "pyproject.toml").write_text(
        """
[project]
name = "runner-fixture"
version = "0.1.0"
dependencies = ["requests>=3"]
""".strip(),
        encoding="utf-8",
    )


def change_python_dependency_source(workspace: Path) -> None:
    """Change a credential-bearing URL without exposing it in evidence."""
    update_candidate_source(workspace)
    (workspace / "pyproject.toml").write_text(
        """
[project]
name = "runner-fixture"
version = "0.1.0"
dependencies = [
  "private-package @ https://user:new-secret@two.example/package.whl"
]
""".strip(),
        encoding="utf-8",
    )


def remove_python_dependency(workspace: Path) -> None:
    """Remove an existing direct dependency."""
    update_candidate_source(workspace)
    (workspace / "pyproject.toml").write_text(
        """
[project]
name = "runner-fixture"
version = "0.1.0"
dependencies = []
""".strip(),
        encoding="utf-8",
    )


def change_pylock_version(workspace: Path) -> None:
    """Modify a lockfile and its resolved package version."""
    update_candidate_source(workspace)
    (workspace / "pylock.toml").write_text(
        """
lock-version = "1.0"
created-by = "tests"
[[packages]]
name = "requests"
version = "3.0"
index = "https://pypi.org/simple"
wheels = [{url = "https://files.example/requests.whl", hashes = {sha256 = "def"}}]
""".strip(),
        encoding="utf-8",
    )


def change_manifest_without_lockfile(workspace: Path) -> None:
    """Change a declaration while leaving its required lockfile untouched."""
    change_python_dependency_version(workspace)


def change_manifest_and_lockfile(workspace: Path) -> None:
    """Change a declaration and its configured lockfile together."""
    change_python_dependency_version(workspace)
    (workspace / "pylock.toml").write_text(
        """
lock-version = "1.0"
created-by = "tests"
[[packages]]
name = "requests"
version = "3.0"
index = "https://pypi.org/simple"
wheels = [{url = "https://files.example/requests.whl", hashes = {sha256 = "def"}}]
""".strip(),
        encoding="utf-8",
    )


def move_dependency_and_change_qualifiers(workspace: Path) -> None:
    """Move a declaration while changing extras and environment markers."""
    update_candidate_source(workspace)
    (workspace / "pyproject.toml").write_text(
        """
[project]
name = "runner-fixture"
version = "0.1.0"
[project.optional-dependencies]
runtime = ["requests[socks]>=2; python_version >= '3.12'"]
""".strip(),
        encoding="utf-8",
    )


def change_constraint_only(workspace: Path) -> None:
    """Change a resolver constraint without changing a direct name."""
    update_candidate_source(workspace)
    (workspace / "constraints.txt").write_text("requests<3\n", encoding="utf-8")


def change_pip_index_policy(workspace: Path) -> None:
    """Change only the credential-safe pip resolver source policy."""
    update_candidate_source(workspace)
    (workspace / "requirements.txt").write_text(
        "--index-url https://user:new-secret@two.example/simple\nrequests\n",
        encoding="utf-8",
    )


def delete_configured_lockfile(workspace: Path) -> None:
    """Delete a lockfile that semantic dependency rules explicitly require."""
    update_candidate_source(workspace)
    (workspace / "pylock.toml").unlink()


def change_lockfile_line_endings(workspace: Path) -> None:
    """Change exact lock bytes without changing parsed lock semantics."""
    update_candidate_source(workspace)
    lockfile = workspace / "pylock.toml"
    lockfile.write_bytes(lockfile.read_bytes().replace(b"\n", b"\r\n"))


def change_manifest_and_lockfile_line_endings(workspace: Path) -> None:
    """Change one resolver input and touch the configured lock's exact bytes."""
    change_python_dependency_version(workspace)
    lockfile = workspace / "pylock.toml"
    lockfile.write_bytes(lockfile.read_bytes().replace(b"\n", b"\r\n"))


def add_transitive_forbidden_lock_package(workspace: Path) -> None:
    """Add a forbidden name only as a transitive lockfile package."""
    update_candidate_source(workspace)
    (workspace / "poetry.lock").write_text(
        """
[[package]]
name = "unsafe-package"
version = "1.0"
[metadata]
lock-version = "2.1"
""".strip(),
        encoding="utf-8",
    )


def add_forbidden_explicit_requirement(workspace: Path) -> None:
    """Add a forbidden package to an explicitly configured nested manifest."""
    update_candidate_source(workspace)
    (workspace / "requirements" / "base.in").write_text(
        "safe-package\nunsafe-package\n",
        encoding="utf-8",
    )


@pytest.mark.asyncio
async def test_runner_executes_isolated_agents_and_collects_results(
    tmp_path: Path,
    source_repository: Path,
) -> None:
    baseline = FakeAdapter("baseline", action=add_baseline_file)
    candidate = FakeAdapter("candidate", action=update_candidate_source)
    source_sha = Repo(source_repository).head.commit.hexsha
    progress_messages: list[str] = []
    runner = CompatibilityRunner({"baseline": baseline, "candidate": candidate})

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
        "dependency_removals",
        "dependency_version_changes",
        "dependency_source_changes",
        "dependency_lockfile_changes",
        "dependency_lockfile_updates",
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


@pytest.mark.asyncio
async def test_runner_does_not_count_deleted_files_as_required_path_matches(
    tmp_path: Path,
    source_repository: Path,
) -> None:
    baseline = FakeAdapter("baseline")
    candidate = FakeAdapter("candidate", action=delete_candidate_source)

    result = await CompatibilityRunner(
        {"baseline": baseline, "candidate": candidate}
    ).run(
        contract_path=write_contract(tmp_path),
        task_path=write_task(tmp_path),
        source_repository=source_repository,
    )

    assert result.candidate.diff.deleted_files == ["src/app.py"]
    assert result.passed is False
    assert result.verdict.blocking_failures == ["required_paths"]
    required_finding = next(
        finding
        for finding in result.verdict.results
        if finding.name == "required_paths"
    )
    assert required_finding.metadata["missing_patterns"] == ["src/**"]


def test_load_task_rejects_missing_prompt(tmp_path: Path) -> None:
    task_path = tmp_path / "task.yaml"
    task_path.write_text("name: incomplete\n", encoding="utf-8")

    with pytest.raises(TaskValidationError, match="prompt: Field required"):
        load_task(task_path)


def test_load_task_parses_strict_task_rules(tmp_path: Path) -> None:
    task = load_task(
        write_task(
            tmp_path,
            rules={
                "forbidden_paths": ["task/**"],
                "max_changed_files": 1,
            },
        )
    )

    assert task.rules is not None
    assert task.rules.forbidden_paths == ["task/**"]
    assert task.rules.max_changed_files == 1


def test_load_task_rejects_unknown_rule_fields(tmp_path: Path) -> None:
    task_path = write_task(tmp_path, rules={"unsupported_rule": True})

    with pytest.raises(
        TaskValidationError,
        match=r"rules\.unsupported_rule: Extra inputs",
    ):
        load_task(task_path)


@pytest.mark.asyncio
async def test_runner_rejects_task_not_declared_by_contract(
    tmp_path: Path,
    source_repository: Path,
) -> None:
    baseline = FakeAdapter("baseline")
    candidate = FakeAdapter("candidate")
    task_path = tmp_path / "undeclared-task.yaml"
    task_path.write_text("rules: [unterminated\n", encoding="utf-8")

    with pytest.raises(
        RunnerConfigurationError,
        match="task is not declared by the contract",
    ):
        await CompatibilityRunner({"baseline": baseline, "candidate": candidate}).run(
            contract_path=write_contract(tmp_path),
            task_path=task_path,
            source_repository=source_repository,
        )

    assert baseline.prepared_workspaces == []
    assert candidate.prepared_workspaces == []


@pytest.mark.asyncio
async def test_runner_enforces_stricter_task_changed_file_limit(
    tmp_path: Path,
    source_repository: Path,
) -> None:
    baseline = FakeAdapter("baseline")
    candidate = FakeAdapter(
        "candidate",
        action=update_candidate_source_and_readme,
    )

    result = await CompatibilityRunner(
        {"baseline": baseline, "candidate": candidate}
    ).run(
        contract_path=write_contract(tmp_path),
        task_path=write_task(tmp_path, rules={"max_changed_files": 1}),
        source_repository=source_repository,
    )

    assert result.candidate.diff.changed_files == ["README.md", "src/app.py"]
    assert result.passed is False
    assert result.verdict.blocking_failures == ["max_changed_files"]
    changed_file_finding = next(
        finding
        for finding in result.verdict.results
        if finding.name == "max_changed_files"
    )
    assert changed_file_finding.metadata["max_changed_files"] == 1
    assert changed_file_finding.metadata["contract_max_changed_files"] == 2
    assert changed_file_finding.metadata["task_max_changed_files"] == 1
    assert "effective maximum is 1" in changed_file_finding.summary


@pytest.mark.asyncio
async def test_runner_reports_but_does_not_count_ignored_files_by_default(
    tmp_path: Path,
    source_repository: Path,
) -> None:
    commit_repository_file(
        source_repository,
        ".gitignore",
        ".pytest_cache/\n",
        "Ignore test cache",
    )

    result = await CompatibilityRunner(
        {
            "baseline": FakeAdapter("baseline"),
            "candidate": FakeAdapter(
                "candidate",
                action=update_candidate_source_and_cache,
            ),
        }
    ).run(
        contract_path=write_contract(tmp_path, max_changed_files=1),
        task_path=write_task(tmp_path),
        source_repository=source_repository,
    )

    assert result.passed is True
    assert result.candidate.diff.changed_files == ["src/app.py"]
    assert result.candidate.diff.added_files == []
    assert result.candidate.diff.ignored_files == [".pytest_cache/README.md"]
    finding = next(
        item for item in result.verdict.results if item.name == "max_changed_files"
    )
    assert finding.evidence == ["src/app.py"]
    assert finding.metadata["changed_file_count"] == 1
    assert finding.metadata["observed_ignored_file_count"] == 1
    assert "1 Git-ignored artifact(s) were not counted" in finding.summary


@pytest.mark.asyncio
async def test_runner_can_count_or_exclude_ignored_files(
    tmp_path: Path,
    source_repository: Path,
) -> None:
    commit_repository_file(
        source_repository,
        ".gitignore",
        ".pytest_cache/\n",
        "Ignore test cache",
    )
    contract_path = write_contract(tmp_path, max_changed_files=1)
    raw_contract = yaml.safe_load(contract_path.read_text(encoding="utf-8"))
    raw_contract["rules"]["changed_files"] = {
        "max": 1,
        "include_ignored": True,
    }
    contract_path.write_text(yaml.safe_dump(raw_contract), encoding="utf-8")

    counted = await CompatibilityRunner(
        {
            "baseline": FakeAdapter("baseline"),
            "candidate": FakeAdapter(
                "candidate",
                action=update_candidate_source_and_cache,
            ),
        }
    ).run(
        contract_path=contract_path,
        task_path=write_task(tmp_path),
        source_repository=source_repository,
    )

    raw_contract["rules"]["changed_files"]["exclude"] = [".pytest_cache/**"]
    contract_path.write_text(yaml.safe_dump(raw_contract), encoding="utf-8")
    excluded = await CompatibilityRunner(
        {
            "baseline": FakeAdapter("baseline"),
            "candidate": FakeAdapter(
                "candidate",
                action=update_candidate_source_and_cache,
            ),
        }
    ).run(
        contract_path=contract_path,
        task_path=write_task(tmp_path),
        source_repository=source_repository,
    )

    assert counted.verdict.blocking_failures == ["max_changed_files"]
    assert excluded.passed is True
    excluded_finding = next(
        item
        for item in excluded.verdict.results
        if item.name == "max_changed_files"
    )
    assert excluded_finding.evidence == ["src/app.py"]
    assert excluded_finding.metadata["excluded_paths"] == [
        ".pytest_cache/README.md"
    ]


@pytest.mark.asyncio
async def test_runner_checks_forbidden_rules_against_ignored_files(
    tmp_path: Path,
    source_repository: Path,
) -> None:
    commit_repository_file(
        source_repository,
        ".gitignore",
        "secrets/\n",
        "Ignore local secrets",
    )

    result = await CompatibilityRunner(
        {
            "baseline": FakeAdapter("baseline"),
            "candidate": FakeAdapter(
                "candidate",
                action=update_candidate_source_and_ignored_secret,
            ),
        }
    ).run(
        contract_path=write_contract(tmp_path, max_changed_files=1),
        task_path=write_task(tmp_path),
        source_repository=source_repository,
    )

    assert result.candidate.diff.ignored_files == ["secrets/token.txt"]
    assert result.verdict.blocking_failures == ["forbidden_paths"]
    forbidden_finding = next(
        item for item in result.verdict.results if item.name == "forbidden_paths"
    )
    assert forbidden_finding.evidence == ["secrets/token.txt"]


@pytest.mark.asyncio
async def test_runner_detects_forbidden_dependency_from_candidate_manifest(
    tmp_path: Path,
    source_repository: Path,
) -> None:
    baseline = FakeAdapter("baseline")
    candidate = FakeAdapter("candidate", action=add_forbidden_python_dependency)

    result = await CompatibilityRunner(
        {"baseline": baseline, "candidate": candidate}
    ).run(
        contract_path=write_contract(tmp_path),
        task_path=write_task(tmp_path),
        source_repository=source_repository,
    )

    assert result.candidate.execution is not None
    assert result.candidate.execution.metadata["added_dependencies"] == [
        "unsafe-package"
    ]
    assert result.passed is False
    assert result.verdict.blocking_failures == ["forbidden_dependencies"]


@pytest.mark.asyncio
async def test_runner_detects_forbidden_dependency_from_requirements_file(
    tmp_path: Path,
    source_repository: Path,
) -> None:
    candidate = FakeAdapter(
        "candidate",
        action=add_forbidden_requirements_dependency,
    )

    result = await CompatibilityRunner(
        {"baseline": FakeAdapter("baseline"), "candidate": candidate}
    ).run(
        contract_path=write_contract(tmp_path),
        task_path=write_task(tmp_path),
        source_repository=source_repository,
    )

    assert result.candidate.execution is not None
    metadata = result.candidate.execution.metadata
    assert metadata["dependency_scanner"] == "python-manifests-lockfiles-v2"
    assert metadata["added_dependencies"] == ["unsafe-package"]
    assert result.passed is False
    assert result.verdict.blocking_failures == ["forbidden_dependencies"]


@pytest.mark.asyncio
async def test_runner_enforces_task_level_forbidden_dependency_rule(
    tmp_path: Path,
    source_repository: Path,
) -> None:
    baseline = FakeAdapter("baseline")
    candidate = FakeAdapter("candidate", action=add_forbidden_python_dependency)

    result = await CompatibilityRunner(
        {"baseline": baseline, "candidate": candidate}
    ).run(
        contract_path=write_contract(tmp_path, forbidden_dependencies=[]),
        task_path=write_task(
            tmp_path,
            rules={"forbidden_dependencies": ["unsafe-package"]},
        ),
        source_repository=source_repository,
    )

    assert result.passed is False
    assert result.verdict.blocking_failures == ["forbidden_dependencies"]


@pytest.mark.asyncio
async def test_runner_fails_dependency_rule_closed_for_malformed_manifest(
    tmp_path: Path,
    source_repository: Path,
) -> None:
    baseline = FakeAdapter("baseline")
    candidate = FakeAdapter("candidate", action=add_malformed_python_manifest)

    result = await CompatibilityRunner(
        {"baseline": baseline, "candidate": candidate}
    ).run(
        contract_path=write_contract(tmp_path),
        task_path=write_task(tmp_path),
        source_repository=source_repository,
    )

    assert result.candidate.execution is not None
    assert "added_dependencies" not in result.candidate.execution.metadata
    assert (
        result.candidate.execution.metadata["dependency_observation_available"] is False
    )
    assert result.passed is False
    assert result.verdict.blocking_failures == ["forbidden_dependencies"]


@pytest.mark.asyncio
async def test_runner_does_not_report_preexisting_forbidden_dependency_as_added(
    tmp_path: Path,
    source_repository: Path,
) -> None:
    commit_python_manifest(
        source_repository,
        """
[project]
name = "runner-fixture"
version = "0.1.0"
dependencies = ["unsafe-package>=1"]
""".strip(),
        "Add existing dependency",
    )

    result = await CompatibilityRunner(
        {
            "baseline": FakeAdapter("baseline"),
            "candidate": FakeAdapter("candidate", action=update_candidate_source),
        }
    ).run(
        contract_path=write_contract(tmp_path),
        task_path=write_task(tmp_path),
        source_repository=source_repository,
    )

    assert result.candidate.execution is not None
    assert result.candidate.execution.metadata["added_dependencies"] == []
    assert result.passed is True


@pytest.mark.asyncio
async def test_runner_skips_dependency_scan_when_rule_is_unconfigured(
    tmp_path: Path,
    source_repository: Path,
) -> None:
    commit_python_manifest(
        source_repository,
        """
[project]
name = "runner-fixture"
version = "0.1.0"
dynamic = ["dependencies"]
""".strip(),
        "Add dynamic dependencies",
    )

    result = await CompatibilityRunner(
        {
            "baseline": FakeAdapter("baseline"),
            "candidate": FakeAdapter("candidate", action=update_candidate_source),
        }
    ).run(
        contract_path=write_contract(tmp_path, forbidden_dependencies=[]),
        task_path=write_task(tmp_path),
        source_repository=source_repository,
    )

    assert result.passed is True


@pytest.mark.asyncio
async def test_runner_rejects_dependency_version_drift(
    tmp_path: Path,
    source_repository: Path,
) -> None:
    commit_python_manifest(
        source_repository,
        """
[project]
name = "runner-fixture"
version = "0.1.0"
dependencies = ["requests>=2"]
""".strip(),
        "Add dependency",
    )

    result = await CompatibilityRunner(
        {
            "baseline": FakeAdapter("baseline"),
            "candidate": FakeAdapter(
                "candidate",
                action=change_python_dependency_version,
            ),
        }
    ).run(
        contract_path=write_contract(
            tmp_path,
            forbidden_dependencies=[],
            dependency_drift={"versions_must_not_change": True},
        ),
        task_path=write_task(tmp_path),
        source_repository=source_repository,
    )

    assert result.verdict.blocking_failures == ["dependency_version_changes"]
    assert result.candidate.execution is not None
    changes = result.candidate.execution.metadata["dependency_version_changes"]
    assert isinstance(changes, list)
    assert changes and "requests" in changes[0]


@pytest.mark.asyncio
async def test_runner_rejects_dependency_source_drift_without_leaking_secrets(
    tmp_path: Path,
    source_repository: Path,
) -> None:
    commit_python_manifest(
        source_repository,
        """
[project]
name = "runner-fixture"
version = "0.1.0"
dependencies = [
  "private-package @ https://user:old-secret@one.example/package.whl"
]
""".strip(),
        "Add private dependency",
    )

    result = await CompatibilityRunner(
        {
            "baseline": FakeAdapter("baseline"),
            "candidate": FakeAdapter(
                "candidate",
                action=change_python_dependency_source,
            ),
        }
    ).run(
        contract_path=write_contract(
            tmp_path,
            forbidden_dependencies=[],
            dependency_drift={"sources_must_not_change": True},
        ),
        task_path=write_task(tmp_path),
        source_repository=source_repository,
    )

    assert result.verdict.blocking_failures == ["dependency_source_changes"]
    assert result.candidate.execution is not None
    serialized_metadata = str(result.candidate.execution.metadata)
    assert "old-secret" not in serialized_metadata
    assert "new-secret" not in serialized_metadata


@pytest.mark.asyncio
async def test_runner_rejects_direct_dependency_removal(
    tmp_path: Path,
    source_repository: Path,
) -> None:
    commit_python_manifest(
        source_repository,
        """
[project]
name = "runner-fixture"
version = "0.1.0"
dependencies = ["requests>=2"]
""".strip(),
        "Add dependency",
    )

    result = await CompatibilityRunner(
        {
            "baseline": FakeAdapter("baseline"),
            "candidate": FakeAdapter("candidate", action=remove_python_dependency),
        }
    ).run(
        contract_path=write_contract(
            tmp_path,
            forbidden_dependencies=[],
            dependency_drift={"removals_forbidden": True},
        ),
        task_path=write_task(tmp_path),
        source_repository=source_repository,
    )

    assert result.verdict.blocking_failures == ["dependency_removals"]
    assert result.candidate.execution is not None
    assert result.candidate.execution.metadata["removed_dependencies"] == ["requests"]


@pytest.mark.asyncio
async def test_runner_rejects_lockfile_content_drift(
    tmp_path: Path,
    source_repository: Path,
) -> None:
    commit_repository_file(
        source_repository,
        "pylock.toml",
        """
lock-version = "1.0"
created-by = "tests"
[[packages]]
name = "requests"
version = "2.0"
index = "https://pypi.org/simple"
wheels = [{url = "https://files.example/requests.whl", hashes = {sha256 = "abc"}}]
""".strip(),
        "Add lockfile",
    )

    result = await CompatibilityRunner(
        {
            "baseline": FakeAdapter("baseline"),
            "candidate": FakeAdapter("candidate", action=change_pylock_version),
        }
    ).run(
        contract_path=write_contract(
            tmp_path,
            forbidden_dependencies=[],
            dependency_drift={
                "lockfiles_must_not_change": True,
                "lockfiles": ["pylock.toml"],
            },
        ),
        task_path=write_task(tmp_path),
        source_repository=source_repository,
    )

    assert result.verdict.blocking_failures == ["dependency_lockfile_changes"]


@pytest.mark.asyncio
async def test_runner_uses_exact_lock_bytes_for_immutability_and_co_update(
    tmp_path: Path,
    source_repository: Path,
) -> None:
    commit_python_manifest(
        source_repository,
        """
[project]
name = "runner-fixture"
version = "0.1.0"
dependencies = ["requests>=2"]
""".strip(),
        "Add dependency",
    )
    commit_repository_file(
        source_repository,
        "pylock.toml",
        """
lock-version = "1.0"
created-by = "tests"
[[packages]]
name = "requests"
version = "2.0"
wheels = [{url = "https://files.example/requests.whl", hashes = {sha256 = "abc"}}]
""".strip()
        + "\n",
        "Add lockfile",
    )

    immutable = await CompatibilityRunner(
        {
            "baseline": FakeAdapter("baseline"),
            "candidate": FakeAdapter(
                "candidate",
                action=change_lockfile_line_endings,
            ),
        }
    ).run(
        contract_path=write_contract(
            tmp_path,
            forbidden_dependencies=[],
            dependency_drift={
                "lockfiles_must_not_change": True,
                "lockfiles": ["pylock.toml"],
            },
        ),
        task_path=write_task(tmp_path),
        source_repository=source_repository,
    )
    co_updated = await CompatibilityRunner(
        {
            "baseline": FakeAdapter("baseline"),
            "candidate": FakeAdapter(
                "candidate",
                action=change_manifest_and_lockfile_line_endings,
            ),
        }
    ).run(
        contract_path=write_contract(
            tmp_path,
            forbidden_dependencies=[],
            dependency_drift={
                "lockfiles_must_be_updated_for_dependency_changes": True,
                "lockfiles": ["pylock.toml"],
            },
            max_changed_files=3,
        ),
        task_path=write_task(tmp_path),
        source_repository=source_repository,
    )

    assert immutable.verdict.blocking_failures == ["dependency_lockfile_changes"]
    assert co_updated.passed is True


@pytest.mark.asyncio
async def test_runner_requires_lockfile_co_update_for_declaration_drift(
    tmp_path: Path,
    source_repository: Path,
) -> None:
    commit_python_manifest(
        source_repository,
        """
[project]
name = "runner-fixture"
version = "0.1.0"
dependencies = ["requests>=2"]
""".strip(),
        "Add dependency",
    )
    commit_repository_file(
        source_repository,
        "pylock.toml",
        """
lock-version = "1.0"
created-by = "tests"
[[packages]]
name = "requests"
version = "2.0"
index = "https://pypi.org/simple"
wheels = [{url = "https://files.example/requests.whl", hashes = {sha256 = "abc"}}]
""".strip(),
        "Add lockfile",
    )
    drift = {
        "lockfiles_must_be_updated_for_dependency_changes": True,
        "lockfiles": ["pylock.toml"],
    }

    failing = await CompatibilityRunner(
        {
            "baseline": FakeAdapter("baseline"),
            "candidate": FakeAdapter(
                "candidate",
                action=change_manifest_without_lockfile,
            ),
        }
    ).run(
        contract_path=write_contract(
            tmp_path,
            forbidden_dependencies=[],
            dependency_drift=drift,
        ),
        task_path=write_task(tmp_path),
        source_repository=source_repository,
    )
    passing = await CompatibilityRunner(
        {
            "baseline": FakeAdapter("baseline"),
            "candidate": FakeAdapter(
                "candidate",
                action=change_manifest_and_lockfile,
            ),
        }
    ).run(
        contract_path=write_contract(
            tmp_path,
            forbidden_dependencies=[],
            dependency_drift=drift,
            max_changed_files=3,
        ),
        task_path=write_task(tmp_path),
        source_repository=source_repository,
    )

    assert failing.verdict.blocking_failures == ["dependency_lockfile_updates"]
    assert passing.passed is True


@pytest.mark.asyncio
async def test_runner_co_update_detects_origin_marker_and_extra_changes(
    tmp_path: Path,
    source_repository: Path,
) -> None:
    commit_python_manifest(
        source_repository,
        """
[project]
name = "runner-fixture"
version = "0.1.0"
dependencies = ["requests[security]>=2; python_version >= '3.11'"]
""".strip(),
        "Add qualified dependency",
    )
    commit_repository_file(
        source_repository,
        "pylock.toml",
        """
lock-version = "1.0"
created-by = "tests"
[[packages]]
name = "requests"
version = "2.0"
wheels = [{url = "https://files.example/requests.whl", hashes = {sha256 = "abc"}}]
""".strip(),
        "Add lockfile",
    )

    result = await CompatibilityRunner(
        {
            "baseline": FakeAdapter("baseline"),
            "candidate": FakeAdapter(
                "candidate",
                action=move_dependency_and_change_qualifiers,
            ),
        }
    ).run(
        contract_path=write_contract(
            tmp_path,
            forbidden_dependencies=[],
            dependency_drift={
                "lockfiles_must_be_updated_for_dependency_changes": True,
                "lockfiles": ["pylock.toml"],
            },
        ),
        task_path=write_task(tmp_path),
        source_repository=source_repository,
    )

    assert result.verdict.blocking_failures == ["dependency_lockfile_updates"]
    assert result.candidate.execution is not None
    metadata = result.candidate.execution.metadata
    assert metadata["dependency_input_changed"] is True
    assert metadata["dependency_input_changes_count"] >= 2


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("manifest", "action"),
    [
        ("-c constraints.txt\nrequests\n", change_constraint_only),
        (
            "--index-url https://user:old-secret@one.example/simple\nrequests\n",
            change_pip_index_policy,
        ),
    ],
)
async def test_runner_co_update_detects_constraint_and_source_policy_changes(
    tmp_path: Path,
    source_repository: Path,
    manifest: str,
    action: WorkspaceAction,
) -> None:
    commit_repository_file(
        source_repository,
        "requirements.txt",
        manifest,
        "Add requirements",
    )
    if action is change_constraint_only:
        commit_repository_file(
            source_repository,
            "constraints.txt",
            "requests<4\n",
            "Add constraints",
        )
    commit_repository_file(
        source_repository,
        "pylock.toml",
        """
lock-version = "1.0"
created-by = "tests"
[[packages]]
name = "requests"
version = "2.0"
wheels = [{url = "https://files.example/requests.whl", hashes = {sha256 = "abc"}}]
""".strip(),
        "Add lockfile",
    )

    result = await CompatibilityRunner(
        {
            "baseline": FakeAdapter("baseline"),
            "candidate": FakeAdapter("candidate", action=action),
        }
    ).run(
        contract_path=write_contract(
            tmp_path,
            forbidden_dependencies=[],
            dependency_drift={
                "lockfiles_must_be_updated_for_dependency_changes": True,
                "lockfiles": ["pylock.toml"],
            },
        ),
        task_path=write_task(tmp_path),
        source_repository=source_repository,
    )

    assert result.verdict.blocking_failures == ["dependency_lockfile_updates"]
    assert result.candidate.execution is not None
    assert "old-secret" not in str(result.candidate.execution.metadata)
    assert "new-secret" not in str(result.candidate.execution.metadata)


@pytest.mark.asyncio
async def test_runner_fails_closed_when_configured_semantic_lock_is_deleted(
    tmp_path: Path,
    source_repository: Path,
) -> None:
    commit_repository_file(
        source_repository,
        "pylock.toml",
        """
lock-version = "1.0"
created-by = "tests"
[[packages]]
name = "requests"
version = "2.0"
wheels = [{url = "https://files.example/requests.whl", hashes = {sha256 = "abc"}}]
""".strip(),
        "Add lockfile",
    )

    result = await CompatibilityRunner(
        {
            "baseline": FakeAdapter("baseline"),
            "candidate": FakeAdapter(
                "candidate",
                action=delete_configured_lockfile,
            ),
        }
    ).run(
        contract_path=write_contract(
            tmp_path,
            forbidden_dependencies=[],
            dependency_drift={
                "versions_must_not_change": True,
                "lockfiles": ["pylock.toml"],
            },
        ),
        task_path=write_task(tmp_path),
        source_repository=source_repository,
    )

    assert "dependency_version_changes" in result.verdict.blocking_failures
    assert result.candidate.execution is not None
    assert (
        result.candidate.execution.metadata["dependency_observation_available"] is False
    )
    assert "does not exist" in str(
        result.candidate.execution.metadata["dependency_scan_error"]
    )


@pytest.mark.asyncio
async def test_runner_does_not_treat_transitive_lock_package_as_direct_addition(
    tmp_path: Path,
    source_repository: Path,
) -> None:
    result = await CompatibilityRunner(
        {
            "baseline": FakeAdapter("baseline"),
            "candidate": FakeAdapter(
                "candidate",
                action=add_transitive_forbidden_lock_package,
            ),
        }
    ).run(
        contract_path=write_contract(tmp_path),
        task_path=write_task(tmp_path),
        source_repository=source_repository,
    )

    assert result.passed is True
    assert result.candidate.execution is not None
    assert result.candidate.execution.metadata["added_dependencies"] == []


@pytest.mark.asyncio
async def test_runner_uses_explicit_nested_requirement_manifest(
    tmp_path: Path,
    source_repository: Path,
) -> None:
    commit_repository_file(
        source_repository,
        "requirements/base.in",
        "safe-package\n",
        "Add nested requirements",
    )

    result = await CompatibilityRunner(
        {
            "baseline": FakeAdapter("baseline"),
            "candidate": FakeAdapter(
                "candidate",
                action=add_forbidden_explicit_requirement,
            ),
        }
    ).run(
        contract_path=write_contract(
            tmp_path,
            dependency_manifests=[
                {"path": "requirements/base.in", "role": "requirement"}
            ],
        ),
        task_path=write_task(tmp_path),
        source_repository=source_repository,
    )

    assert result.verdict.blocking_failures == ["forbidden_dependencies"]


@pytest.mark.asyncio
async def test_runner_replaces_all_adapter_dependency_metadata(
    tmp_path: Path,
    source_repository: Path,
) -> None:
    candidate = FakeAdapter(
        "candidate",
        action=update_candidate_source,
        metadata={
            "dependency_version_changes": ["spoofed"],
            "dependency_observation_available": False,
            "dependency_scan_error": "spoofed",
            "removed_dependencies": ["spoofed"],
        },
    )

    result = await CompatibilityRunner(
        {"baseline": FakeAdapter("baseline"), "candidate": candidate}
    ).run(
        contract_path=write_contract(tmp_path),
        task_path=write_task(tmp_path),
        source_repository=source_repository,
    )

    assert result.candidate.execution is not None
    metadata = result.candidate.execution.metadata
    assert metadata["dependency_observation_available"] is True
    assert metadata["dependency_version_changes"] == []
    assert metadata["removed_dependencies"] == []
    assert "dependency_scan_error" not in metadata


@pytest.mark.asyncio
async def test_runner_sanitizes_dependency_metadata_when_scanning_is_disabled(
    tmp_path: Path,
    source_repository: Path,
) -> None:
    candidate = FakeAdapter(
        "candidate",
        action=update_candidate_source,
        metadata={
            "added_dependencies": ["spoofed"],
            "dependency_observation_available": True,
            "dependency_source_changes": ["spoofed"],
            "forbidden_dependency_additions_count": 999,
        },
    )

    result = await CompatibilityRunner(
        {"baseline": FakeAdapter("baseline"), "candidate": candidate}
    ).run(
        contract_path=write_contract(tmp_path, forbidden_dependencies=[]),
        task_path=write_task(tmp_path),
        source_repository=source_repository,
    )

    assert result.candidate.execution is not None
    metadata = result.candidate.execution.metadata
    assert "added_dependencies" not in metadata
    assert "dependency_observation_available" not in metadata
    assert "dependency_source_changes" not in metadata
    assert "forbidden_dependency_additions_count" not in metadata


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
        "import ast, pathlib; ast.parse(pathlib.Path('src/app.py').read_text())"
    )
    task_path = tmp_path / "verified-task.yaml"
    task_path.write_text(
        yaml.safe_dump(
            {
                "name": "verified-task",
                "prompt": "Update the application.",
                "verification": {
                    "test_command": shlex.join([sys.executable, "-c", source_check]),
                    "build_command": shlex.join([sys.executable, "-c", build_check]),
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
        contract_path=write_contract(tmp_path, tasks=["verified-task.yaml"]),
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
