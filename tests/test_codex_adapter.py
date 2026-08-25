"""Unit tests for the non-interactive Codex CLI adapter."""

import asyncio
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from git import Actor, Repo

import agentcompat.adapters.codex as codex_module
from agentcompat.adapters.base import AgentAdapter
from agentcompat.adapters.codex import (
    CodexAdapter,
    CodexCLIUnavailableError,
    UnsafeWorkspaceError,
)
from agentcompat.models import TaskDefinition
from agentcompat.workspace import DisposableWorkspace, WorkspaceManager


@pytest.fixture
def source_repository(tmp_path: Path) -> Path:
    """Create the source Git repository used by adapter tests."""
    source = tmp_path / "source"
    source.mkdir()
    (source / "README.md").write_text("fixture\n", encoding="utf-8")
    repository = Repo.init(source)
    repository.index.add(["README.md"])
    author = Actor("AgentCompat Tests", "tests@agentcompat.local")
    repository.index.commit("Initial commit", author=author, committer=author)
    repository.close()
    return source


@pytest.fixture
def disposable_workspace(
    source_repository: Path,
) -> Iterator[DisposableWorkspace]:
    """Create an AgentCompat-stamped Git workspace for adapter tests."""
    workspace = WorkspaceManager(source_repository).create_workspace()
    try:
        yield workspace
    finally:
        workspace.cleanup()


def make_process(
    *,
    returncode: int = 0,
    stdout: bytes = b"completed\n",
    stderr: bytes = b"",
) -> SimpleNamespace:
    """Return a subprocess-shaped mock with captured communication."""
    return SimpleNamespace(
        returncode=returncode,
        communicate=AsyncMock(return_value=(stdout, stderr)),
        kill=Mock(),
    )


def install_subprocess_mock(
    monkeypatch: pytest.MonkeyPatch,
    process: object,
) -> AsyncMock:
    """Make Codex available and replace async subprocess creation."""
    monkeypatch.setattr(codex_module, "which", lambda executable: "/mock/codex")
    create_subprocess = AsyncMock(return_value=process)
    monkeypatch.setattr(
        codex_module.asyncio,
        "create_subprocess_exec",
        create_subprocess,
    )
    return create_subprocess


@pytest.mark.asyncio
async def test_codex_adapter_executes_prompt_non_interactively(
    disposable_workspace: DisposableWorkspace,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    process = make_process(stdout=b"agent output\n")
    create_subprocess = install_subprocess_mock(monkeypatch, process)
    adapter = CodexAdapter(timeout_seconds=30)
    task = TaskDefinition(prompt="Implement pagination safely.")

    result = await adapter.execute(task, disposable_workspace.workspace_path)

    assert isinstance(adapter, AgentAdapter)
    assert result.agent_name == "codex"
    assert result.exit_code == 0
    assert result.stdout == "agent output\n"
    assert result.stderr == ""
    assert result.metadata["timed_out"] is False
    assert result.metadata["workspace"] == str(
        disposable_workspace.workspace_path.resolve()
    )
    command = create_subprocess.await_args.args
    assert command == (
        "/mock/codex",
        "--ask-for-approval",
        "never",
        "exec",
        "--sandbox",
        "workspace-write",
        "--ephemeral",
        "--color",
        "never",
        "--cd",
        str(disposable_workspace.workspace_path.resolve()),
        "-",
    )
    assert create_subprocess.await_args.kwargs["cwd"] == (
        disposable_workspace.workspace_path.resolve()
    )
    process.communicate.assert_awaited_once_with(b"Implement pagination safely.")
    assert "Implement pagination safely." not in result.commands_executed[0]


@pytest.mark.asyncio
async def test_codex_adapter_captures_nonzero_exit_and_stderr(
    disposable_workspace: DisposableWorkspace,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    process = make_process(returncode=2, stdout=b"partial", stderr=b"failed")
    install_subprocess_mock(monkeypatch, process)

    result = await CodexAdapter().execute(
        TaskDefinition(prompt="Run the task."),
        disposable_workspace.workspace_path,
    )

    assert result.exit_code == 2
    assert result.stdout == "partial"
    assert result.stderr == "failed"


class BlockingProcess:
    """Subprocess mock that exits only after it is killed."""

    def __init__(self) -> None:
        self.returncode: int | None = None
        self.killed = False
        self.prompt: bytes | None = None
        self._killed = asyncio.Event()

    async def communicate(self, prompt: bytes) -> tuple[bytes, bytes]:
        self.prompt = prompt
        await self._killed.wait()
        return b"partial output", b"partial error"

    def kill(self) -> None:
        self.killed = True
        self.returncode = -9
        self._killed.set()


@pytest.mark.asyncio
async def test_codex_adapter_kills_process_on_timeout(
    disposable_workspace: DisposableWorkspace,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    process = BlockingProcess()
    install_subprocess_mock(monkeypatch, process)

    result = await CodexAdapter(timeout_seconds=0.01).execute(
        TaskDefinition(prompt="Long task."),
        disposable_workspace.workspace_path,
    )

    assert process.killed is True
    assert process.prompt == b"Long task."
    assert result.exit_code == -9
    assert result.stdout == "partial output"
    assert "partial error" in result.stderr
    assert "timed out after 0.01 seconds" in result.stderr
    assert result.metadata["timed_out"] is True


@pytest.mark.asyncio
async def test_codex_adapter_refuses_original_repository(
    source_repository: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    create_subprocess = install_subprocess_mock(monkeypatch, make_process())

    with pytest.raises(UnsafeWorkspaceError, match="disposable workspace"):
        await CodexAdapter().execute(
            TaskDefinition(prompt="Unsafe task."),
            source_repository,
        )

    create_subprocess.assert_not_awaited()


@pytest.mark.asyncio
async def test_codex_adapter_refuses_unmarked_git_repository(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository_path = tmp_path / "repository"
    Repo.init(repository_path).close()
    create_subprocess = install_subprocess_mock(monkeypatch, make_process())

    with pytest.raises(UnsafeWorkspaceError, match="disposable workspace"):
        await CodexAdapter().execute(
            TaskDefinition(prompt="Unsafe task."),
            repository_path,
        )

    create_subprocess.assert_not_awaited()


@pytest.mark.asyncio
async def test_codex_adapter_reports_missing_executable(
    disposable_workspace: DisposableWorkspace,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(codex_module, "which", lambda executable: None)

    with pytest.raises(CodexCLIUnavailableError, match="not found on PATH"):
        await CodexAdapter().prepare(disposable_workspace.workspace_path)


def test_codex_adapter_rejects_nonpositive_timeout() -> None:
    with pytest.raises(ValueError, match="must be positive"):
        CodexAdapter(timeout_seconds=0)
