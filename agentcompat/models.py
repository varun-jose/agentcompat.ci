"""Pydantic models used by AgentCompat CI."""

from datetime import datetime
from pathlib import Path
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

type NonEmptyString = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1),
]
type NonEmptyStringList = Annotated[list[NonEmptyString], Field(min_length=1)]


class ContractModel(BaseModel):
    """Base model for strict contract schema validation."""

    model_config = ConfigDict(extra="forbid", strict=True)


class ProjectConfig(ContractModel):
    """Project metadata covered by a compatibility contract."""

    name: NonEmptyString


class AgentConfig(ContractModel):
    """An agent selected for a compatibility run."""

    agent: NonEmptyString


class ContractRules(ContractModel):
    """Deterministic engineering rules enforced by a contract."""

    tests_must_pass: bool
    build_must_pass: bool
    forbidden_paths: list[NonEmptyString]
    required_paths: list[NonEmptyString]
    max_changed_files: int | None = Field(default=None, gt=0)
    forbidden_dependencies: list[NonEmptyString]


class AgentContract(ContractModel):
    """Complete AgentCompat CI compatibility contract."""

    version: int
    project: ProjectConfig
    baseline: AgentConfig
    candidates: NonEmptyStringList
    rules: ContractRules
    tasks: NonEmptyStringList


class TaskVerification(ContractModel):
    """Deterministic commands executed after an agent finishes its task."""

    test_command: NonEmptyString | None = None
    build_command: NonEmptyString | None = None


class TaskDefinition(BaseModel):
    """Task payload passed through to an agent adapter.

    Additional fields remain extensible until the full task-file schema is defined.
    """

    model_config = ConfigDict(extra="allow", strict=True)

    prompt: NonEmptyString
    verification: TaskVerification | None = None


class AgentExecutionResult(BaseModel):
    """Structured result returned by an agent adapter execution."""

    model_config = ConfigDict(extra="forbid", strict=True)

    agent_name: NonEmptyString
    started_at: datetime
    completed_at: datetime
    exit_code: int
    stdout: str
    stderr: str
    commands_executed: list[str] = Field(default_factory=list)
    estimated_cost: float | None = None
    metadata: dict[str, object] = Field(default_factory=dict)


class WorkspaceChanges(BaseModel):
    """Serializable Git changes collected after an agent run."""

    model_config = ConfigDict(extra="forbid", strict=True)

    changed_files: list[str] = Field(default_factory=list)
    added_files: list[str] = Field(default_factory=list)
    deleted_files: list[str] = Field(default_factory=list)


class AgentRunResult(BaseModel):
    """Execution outcome and repository changes for one isolated agent run."""

    model_config = ConfigDict(extra="forbid", strict=True)

    agent_name: NonEmptyString
    workspace_path: Path
    execution: AgentExecutionResult | None = None
    error: str | None = None
    diff: WorkspaceChanges

    @property
    def succeeded(self) -> bool:
        """Return whether the adapter completed with a successful exit code."""
        return (
            self.error is None
            and self.execution is not None
            and self.execution.exit_code == 0
        )


class EvaluationResult(BaseModel):
    """Structured deterministic finding produced by one evaluator."""

    model_config = ConfigDict(extra="forbid", strict=True)

    name: NonEmptyString
    passed: bool
    blocking: bool
    summary: NonEmptyString
    evidence: list[str] = Field(default_factory=list)
    metadata: dict[str, object] = Field(default_factory=dict)


class CompatibilityVerdict(BaseModel):
    """Aggregate verdict derived from evaluator findings."""

    model_config = ConfigDict(extra="forbid", strict=True)

    passed: bool
    blocking_failures: list[str] = Field(default_factory=list)
    results: list[EvaluationResult] = Field(default_factory=list)


class CompatibilityResult(BaseModel):
    """Complete report produced by one baseline/candidate compatibility run."""

    model_config = ConfigDict(extra="forbid", strict=True)

    contract: AgentContract
    task: TaskDefinition
    original_commit_sha: NonEmptyString
    baseline: AgentRunResult
    candidate: AgentRunResult
    verdict: CompatibilityVerdict

    @property
    def passed(self) -> bool:
        """Expose the final verdict directly for report consumers."""
        return self.verdict.passed
