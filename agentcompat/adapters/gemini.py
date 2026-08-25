"""Non-interactive adapter for the locally installed Gemini CLI."""

import asyncio
import shlex
from datetime import UTC, datetime
from pathlib import Path
from shutil import which

from agentcompat.adapters.base import AgentAdapter
from agentcompat.models import AgentExecutionResult, TaskDefinition
from agentcompat.workspace import is_disposable_workspace

DEFAULT_TIMEOUT_SECONDS = 900.0
AUTHENTICATION_EXIT_CODE = 41


class GeminiCLIUnavailableError(RuntimeError):
    """Raised when the configured Gemini executable is unavailable."""


class UnsafeWorkspaceError(RuntimeError):
    """Raised when an adapter is asked to use a non-disposable workspace."""


class GeminiAdapter(AgentAdapter):
    """Execute tasks with Gemini CLI headless mode in disposable workspaces."""

    agent_name = "gemini"

    def __init__(
        self,
        *,
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
        executable: str = "gemini",
    ) -> None:
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        self.timeout_seconds = timeout_seconds
        self.executable = executable
        self._executable_path: str | None = None

    async def prepare(self, workspace: Path) -> None:
        """Validate the Gemini executable and disposable workspace boundary."""
        resolved_workspace = workspace.resolve()
        if not is_disposable_workspace(resolved_workspace):
            raise UnsafeWorkspaceError(
                "refusing to run Gemini outside an AgentCompat disposable workspace: "
                f"{resolved_workspace}"
            )

        executable_path = which(self.executable)
        if executable_path is None:
            raise GeminiCLIUnavailableError(
                f"Gemini CLI executable was not found on PATH: {self.executable}"
            )
        self._executable_path = executable_path

    def _command(self) -> list[str]:
        if self._executable_path is None:
            raise GeminiCLIUnavailableError("GeminiAdapter has not been prepared")
        return [
            self._executable_path,
            "--prompt",
            "",
            "--approval-mode",
            "yolo",
            "--skip-trust",
            "--output-format",
            "text",
        ]

    async def execute(
        self,
        task: TaskDefinition,
        workspace: Path,
    ) -> AgentExecutionResult:
        """Run Gemini headlessly and capture its complete process result."""
        resolved_workspace = workspace.resolve()
        await self.prepare(resolved_workspace)
        command = self._command()
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
            raise GeminiCLIUnavailableError(
                f"Gemini CLI executable could not be started: {command[0]}"
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
                f"Gemini execution timed out after {self.timeout_seconds:g} seconds."
            )
            stderr = f"{stderr.rstrip()}\n{timeout_message}".lstrip()

        exit_code = process.returncode if process.returncode is not None else 124
        failure_reason: str | None = None
        if exit_code == AUTHENTICATION_EXIT_CODE:
            failure_reason = "authentication"
            authentication_message = (
                "Gemini authentication failed (exit code 41). Run `gemini` "
                "interactively to configure authentication."
            )
            stderr = f"{stderr.rstrip()}\n{authentication_message}".lstrip()
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
                "approval_mode": "yolo",
                "output_format": "text",
                "failure_reason": failure_reason,
            },
        )
