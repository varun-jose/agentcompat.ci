"""Deterministic compatibility evaluators."""

from collections.abc import Iterable, Sequence
from fnmatch import fnmatchcase

from agentcompat.dependencies import canonicalize_dependency_name
from agentcompat.models import (
    CompatibilityVerdict,
    EvaluationResult,
    WorkspaceChanges,
)
from agentcompat.workspace import WorkspaceDiff

type StructuredChanges = WorkspaceDiff | WorkspaceChanges
type ChangedPaths = StructuredChanges | Iterable[str]


def _normalize_path(path: str) -> str:
    """Normalize a repository-relative path for cross-platform matching."""
    normalized = path.replace("\\", "/")
    while normalized.startswith("./"):
        normalized = normalized[2:]
    return normalized


def _collect_changed_paths(paths: ChangedPaths) -> list[str]:
    """Collect all reported paths, including visible Git-ignored artifacts."""
    if isinstance(paths, (WorkspaceDiff, WorkspaceChanges)):
        values = (
            paths.changed_files
            + paths.added_files
            + paths.deleted_files
            + paths.ignored_files
        )
    elif isinstance(paths, str):
        values = [paths]
    else:
        values = list(paths)
    return sorted({_normalize_path(path) for path in values})


def _collect_present_changed_paths(paths: ChangedPaths) -> list[str]:
    """Collect changed paths that still exist in the final workspace."""
    if isinstance(paths, (WorkspaceDiff, WorkspaceChanges)):
        values = paths.changed_files + paths.added_files
        return sorted({_normalize_path(path) for path in values})
    return _collect_changed_paths(paths)


def _collect_budget_paths(paths: ChangedPaths) -> tuple[list[str], list[str]]:
    """Separate ordinary Git changes from untracked Git-ignored artifacts."""
    if isinstance(paths, (WorkspaceDiff, WorkspaceChanges)):
        ordinary = (
            paths.changed_files + paths.added_files + paths.deleted_files
        )
        ignored = paths.ignored_files
        return (
            sorted({_normalize_path(path) for path in ordinary}),
            sorted({_normalize_path(path) for path in ignored}),
        )
    return _collect_changed_paths(paths), []


def _path_matches(path: str, pattern: str) -> bool:
    """Match a repository path against an exact, directory, or glob pattern."""
    normalized_path = _normalize_path(path)
    normalized_pattern = _normalize_path(pattern)
    if normalized_pattern.endswith("/"):
        return normalized_path.startswith(normalized_pattern)
    return fnmatchcase(normalized_path, normalized_pattern)


def _command_result(
    *,
    name: str,
    label: str,
    must_pass: bool,
    passed: bool | None,
) -> EvaluationResult:
    """Evaluate one required deterministic command outcome."""
    if not must_pass:
        return EvaluationResult(
            name=name,
            passed=True,
            blocking=False,
            summary=f"{label} success is not required by the contract.",
            evidence=[] if passed is None else [f"passed={passed}"],
            metadata={"required": False, "observed_passed": passed},
        )

    if passed is None:
        summary = f"{label} did not produce a result."
    elif passed:
        summary = f"{label} passed."
    else:
        summary = f"{label} failed."

    return EvaluationResult(
        name=name,
        passed=passed is True,
        blocking=True,
        summary=summary,
        evidence=[] if passed is None else [f"passed={passed}"],
        metadata={"required": True, "observed_passed": passed},
    )


class TestsMustPassEvaluator:
    """Require the test command to report success when configured."""

    name = "tests_must_pass"

    def __init__(self, must_pass: bool = True) -> None:
        self.must_pass = must_pass

    def evaluate(self, passed: bool | None) -> EvaluationResult:
        """Evaluate the observed test outcome."""
        return _command_result(
            name=self.name,
            label="Tests",
            must_pass=self.must_pass,
            passed=passed,
        )


class BuildMustPassEvaluator:
    """Require the build command to report success when configured."""

    name = "build_must_pass"

    def __init__(self, must_pass: bool = True) -> None:
        self.must_pass = must_pass

    def evaluate(self, passed: bool | None) -> EvaluationResult:
        """Evaluate the observed build outcome."""
        return _command_result(
            name=self.name,
            label="Build",
            must_pass=self.must_pass,
            passed=passed,
        )


class ForbiddenPathEvaluator:
    """Reject changes matching configured forbidden path patterns."""

    name = "forbidden_paths"

    def __init__(self, forbidden_paths: Sequence[str] = ()) -> None:
        self.forbidden_paths = tuple(forbidden_paths)

    def evaluate(self, changed_paths: ChangedPaths) -> EvaluationResult:
        """Evaluate all changed paths against exact and glob patterns."""
        paths = _collect_changed_paths(changed_paths)
        violations = sorted(
            path
            for path in paths
            if any(_path_matches(path, pattern) for pattern in self.forbidden_paths)
        )
        configured = bool(self.forbidden_paths)
        passed = not violations
        summary = (
            f"Found {len(violations)} forbidden path change(s)."
            if violations
            else "No changed files match forbidden path rules."
        )
        return EvaluationResult(
            name=self.name,
            passed=passed,
            blocking=configured,
            summary=summary,
            evidence=violations,
            metadata={
                "forbidden_patterns": list(self.forbidden_paths),
                "checked_path_count": len(paths),
            },
        )


class RequiredPathEvaluator:
    """Require every configured pattern to match a surviving changed file."""

    name = "required_paths"

    def __init__(self, required_paths: Sequence[str] = ()) -> None:
        self.required_paths = tuple(required_paths)

    def evaluate(self, changed_paths: ChangedPaths) -> EvaluationResult:
        """Evaluate whether each required pattern has a present changed path."""
        paths = _collect_present_changed_paths(changed_paths)
        matches = {
            pattern: [path for path in paths if _path_matches(path, pattern)]
            for pattern in self.required_paths
        }
        missing_patterns = sorted(
            pattern for pattern, matched_paths in matches.items() if not matched_paths
        )
        evidence = [
            f"{pattern} -> {path}"
            for pattern, matched_paths in matches.items()
            for path in matched_paths
        ]
        passed = not missing_patterns
        summary = (
            f"Missing {len(missing_patterns)} required path pattern(s)."
            if missing_patterns
            else "All required path patterns matched changed files."
        )
        return EvaluationResult(
            name=self.name,
            passed=passed,
            blocking=bool(self.required_paths),
            summary=summary,
            evidence=evidence,
            metadata={
                "required_patterns": list(self.required_paths),
                "missing_patterns": missing_patterns,
            },
        )


class ChangedFileCountEvaluator:
    """Enforce the maximum number of unique changed files."""

    name = "max_changed_files"

    def __init__(
        self,
        max_changed_files: int | None = None,
        *,
        include_ignored: bool = False,
        exclude: Sequence[str] = (),
        contract_max_changed_files: int | None = None,
        task_max_changed_files: int | None = None,
    ) -> None:
        if max_changed_files is not None and max_changed_files <= 0:
            raise ValueError("max_changed_files must be positive")
        self.max_changed_files = max_changed_files
        self.include_ignored = include_ignored
        self.exclude = tuple(exclude)
        self.contract_max_changed_files = contract_max_changed_files
        self.task_max_changed_files = task_max_changed_files

    def evaluate(self, changed_paths: ChangedPaths) -> EvaluationResult:
        """Count policy-eligible paths and compare them with the effective limit."""
        ordinary_paths, ignored_paths = _collect_budget_paths(changed_paths)
        eligible_paths = set(ordinary_paths)
        if self.include_ignored:
            eligible_paths.update(ignored_paths)
        excluded_paths = sorted(
            path
            for path in eligible_paths
            if any(_path_matches(path, pattern) for pattern in self.exclude)
        )
        paths = sorted(eligible_paths - set(excluded_paths))
        changed_count = len(paths)
        configured = self.max_changed_files is not None
        if self.max_changed_files is None:
            passed = True
            summary = (
                f"Counted {changed_count} changed file(s); no maximum is configured."
            )
        else:
            passed = changed_count <= self.max_changed_files
            summary = (
                f"Counted {changed_count} changed file(s); "
                f"the effective maximum is {self.max_changed_files}."
            )

        policy_notes: list[str] = []
        if ignored_paths and not self.include_ignored:
            policy_notes.append(
                f"{len(ignored_paths)} Git-ignored artifact(s) were not counted"
            )
        if excluded_paths:
            policy_notes.append(
                f"{len(excluded_paths)} path(s) matched exclusion rules"
            )
        if policy_notes:
            summary = f"{summary} {'; '.join(policy_notes)}."

        return EvaluationResult(
            name=self.name,
            passed=passed,
            blocking=configured,
            summary=summary,
            evidence=paths,
            metadata={
                "changed_file_count": changed_count,
                "max_changed_files": self.max_changed_files,
                "contract_max_changed_files": self.contract_max_changed_files,
                "task_max_changed_files": self.task_max_changed_files,
                "include_ignored": self.include_ignored,
                "exclude_patterns": list(self.exclude),
                "excluded_paths": excluded_paths,
                "excluded_path_count": len(excluded_paths),
                "observed_file_count": len(set(ordinary_paths + ignored_paths)),
                "observed_ignored_file_count": len(ignored_paths),
                "ignored_file_counted": (
                    len(set(ignored_paths) & set(paths))
                    if self.include_ignored
                    else 0
                ),
            },
        )


class ForbiddenDependencyEvaluator:
    """Reject newly added dependencies forbidden by the contract."""

    name = "forbidden_dependencies"

    def __init__(self, forbidden_dependencies: Sequence[str] = ()) -> None:
        self.forbidden_dependencies = tuple(forbidden_dependencies)

    def evaluate(
        self,
        added_dependencies: Iterable[str] | None,
        *,
        observation_error: str | None = None,
    ) -> EvaluationResult:
        """Compare added dependency names case-insensitively."""
        configured = bool(self.forbidden_dependencies)
        if added_dependencies is None:
            return EvaluationResult(
                name=self.name,
                passed=not configured,
                blocking=configured,
                summary="Dependency changes could not be determined.",
                evidence=[] if observation_error is None else [observation_error],
                metadata={
                    "forbidden_dependencies": list(self.forbidden_dependencies),
                    "added_dependencies": None,
                    "observation_available": False,
                    "observation_error": observation_error,
                },
            )

        dependencies_by_name: dict[str, str] = {}
        for dependency in added_dependencies:
            canonical_name = canonicalize_dependency_name(dependency)
            dependencies_by_name.setdefault(canonical_name, dependency)
        dependencies = [
            dependencies_by_name[name] for name in sorted(dependencies_by_name)
        ]
        forbidden_names = {
            canonicalize_dependency_name(dependency)
            for dependency in self.forbidden_dependencies
        }
        violations = [
            dependency
            for dependency in dependencies
            if canonicalize_dependency_name(dependency) in forbidden_names
        ]
        passed = not violations
        summary = (
            f"Found {len(violations)} forbidden dependency change(s)."
            if violations
            else "No forbidden dependencies were added."
        )
        return EvaluationResult(
            name=self.name,
            passed=passed,
            blocking=configured,
            summary=summary,
            evidence=violations,
            metadata={
                "forbidden_dependencies": list(self.forbidden_dependencies),
                "added_dependencies": dependencies,
                "observation_available": True,
                "observation_error": None,
            },
        )


def _dependency_drift_result(
    *,
    name: str,
    label: str,
    must_not_change: bool,
    changes: Iterable[str] | None,
    observation_error: str | None,
    total_count: int | None = None,
    missing_changes: bool = False,
) -> EvaluationResult:
    configured = must_not_change
    if changes is None:
        return EvaluationResult(
            name=name,
            passed=not configured,
            blocking=configured,
            summary=f"{label} could not be determined.",
            evidence=[] if observation_error is None else [observation_error],
            metadata={
                "required": configured,
                "observation_available": False,
                "observation_error": observation_error,
                "changes": None,
            },
        )
    observed_changes = sorted(set(changes))
    count = max(len(observed_changes), total_count or 0)
    passed = not configured or not observed_changes
    if observed_changes:
        summary = (
            f"Missing {count} required dependency lockfile update(s)."
            if missing_changes
            else f"Found {count} {label.lower()}."
        )
    else:
        summary = (
            "No required dependency lockfile updates are missing."
            if missing_changes
            else f"No {label.lower()} were observed."
        )
    return EvaluationResult(
        name=name,
        passed=passed,
        blocking=configured,
        summary=summary,
        evidence=observed_changes,
        metadata={
            "required": configured,
            "observation_available": True,
            "observation_error": None,
            "changes": observed_changes,
            "change_count": count,
            "truncated": count > len(observed_changes),
        },
    )


class DependencyRemovalEvaluator:
    """Optionally reject removal of direct dependency declarations."""

    name = "dependency_removals"

    def __init__(self, removals_forbidden: bool = False) -> None:
        self.removals_forbidden = removals_forbidden

    def evaluate(
        self,
        removed_dependencies: Iterable[str] | None,
        *,
        observation_error: str | None = None,
        total_count: int | None = None,
    ) -> EvaluationResult:
        """Evaluate direct dependency removals."""
        return _dependency_drift_result(
            name=self.name,
            label="Dependency removals",
            must_not_change=self.removals_forbidden,
            changes=removed_dependencies,
            observation_error=observation_error,
            total_count=total_count,
        )


class DependencyVersionChangeEvaluator:
    """Optionally reject normalized declaration and lock version drift."""

    name = "dependency_version_changes"

    def __init__(self, versions_must_not_change: bool = False) -> None:
        self.versions_must_not_change = versions_must_not_change

    def evaluate(
        self,
        changes: Iterable[str] | None,
        *,
        observation_error: str | None = None,
        total_count: int | None = None,
    ) -> EvaluationResult:
        """Evaluate context-aware version changes."""
        return _dependency_drift_result(
            name=self.name,
            label="Dependency version changes",
            must_not_change=self.versions_must_not_change,
            changes=changes,
            observation_error=observation_error,
            total_count=total_count,
        )


class DependencySourceChangeEvaluator:
    """Optionally reject hashed source-identity drift."""

    name = "dependency_source_changes"

    def __init__(self, sources_must_not_change: bool = False) -> None:
        self.sources_must_not_change = sources_must_not_change

    def evaluate(
        self,
        changes: Iterable[str] | None,
        *,
        observation_error: str | None = None,
        total_count: int | None = None,
    ) -> EvaluationResult:
        """Evaluate source changes without exposing source credentials."""
        return _dependency_drift_result(
            name=self.name,
            label="Dependency source changes",
            must_not_change=self.sources_must_not_change,
            changes=changes,
            observation_error=observation_error,
            total_count=total_count,
        )


class DependencyLockfileChangeEvaluator:
    """Optionally reject exact supported/configured lockfile changes."""

    name = "dependency_lockfile_changes"

    def __init__(self, lockfiles_must_not_change: bool = False) -> None:
        self.lockfiles_must_not_change = lockfiles_must_not_change

    def evaluate(
        self,
        changes: Iterable[str] | None,
        *,
        observation_error: str | None = None,
        total_count: int | None = None,
    ) -> EvaluationResult:
        """Evaluate lockfile additions, removals, and modifications."""
        return _dependency_drift_result(
            name=self.name,
            label="Dependency lockfile changes",
            must_not_change=self.lockfiles_must_not_change,
            changes=changes,
            observation_error=observation_error,
            total_count=total_count,
        )


class DependencyLockfileUpdateEvaluator:
    """Require configured lockfiles to accompany resolver-input drift."""

    name = "dependency_lockfile_updates"

    def __init__(self, updates_required: bool = False) -> None:
        self.updates_required = updates_required

    def evaluate(
        self,
        missing_updates: Iterable[str] | None,
        *,
        observation_error: str | None = None,
        total_count: int | None = None,
    ) -> EvaluationResult:
        """Evaluate deterministic resolver-input/lockfile co-update evidence."""
        return _dependency_drift_result(
            name=self.name,
            label="Required dependency lockfile updates",
            must_not_change=self.updates_required,
            changes=missing_updates,
            observation_error=observation_error,
            total_count=total_count,
            missing_changes=True,
        )


def build_compatibility_verdict(
    results: Sequence[EvaluationResult],
) -> CompatibilityVerdict:
    """Fail compatibility whenever at least one blocking evaluator fails."""
    result_list = list(results)
    blocking_failures = [
        result.name for result in result_list if result.blocking and not result.passed
    ]
    return CompatibilityVerdict(
        passed=not blocking_failures,
        blocking_failures=blocking_failures,
        results=result_list,
    )
