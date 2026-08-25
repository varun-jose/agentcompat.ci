"""Pydantic models used by AgentCompat CI."""

from datetime import datetime
from pathlib import Path, PurePosixPath
from typing import Annotated, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    field_validator,
    model_validator,
)

type NonEmptyString = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1),
]
type NonEmptyStringList = Annotated[list[NonEmptyString], Field(min_length=1)]
type DependencyName = Annotated[
    str,
    StringConstraints(
        strip_whitespace=True,
        min_length=1,
        pattern=r"^[A-Za-z0-9](?:[A-Za-z0-9._-]*[A-Za-z0-9])?$",
    ),
]


class ContractModel(BaseModel):
    """Base model for strict contract schema validation."""

    model_config = ConfigDict(extra="forbid", strict=True)


class ProjectConfig(ContractModel):
    """Project metadata covered by a compatibility contract."""

    name: NonEmptyString


class AgentConfig(ContractModel):
    """An agent selected for a compatibility run."""

    agent: NonEmptyString


def _repository_relative_file(value: str) -> str:
    path = PurePosixPath(value)
    if (
        "\\" in value
        or not path.parts
        or path.is_absolute()
        or value != path.as_posix()
        or any(part in {"", ".", ".."} for part in path.parts)
        or any(character in value for character in "*?[]")
        or any(ord(character) < 32 or ord(character) == 127 for character in value)
    ):
        raise ValueError("must be a normalized repository-relative file path")
    return value


class DependencyManifestRule(ContractModel):
    """Explicit pip manifest entrypoint and its invocation role."""

    path: NonEmptyString
    role: Literal["requirement", "constraint"]

    @field_validator("path")
    @classmethod
    def validate_path(cls, value: str) -> str:
        """Reject paths that are ambiguous or can escape a workspace."""
        return _repository_relative_file(value)


class DependencyDriftRules(ContractModel):
    """Opt-in rules for deterministic dependency-state drift."""

    removals_forbidden: bool = False
    versions_must_not_change: bool = False
    sources_must_not_change: bool = False
    lockfiles_must_not_change: bool = False
    lockfiles_must_be_updated_for_dependency_changes: bool = False
    lockfiles: list[NonEmptyString] = Field(default_factory=list, max_length=20)

    @field_validator("lockfiles")
    @classmethod
    def validate_lockfiles(cls, values: list[str]) -> list[str]:
        """Require explicit lockfiles to remain inside the repository."""
        return [_repository_relative_file(value) for value in values]

    @model_validator(mode="after")
    def validate_update_rule(self) -> "DependencyDriftRules":
        """A co-update rule needs an explicit expected lockfile set."""
        if self.lockfiles_must_be_updated_for_dependency_changes and not self.lockfiles:
            raise ValueError(
                "lockfiles must be configured when lockfile updates are required"
            )
        return self


class TaskDependencyDriftRules(ContractModel):
    """Task-level dependency rules that can only tighten the contract."""

    removals_forbidden: bool | None = None
    versions_must_not_change: bool | None = None
    sources_must_not_change: bool | None = None
    lockfiles_must_not_change: bool | None = None
    lockfiles_must_be_updated_for_dependency_changes: bool | None = None
    lockfiles: list[NonEmptyString] = Field(default_factory=list, max_length=20)

    @field_validator("lockfiles")
    @classmethod
    def validate_lockfiles(cls, values: list[str]) -> list[str]:
        """Require task lockfiles to use safe repository paths."""
        return [_repository_relative_file(value) for value in values]


def _repository_relative_pattern(value: str) -> str:
    """Validate a repository-relative glob without interpreting it."""
    path_without_trailing_slash = value[:-1] if value.endswith("/") else value
    parts = path_without_trailing_slash.split("/")
    if (
        "\\" in value
        or not path_without_trailing_slash
        or value.startswith("/")
        or any(part in {"", ".", ".."} for part in parts)
        or any(ord(character) < 32 or ord(character) == 127 for character in value)
    ):
        raise ValueError("must be a normalized repository-relative path pattern")
    return value


class ChangedFileRules(ContractModel):
    """Repository policy for the changed-file budget."""

    max: int | None = Field(default=None, gt=0)
    include_ignored: bool = False
    exclude: list[NonEmptyString] = Field(default_factory=list, max_length=100)

    @field_validator("exclude")
    @classmethod
    def validate_exclusions(cls, values: list[str]) -> list[str]:
        """Require deterministic repository-relative exclusion patterns."""
        return list(
            dict.fromkeys(_repository_relative_pattern(value) for value in values)
        )


class TaskChangedFileRules(ContractModel):
    """Task changed-file policy that may only tighten repository rules."""

    max: int | None = Field(default=None, gt=0)
    include_ignored: bool | None = None
    exclude: list[NonEmptyString] | None = Field(default=None, max_length=100)

    @field_validator("exclude")
    @classmethod
    def validate_exclusions(cls, values: list[str] | None) -> list[str] | None:
        """Require deterministic repository-relative exclusion patterns."""
        if values is None:
            return None
        return list(
            dict.fromkeys(_repository_relative_pattern(value) for value in values)
        )


def _validate_manifest_roles(
    manifests: list[DependencyManifestRule],
) -> list[DependencyManifestRule]:
    roles: dict[str, str] = {}
    for manifest in manifests:
        previous_role = roles.setdefault(manifest.path, manifest.role)
        if previous_role != manifest.role:
            raise ValueError(
                f"dependency manifest {manifest.path!r} has conflicting roles"
            )
    return manifests


class ContractRules(ContractModel):
    """Deterministic engineering rules enforced by a contract."""

    tests_must_pass: bool
    build_must_pass: bool
    forbidden_paths: list[NonEmptyString]
    required_paths: list[NonEmptyString]
    max_changed_files: int | None = Field(default=None, gt=0)
    changed_files: ChangedFileRules = Field(default_factory=ChangedFileRules)
    forbidden_dependencies: list[DependencyName] = Field(max_length=50_000)
    dependency_manifests: list[DependencyManifestRule] = Field(
        default_factory=list,
        max_length=100,
    )
    dependency_drift: DependencyDriftRules = Field(default_factory=DependencyDriftRules)

    @field_validator("dependency_manifests")
    @classmethod
    def validate_dependency_manifests(
        cls,
        manifests: list[DependencyManifestRule],
    ) -> list[DependencyManifestRule]:
        """Reject one pip file being assigned incompatible root roles."""
        return _validate_manifest_roles(manifests)

    @model_validator(mode="after")
    def normalize_changed_file_maximum(self) -> "ContractRules":
        """Keep the legacy scalar and nested policy backward compatible."""
        legacy_maximum = self.max_changed_files
        nested_maximum = self.changed_files.max
        if (
            legacy_maximum is not None
            and nested_maximum is not None
            and legacy_maximum != nested_maximum
        ):
            raise ValueError(
                "max_changed_files and changed_files.max must be equal when both "
                "are configured"
            )
        effective_maximum = (
            nested_maximum if nested_maximum is not None else legacy_maximum
        )
        self.max_changed_files = effective_maximum
        self.changed_files.max = effective_maximum
        return self


class TaskRules(ContractModel):
    """Optional task rules that may tighten the repository contract."""

    tests_must_pass: bool | None = None
    build_must_pass: bool | None = None
    forbidden_paths: list[NonEmptyString] = Field(default_factory=list)
    required_paths: list[NonEmptyString] = Field(default_factory=list)
    max_changed_files: int | None = Field(default=None, gt=0)
    changed_files: TaskChangedFileRules | None = None
    forbidden_dependencies: list[DependencyName] = Field(
        default_factory=list,
        max_length=50_000,
    )
    dependency_manifests: list[DependencyManifestRule] = Field(
        default_factory=list,
        max_length=100,
    )
    dependency_drift: TaskDependencyDriftRules | None = None

    @field_validator("dependency_manifests")
    @classmethod
    def validate_dependency_manifests(
        cls,
        manifests: list[DependencyManifestRule],
    ) -> list[DependencyManifestRule]:
        """Reject conflicting task-level pip manifest roles."""
        return _validate_manifest_roles(manifests)

    @model_validator(mode="after")
    def normalize_changed_file_maximum(self) -> "TaskRules":
        """Normalize legacy and nested task maximums to one value."""
        nested_rules = self.changed_files
        nested_maximum = nested_rules.max if nested_rules is not None else None
        if (
            self.max_changed_files is not None
            and nested_maximum is not None
            and self.max_changed_files != nested_maximum
        ):
            raise ValueError(
                "max_changed_files and changed_files.max must be equal when both "
                "are configured"
            )
        effective_maximum = (
            nested_maximum
            if nested_maximum is not None
            else self.max_changed_files
        )
        if nested_rules is None and effective_maximum is not None:
            nested_rules = TaskChangedFileRules(max=effective_maximum)
            self.changed_files = nested_rules
        elif nested_rules is not None:
            nested_rules.max = effective_maximum
        self.max_changed_files = effective_maximum
        return self


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
    rules: TaskRules | None = None


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
    ignored_files: list[str] = Field(default_factory=list)


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
