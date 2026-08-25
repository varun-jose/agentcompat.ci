"""Deterministic compatibility evaluators."""

from collections.abc import Iterable, Sequence
from fnmatch import fnmatchcase

from agentcompat.models import CompatibilityVerdict, EvaluationResult
from agentcompat.workspace import WorkspaceDiff

type ChangedPaths = WorkspaceDiff | Iterable[str]


def _normalize_path(path: str) -> str:
    """Normalize a repository-relative path for cross-platform matching."""
    normalized = path.replace("\\", "/")
    while normalized.startswith("./"):
        normalized = normalized[2:]
    return normalized


def _collect_changed_paths(paths: ChangedPaths) -> list[str]:
    """Collect unique modified, added, and deleted repository paths."""
    if isinstance(paths, WorkspaceDiff):
        values = paths.changed_files + paths.added_files + paths.deleted_files
    elif isinstance(paths, str):
        values = [paths]
    else:
        values = list(paths)
    return sorted({_normalize_path(path) for path in values})


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
    """Require every configured path pattern to match a changed file."""

    name = "required_paths"

    def __init__(self, required_paths: Sequence[str] = ()) -> None:
        self.required_paths = tuple(required_paths)

    def evaluate(self, changed_paths: ChangedPaths) -> EvaluationResult:
        """Evaluate whether each required pattern has a changed path."""
        paths = _collect_changed_paths(changed_paths)
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

    def __init__(self, max_changed_files: int | None = None) -> None:
        if max_changed_files is not None and max_changed_files <= 0:
            raise ValueError("max_changed_files must be positive")
        self.max_changed_files = max_changed_files

    def evaluate(self, changed_paths: ChangedPaths) -> EvaluationResult:
        """Count unique changed paths and compare them with the configured limit."""
        paths = _collect_changed_paths(changed_paths)
        changed_count = len(paths)
        configured = self.max_changed_files is not None
        if self.max_changed_files is None:
            passed = True
            summary = f"Changed {changed_count} file(s); no maximum is configured."
        else:
            passed = changed_count <= self.max_changed_files
            summary = (
                f"Changed {changed_count} file(s); "
                f"the maximum is {self.max_changed_files}."
            )
        return EvaluationResult(
            name=self.name,
            passed=passed,
            blocking=configured,
            summary=summary,
            evidence=paths,
            metadata={
                "changed_file_count": changed_count,
                "max_changed_files": self.max_changed_files,
            },
        )


class ForbiddenDependencyEvaluator:
    """Reject newly added dependencies forbidden by the contract."""

    name = "forbidden_dependencies"

    def __init__(self, forbidden_dependencies: Sequence[str] = ()) -> None:
        self.forbidden_dependencies = tuple(forbidden_dependencies)

    def evaluate(self, added_dependencies: Iterable[str]) -> EvaluationResult:
        """Compare added dependency names case-insensitively."""
        dependencies = sorted(set(added_dependencies), key=str.casefold)
        forbidden_names = {
            dependency.casefold() for dependency in self.forbidden_dependencies
        }
        violations = [
            dependency
            for dependency in dependencies
            if dependency.casefold() in forbidden_names
        ]
        configured = bool(self.forbidden_dependencies)
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
            },
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
