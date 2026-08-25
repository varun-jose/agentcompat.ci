"""Non-interactive adapter for the locally installed Codex CLI."""

import asyncio
import shlex
from datetime import UTC, datetime
from pathlib import Path
from shutil import which

from agentcompat.adapters.base import AgentAdapter
from agentcompat.models import AgentExecutionResult, TaskDefinition
from agentcompat.workspace import is_disposable_workspace

DEFAULT_TIMEOUT_SECONDS = 900.0


class CodexCLIUnavailableError(RuntimeError):
    """Raised when the configured Codex executable is unavailable."""


class UnsafeWorkspaceError(RuntimeError):
    """Raised when an adapter is asked to use a non-disposable workspace."""


class CodexAdapter(AgentAdapter):
    """Execute tasks with ``codex exec`` inside disposable workspaces."""

    agent_name = "codex"

    def __init__(
        self,
        *,
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
        executable: str = "codex",
    ) -> None:
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        self.timeout_seconds = timeout_seconds
        self.executable = executable
        self._executable_path: str | None = None

    async def prepare(self, workspace: Path) -> None:
        """Validate the Codex executable and disposable workspace boundary."""
        resolved_workspace = workspace.resolve()
        if not is_disposable_workspace(resolved_workspace):
            raise UnsafeWorkspaceError(
                "refusing to run Codex outside an AgentCompat disposable workspace: "
                f"{resolved_workspace}"
            )

        executable_path = which(self.executable)
        if executable_path is None:
            raise CodexCLIUnavailableError(
                f"Codex CLI executable was not found on PATH: {self.executable}"
            )
        self._executable_path = executable_path

    def _command(self, workspace: Path) -> list[str]:
        if self._executable_path is None:
            raise CodexCLIUnavailableError("CodexAdapter has not been prepared")
        return [
            self._executable_path,
            "--ask-for-approval",
            "never",
            "exec",
            "--sandbox",
            "workspace-write",
            "--ephemeral",
            "--color",
            "never",
            "--cd",
            str(workspace),
            "-",
        ]

    async def execute(
        self,
        task: TaskDefinition,
        workspace: Path,
    ) -> AgentExecutionResult:
        """Run Codex non-interactively and capture its complete process result."""
        resolved_workspace = workspace.resolve()
        await self.prepare(resolved_workspace)
        command = self._command(resolved_workspace)
        started_at = datetime.now(UTC)

        try:
            process = await asyncio.create_subprocess_exec(
                *command,
                cwd=resolved_workspace,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except FileNotFoundError as exc:
            raise CodexCLIUnavailableError(
                f"Codex CLI executable could not be started: {command[0]}"
            ) from exc

        communication = asyncio.create_task(
            process.communicate(task.prompt.encode("utf-8"))
        )
        timed_out = False
        try:
            stdout_bytes, stderr_bytes = await asyncio.wait_for(
                asyncio.shield(communication),
                timeout=self.timeout_seconds,
            )
        except TimeoutError:
            timed_out = True
            if process.returncode is None:
                process.kill()
            stdout_bytes, stderr_bytes = await communication
        except asyncio.CancelledError:
            if process.returncode is None:
                process.kill()
            await communication
            raise

        completed_at = datetime.now(UTC)
        stdout = stdout_bytes.decode("utf-8", errors="replace")
        stderr = stderr_bytes.decode("utf-8", errors="replace")
        if timed_out:
            timeout_message = (
                f"Codex execution timed out after {self.timeout_seconds:g} seconds."
            )
            stderr = f"{stderr.rstrip()}\n{timeout_message}".lstrip()

        exit_code = process.returncode if process.returncode is not None else 124
        return AgentExecutionResult(
            agent_name=self.agent_name,
            started_at=started_at,
            completed_at=completed_at,
            exit_code=exit_code,
            stdout=stdout,
            stderr=stderr,
            commands_executed=[shlex.join(command)],
            metadata={
                "workspace": str(resolved_workspace),
                "timeout_seconds": self.timeout_seconds,
                "timed_out": timed_out,
                "non_interactive": True,
                "sandbox": "workspace-write",
            },
        )
