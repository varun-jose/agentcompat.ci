"""Unit tests for deterministic compatibility evaluators."""

import pytest

from agentcompat.evaluators import (
    BuildMustPassEvaluator,
    ChangedFileCountEvaluator,
    ForbiddenDependencyEvaluator,
    ForbiddenPathEvaluator,
    RequiredPathEvaluator,
    build_compatibility_verdict,
)
from agentcompat.evaluators import (
    TestsMustPassEvaluator as MustPassTestsEvaluator,
)
from agentcompat.models import EvaluationResult
from agentcompat.workspace import WorkspaceDiff


def workspace_diff(
    *,
    changed: list[str] | None = None,
    added: list[str] | None = None,
    deleted: list[str] | None = None,
) -> WorkspaceDiff:
    """Build a workspace diff with concise defaults."""
    return WorkspaceDiff(
        changed_files=changed or [],
        added_files=added or [],
        deleted_files=deleted or [],
    )


@pytest.mark.parametrize("passed", [False, None])
def test_tests_must_pass_evaluator_blocks_failure(passed: bool | None) -> None:
    result = MustPassTestsEvaluator(must_pass=True).evaluate(passed)

    assert result.name == "tests_must_pass"
    assert result.passed is False
    assert result.blocking is True
    assert "Tests" in result.summary
    assert result.metadata["required"] is True


def test_tests_must_pass_evaluator_accepts_success() -> None:
    result = MustPassTestsEvaluator().evaluate(True)

    assert result.passed is True
    assert result.blocking is True
    assert result.evidence == ["passed=True"]


def test_tests_must_pass_evaluator_skips_disabled_rule() -> None:
    result = MustPassTestsEvaluator(must_pass=False).evaluate(False)

    assert result.passed is True
    assert result.blocking is False
    assert "not required" in result.summary


def test_build_must_pass_evaluator_reports_success_and_failure() -> None:
    evaluator = BuildMustPassEvaluator()

    passing = evaluator.evaluate(True)
    failing = evaluator.evaluate(False)

    assert passing.name == "build_must_pass"
    assert passing.passed is True
    assert passing.blocking is True
    assert failing.passed is False
    assert failing.blocking is True
    assert failing.evidence == ["passed=False"]


def test_forbidden_path_evaluator_supports_recursive_globs() -> None:
    evaluator = ForbiddenPathEvaluator(
        [".github/workflows/**", "infra/production/**"]
    )
    changes = workspace_diff(
        changed=["src/app.py", ".github/workflows/ci.yml"],
        added=["infra/production/eu/service.yml"],
    )

    result = evaluator.evaluate(changes)

    assert result.name == "forbidden_paths"
    assert result.passed is False
    assert result.blocking is True
    assert result.evidence == [
        ".github/workflows/ci.yml",
        "infra/production/eu/service.yml",
    ]
    assert result.metadata["checked_path_count"] == 3


def test_forbidden_path_evaluator_passes_clean_changes() -> None:
    result = ForbiddenPathEvaluator(["secrets/**"]).evaluate(["src/app.py"])

    assert result.passed is True
    assert result.blocking is True
    assert result.evidence == []


def test_required_path_evaluator_requires_every_pattern() -> None:
    evaluator = RequiredPathEvaluator(["src/orders/**", "tests/**"])
    result = evaluator.evaluate(["src/orders/api.py"])

    assert result.name == "required_paths"
    assert result.passed is False
    assert result.blocking is True
    assert result.metadata["missing_patterns"] == ["tests/**"]
    assert result.evidence == ["src/orders/** -> src/orders/api.py"]


def test_required_path_evaluator_passes_when_all_patterns_match() -> None:
    evaluator = RequiredPathEvaluator(["src/orders/**", "tests/"])
    result = evaluator.evaluate(
        workspace_diff(
            changed=["src/orders/api.py"],
            added=["tests/test_orders.py"],
        )
    )

    assert result.passed is True
    assert result.blocking is True
    assert result.metadata["missing_patterns"] == []


def test_changed_file_count_evaluator_enforces_unique_file_limit() -> None:
    evaluator = ChangedFileCountEvaluator(max_changed_files=2)
    result = evaluator.evaluate(
        workspace_diff(
            changed=["src/app.py"],
            added=["tests/test_app.py"],
            deleted=["src/app.py"],
        )
    )

    assert result.name == "max_changed_files"
    assert result.passed is True
    assert result.blocking is True
    assert result.metadata["changed_file_count"] == 2


def test_changed_file_count_evaluator_blocks_excess_changes() -> None:
    result = ChangedFileCountEvaluator(max_changed_files=1).evaluate(
        ["one.py", "two.py"]
    )

    assert result.passed is False
    assert result.blocking is True
    assert result.evidence == ["one.py", "two.py"]


def test_changed_file_count_evaluator_allows_unconfigured_limit() -> None:
    result = ChangedFileCountEvaluator().evaluate(["one.py", "two.py"])

    assert result.passed is True
    assert result.blocking is False


def test_changed_file_count_evaluator_rejects_invalid_limit() -> None:
    with pytest.raises(ValueError, match="must be positive"):
        ChangedFileCountEvaluator(max_changed_files=0)


def test_forbidden_dependency_evaluator_is_case_insensitive() -> None:
    evaluator = ForbiddenDependencyEvaluator(["requests", "unsafe-package"])
    result = evaluator.evaluate(["pydantic", "Requests"])

    assert result.name == "forbidden_dependencies"
    assert result.passed is False
    assert result.blocking is True
    assert result.evidence == ["Requests"]
    assert result.metadata["added_dependencies"] == ["pydantic", "Requests"]


def test_forbidden_dependency_evaluator_passes_allowed_dependencies() -> None:
    result = ForbiddenDependencyEvaluator(["requests"]).evaluate(["pydantic"])

    assert result.passed is True
    assert result.blocking is True
    assert result.evidence == []


def test_blocking_failure_forces_final_verdict_to_fail() -> None:
    passing = MustPassTestsEvaluator().evaluate(True)
    blocking_failure = ForbiddenPathEvaluator(["secrets/**"]).evaluate(
        ["secrets/token.txt"]
    )

    verdict = build_compatibility_verdict([passing, blocking_failure])

    assert verdict.passed is False
    assert verdict.blocking_failures == ["forbidden_paths"]
    assert verdict.results == [passing, blocking_failure]


def test_non_blocking_failure_does_not_force_verdict_to_fail() -> None:
    advisory_failure = EvaluationResult(
        name="advisory",
        passed=False,
        blocking=False,
        summary="Advisory check failed.",
    )

    verdict = build_compatibility_verdict([advisory_failure])

    assert verdict.passed is True
    assert verdict.blocking_failures == []
