"""Compatibility run orchestration."""

import asyncio
import shlex
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path

import yaml
from pydantic import ValidationError

from agentcompat.adapters.base import AgentAdapter
from agentcompat.contracts import load_contract
from agentcompat.evaluators import (
    BuildMustPassEvaluator,
    ChangedFileCountEvaluator,
    ForbiddenDependencyEvaluator,
    ForbiddenPathEvaluator,
    RequiredPathEvaluator,
    TestsMustPassEvaluator,
    build_compatibility_verdict,
)
from agentcompat.models import (
    AgentContract,
    AgentExecutionResult,
    AgentRunResult,
    CompatibilityResult,
    EvaluationResult,
    TaskDefinition,
    WorkspaceChanges,
)
from agentcompat.workspace import DisposableWorkspace, WorkspaceDiff, WorkspaceManager

DEFAULT_VERIFICATION_TIMEOUT_SECONDS = 300.0
type ProgressCallback = Callable[[str], None]


@dataclass(frozen=True, slots=True)
class VerificationCommandResult:
    """Captured outcome of one deterministic post-agent command."""

    command: str
    exit_code: int
    stdout: str
    stderr: str
    timed_out: bool = False

    @property
    def passed(self) -> bool:
        """Return whether the command completed successfully."""
        return self.exit_code == 0 and not self.timed_out


class TaskValidationError(ValueError):
    """Raised when a task cannot be loaded or validated."""


class RunnerConfigurationError(ValueError):
    """Raised when a compatibility run cannot resolve its adapters or candidate."""


def _notify_progress(
    callback: ProgressCallback | None,
    message: str,
) -> None:
    """Publish a best-effort progress message without affecting execution."""
    if callback is None:
        return
    try:
        callback(message)
    except Exception:
        return


def _format_task_validation_error(exc: ValidationError) -> str:
    """Format Pydantic task errors with YAML-friendly dotted paths."""
    messages: list[str] = []
    for error in exc.errors(include_input=False, include_url=False):
        location = ".".join(str(part) for part in error["loc"])
        messages.append(f"{location or 'task'}: {error['msg']}")
    return "\n".join(messages)


def load_task(path: Path) -> TaskDefinition:
    """Load and validate a YAML task definition."""
    try:
        raw_task = yaml.safe_load(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise TaskValidationError(f"could not read task: {exc}") from exc
    except yaml.YAMLError as exc:
        raise TaskValidationError(f"invalid YAML: {exc}") from exc

    try:
        return TaskDefinition.model_validate(raw_task)
    except ValidationError as exc:
        raise TaskValidationError(_format_task_validation_error(exc)) from exc


def _workspace_changes(diff: WorkspaceDiff) -> WorkspaceChanges:
    """Convert the workspace-layer diff into the serializable report model."""
    return WorkspaceChanges(
        changed_files=diff.changed_files,
        added_files=diff.added_files,
        deleted_files=diff.deleted_files,
    )


async def _run_verification_command(
    command: str,
    workspace: Path,
    timeout_seconds: float,
) -> VerificationCommandResult:
    """Run one task-defined command without invoking a shell."""
    try:
        arguments = shlex.split(command)
    except ValueError as exc:
        return VerificationCommandResult(
            command=command,
            exit_code=2,
            stdout="",
            stderr=f"Invalid verification command: {exc}",
        )
    if not arguments:
        return VerificationCommandResult(
            command=command,
            exit_code=2,
            stdout="",
            stderr="Verification command is empty.",
        )

    try:
        process = await asyncio.create_subprocess_exec(
            *arguments,
            cwd=workspace,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except OSError as exc:
        return VerificationCommandResult(
            command=shlex.join(arguments),
            exit_code=127,
            stdout="",
            stderr=f"Could not start verification command: {exc}",
        )

    communication = asyncio.create_task(process.communicate())
    timed_out = False
    try:
        stdout_bytes, stderr_bytes = await asyncio.wait_for(
            asyncio.shield(communication),
            timeout=timeout_seconds,
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

    stderr = stderr_bytes.decode("utf-8", errors="replace")
    if timed_out:
        timeout_message = (
            f"Verification command timed out after {timeout_seconds:g} seconds."
        )
        stderr = f"{stderr.rstrip()}\n{timeout_message}".lstrip()
    return VerificationCommandResult(
        command=shlex.join(arguments),
        exit_code=process.returncode if process.returncode is not None else 124,
        stdout=stdout_bytes.decode("utf-8", errors="replace"),
        stderr=stderr,
        timed_out=timed_out,
    )


async def _with_verification_results(
    execution: AgentExecutionResult,
    task: TaskDefinition,
    workspace: Path,
    timeout_seconds: float,
    role: str,
    progress_callback: ProgressCallback | None,
) -> AgentExecutionResult:
    """Attach deterministic test and build outcomes to an agent result."""
    if task.verification is None:
        return execution

    metadata = dict(execution.metadata)
    commands_executed = list(execution.commands_executed)
    commands = (
        ("test", "tests_passed", task.verification.test_command),
        ("build", "build_passed", task.verification.build_command),
    )
    for prefix, passed_key, command in commands:
        if command is None:
            continue
        _notify_progress(
            progress_callback,
            f"Running {role} {prefix} verification",
        )
        outcome = await _run_verification_command(
            command,
            workspace,
            timeout_seconds,
        )
        commands_executed.append(outcome.command)
        metadata.update(
            {
                passed_key: outcome.passed,
                f"{prefix}_command": outcome.command,
                f"{prefix}_exit_code": outcome.exit_code,
                f"{prefix}_stdout": outcome.stdout,
                f"{prefix}_stderr": outcome.stderr,
                f"{prefix}_timed_out": outcome.timed_out,
            }
        )
        status = "passed" if outcome.passed else "failed"
        _notify_progress(
            progress_callback,
            f"{role.capitalize()} {prefix} verification {status}",
        )

    return execution.model_copy(
        update={
            "commands_executed": commands_executed,
            "metadata": metadata,
        }
    )


async def _execute_agent(
    adapter: AgentAdapter,
    task: TaskDefinition,
    workspace: DisposableWorkspace,
    verification_timeout_seconds: float,
    role: str,
    agent_name: str,
    progress_callback: ProgressCallback | None,
) -> tuple[AgentExecutionResult | None, str | None]:
    """Execute one adapter and turn operational failures into report data."""
    try:
        _notify_progress(
            progress_callback,
            f"Preparing {role} agent: {agent_name}",
        )
        await adapter.prepare(workspace.workspace_path)
        _notify_progress(
            progress_callback,
            f"Running {role} agent: {agent_name}",
        )
        result = await adapter.execute(task, workspace.workspace_path)
        _notify_progress(
            progress_callback,
            f"{role.capitalize()} agent finished; verifying its workspace",
        )
        result = await _with_verification_results(
            result,
            task,
            workspace.workspace_path,
            verification_timeout_seconds,
            role,
            progress_callback,
        )
    except Exception as exc:
        detail = str(exc) or "no error message was provided"
        _notify_progress(
            progress_callback,
            f"{role.capitalize()} agent failed: {detail}",
        )
        return None, f"{type(exc).__name__}: {detail}"
    exit_status = (
        "completed successfully"
        if result.exit_code == 0
        else f"exited with code {result.exit_code}"
    )
    _notify_progress(
        progress_callback,
        f"{role.capitalize()} agent {exit_status}",
    )
    return result, None


def _agent_run_result(
    *,
    agent_name: str,
    workspace: DisposableWorkspace,
    execution: AgentExecutionResult | None,
    error: str | None,
) -> AgentRunResult:
    """Collect one agent's execution outcome and final Git diff."""
    return AgentRunResult(
        agent_name=agent_name,
        workspace_path=workspace.workspace_path,
        execution=execution,
        error=error,
        diff=_workspace_changes(workspace.get_diff()),
    )


def _execution_evaluation(role: str, run: AgentRunResult) -> EvaluationResult:
    """Create a blocking finding for adapter exceptions and nonzero exits."""
    label = role.capitalize()
    if run.error is not None:
        summary = f"{label} agent failed to execute: {run.error}"
        evidence = [run.error]
        exit_code: int | None = None
    elif run.execution is None:
        summary = f"{label} agent did not produce an execution result."
        evidence = []
        exit_code = None
    elif run.execution.exit_code != 0:
        exit_code = run.execution.exit_code
        summary = f"{label} agent exited with code {exit_code}."
        evidence = [f"exit_code={exit_code}"]
    else:
        exit_code = run.execution.exit_code
        summary = f"{label} agent executed successfully."
        evidence = ["exit_code=0"]

    return EvaluationResult(
        name=f"{role}_execution",
        passed=run.succeeded,
        blocking=True,
        summary=summary,
        evidence=evidence,
        metadata={"agent_name": run.agent_name, "exit_code": exit_code},
    )


def _boolean_metadata(
    execution: AgentExecutionResult | None,
    key: str,
) -> bool | None:
    """Read a deterministic boolean observation from execution metadata."""
    if execution is None:
        return None
    value = execution.metadata.get(key)
    return value if isinstance(value, bool) else None


def _added_dependencies(execution: AgentExecutionResult | None) -> list[str]:
    """Read normalized dependency observations from execution metadata."""
    if execution is None:
        return []
    value = execution.metadata.get("added_dependencies")
    if not isinstance(value, list):
        return []
    return [dependency for dependency in value if isinstance(dependency, str)]


def _evaluate_candidate(
    contract: AgentContract,
    candidate: AgentRunResult,
) -> list[EvaluationResult]:
    """Apply all deterministic contract evaluators to the candidate run."""
    rules = contract.rules
    return [
        TestsMustPassEvaluator(rules.tests_must_pass).evaluate(
            _boolean_metadata(candidate.execution, "tests_passed")
        ),
        BuildMustPassEvaluator(rules.build_must_pass).evaluate(
            _boolean_metadata(candidate.execution, "build_passed")
        ),
        ForbiddenPathEvaluator(rules.forbidden_paths).evaluate(
            candidate.diff.changed_files
            + candidate.diff.added_files
            + candidate.diff.deleted_files
        ),
        RequiredPathEvaluator(rules.required_paths).evaluate(
            candidate.diff.changed_files
            + candidate.diff.added_files
            + candidate.diff.deleted_files
        ),
        ChangedFileCountEvaluator(rules.max_changed_files).evaluate(
            candidate.diff.changed_files
            + candidate.diff.added_files
            + candidate.diff.deleted_files
        ),
        ForbiddenDependencyEvaluator(rules.forbidden_dependencies).evaluate(
            _added_dependencies(candidate.execution)
        ),
    ]


class CompatibilityRunner:
    """Run baseline and candidate adapters in independent disposable workspaces."""

    def __init__(
        self,
        adapters: Mapping[str, AgentAdapter],
        *,
        verification_timeout_seconds: float = DEFAULT_VERIFICATION_TIMEOUT_SECONDS,
    ) -> None:
        if verification_timeout_seconds <= 0:
            raise ValueError("verification_timeout_seconds must be positive")
        self._adapters = dict(adapters)
        self.verification_timeout_seconds = verification_timeout_seconds

    def _adapter_for(self, agent_name: str) -> AgentAdapter:
        try:
            return self._adapters[agent_name]
        except KeyError as exc:
            raise RunnerConfigurationError(
                f"no adapter is configured for agent: {agent_name}"
            ) from exc

    async def run(
        self,
        *,
        contract_path: Path,
        task_path: Path,
        source_repository: Path,
        baseline_name: str | None = None,
        candidate_name: str | None = None,
        progress_callback: ProgressCallback | None = None,
    ) -> CompatibilityResult:
        """Execute one contract task for a baseline/candidate agent pair."""
        _notify_progress(progress_callback, "Loading contract and task")
        contract = load_contract(contract_path)
        task = load_task(task_path)
        selected_baseline = baseline_name or contract.baseline.agent
        if selected_baseline != contract.baseline.agent:
            raise RunnerConfigurationError(
                "baseline does not match the contract: "
                f"expected {contract.baseline.agent}, received {selected_baseline}"
            )
        selected_candidate = candidate_name or contract.candidates[0]
        if selected_candidate not in contract.candidates:
            raise RunnerConfigurationError(
                f"candidate is not declared by the contract: {selected_candidate}"
            )

        baseline_adapter = self._adapter_for(selected_baseline)
        candidate_adapter = self._adapter_for(selected_candidate)
        workspace_manager = WorkspaceManager(source_repository)

        _notify_progress(
            progress_callback,
            "Creating independent disposable workspaces",
        )
        with (
            workspace_manager.create_workspace() as baseline_workspace,
            workspace_manager.create_workspace() as candidate_workspace,
        ):
            try:
                baseline_execution, baseline_error = await _execute_agent(
                    baseline_adapter,
                    task,
                    baseline_workspace,
                    self.verification_timeout_seconds,
                    "baseline",
                    selected_baseline,
                    progress_callback,
                )
                candidate_execution, candidate_error = await _execute_agent(
                    candidate_adapter,
                    task,
                    candidate_workspace,
                    self.verification_timeout_seconds,
                    "candidate",
                    selected_candidate,
                    progress_callback,
                )
                _notify_progress(progress_callback, "Collecting Git changes")
                baseline = _agent_run_result(
                    agent_name=selected_baseline,
                    workspace=baseline_workspace,
                    execution=baseline_execution,
                    error=baseline_error,
                )
                candidate = _agent_run_result(
                    agent_name=selected_candidate,
                    workspace=candidate_workspace,
                    execution=candidate_execution,
                    error=candidate_error,
                )
                _notify_progress(
                    progress_callback,
                    "Evaluating deterministic compatibility rules",
                )
                evaluations = [
                    _execution_evaluation("baseline", baseline),
                    _execution_evaluation("candidate", candidate),
                    *_evaluate_candidate(contract, candidate),
                ]
                result = CompatibilityResult(
                    contract=contract,
                    task=task,
                    original_commit_sha=workspace_manager.original_commit_sha,
                    baseline=baseline,
                    candidate=candidate,
                    verdict=build_compatibility_verdict(evaluations),
                )
            finally:
                _notify_progress(
                    progress_callback,
                    "Cleaning up disposable workspaces",
                )

        _notify_progress(progress_callback, "Compatibility run complete")
        return result


async def run_compatibility(
    *,
    adapters: Mapping[str, AgentAdapter],
    contract_path: Path,
    task_path: Path,
    source_repository: Path,
    baseline_name: str | None = None,
    candidate_name: str | None = None,
    progress_callback: ProgressCallback | None = None,
) -> CompatibilityResult:
    """Run compatibility through the concise functional API."""
    runner = CompatibilityRunner(adapters)
    return await runner.run(
        contract_path=contract_path,
        task_path=task_path,
        source_repository=source_repository,
        baseline_name=baseline_name,
        candidate_name=candidate_name,
        progress_callback=progress_callback,
    )
