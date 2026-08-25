"""Engineering contract loading and validation."""

from pathlib import Path

import yaml
from pydantic import ValidationError

from agentcompat.models import (
    AgentContract,
    ChangedFileRules,
    ContractRules,
    DependencyDriftRules,
    DependencyManifestRule,
    TaskChangedFileRules,
    TaskDependencyDriftRules,
    TaskRules,
)


class ContractValidationError(ValueError):
    """Raised when a contract cannot be loaded or validated."""


def _merge_unique(base_values: list[str], task_values: list[str]) -> list[str]:
    """Combine rule values without allowing task-level removal or duplication."""
    return list(dict.fromkeys([*base_values, *task_values]))


def _merge_manifests(
    base_values: list[DependencyManifestRule],
    task_values: list[DependencyManifestRule],
) -> list[DependencyManifestRule]:
    merged: dict[str, DependencyManifestRule] = {}
    for manifest in [*base_values, *task_values]:
        previous = merged.setdefault(manifest.path, manifest)
        if previous.role != manifest.role:
            raise ValueError(
                f"dependency manifest {manifest.path!r} has conflicting roles"
            )
    return list(merged.values())


def _merge_dependency_drift(
    contract: DependencyDriftRules,
    task: TaskDependencyDriftRules | None,
) -> DependencyDriftRules:
    if task is None:
        return contract
    return DependencyDriftRules(
        removals_forbidden=(
            contract.removals_forbidden or task.removals_forbidden is True
        ),
        versions_must_not_change=(
            contract.versions_must_not_change or task.versions_must_not_change is True
        ),
        sources_must_not_change=(
            contract.sources_must_not_change or task.sources_must_not_change is True
        ),
        lockfiles_must_not_change=(
            contract.lockfiles_must_not_change or task.lockfiles_must_not_change is True
        ),
        lockfiles_must_be_updated_for_dependency_changes=(
            contract.lockfiles_must_be_updated_for_dependency_changes
            or task.lockfiles_must_be_updated_for_dependency_changes is True
        ),
        lockfiles=_merge_unique(contract.lockfiles, task.lockfiles),
    )


def _merge_changed_files(
    contract: ChangedFileRules,
    task: TaskChangedFileRules | None,
) -> ChangedFileRules:
    """Merge changed-file rules without allowing a task to weaken the contract."""
    if task is None:
        return contract

    limits = [limit for limit in (contract.max, task.max) if limit is not None]
    if task.exclude is None:
        exclusions = contract.exclude
    else:
        task_additions = sorted(set(task.exclude) - set(contract.exclude))
        if task_additions:
            joined_additions = ", ".join(task_additions)
            raise ValueError(
                "task changed_files.exclude cannot add exclusions that are not in "
                f"the repository contract: {joined_additions}"
            )
        exclusions = task.exclude

    return ChangedFileRules(
        max=min(limits) if limits else None,
        include_ignored=(
            contract.include_ignored or task.include_ignored is True
        ),
        exclude=exclusions,
    )


def merge_task_rules(
    contract_rules: ContractRules,
    task_rules: TaskRules | None,
) -> ContractRules:
    """Merge task rules into a contract without weakening team requirements."""
    if task_rules is None:
        return contract_rules

    changed_files = _merge_changed_files(
        contract_rules.changed_files,
        task_rules.changed_files,
    )
    return ContractRules(
        tests_must_pass=(
            contract_rules.tests_must_pass or task_rules.tests_must_pass is True
        ),
        build_must_pass=(
            contract_rules.build_must_pass or task_rules.build_must_pass is True
        ),
        forbidden_paths=_merge_unique(
            contract_rules.forbidden_paths,
            task_rules.forbidden_paths,
        ),
        required_paths=_merge_unique(
            contract_rules.required_paths,
            task_rules.required_paths,
        ),
        max_changed_files=changed_files.max,
        changed_files=changed_files,
        forbidden_dependencies=_merge_unique(
            contract_rules.forbidden_dependencies,
            task_rules.forbidden_dependencies,
        ),
        dependency_manifests=_merge_manifests(
            contract_rules.dependency_manifests,
            task_rules.dependency_manifests,
        ),
        dependency_drift=_merge_dependency_drift(
            contract_rules.dependency_drift,
            task_rules.dependency_drift,
        ),
    )


def _format_validation_error(exc: ValidationError) -> str:
    """Format Pydantic errors with YAML-friendly dotted field paths."""
    messages: list[str] = []
    for error in exc.errors(include_input=False, include_url=False):
        location = ".".join(str(part) for part in error["loc"])
        messages.append(f"{location or 'contract'}: {error['msg']}")
    return "\n".join(messages)


def load_contract(path: Path) -> AgentContract:
    """Load and validate an AgentCompat CI contract from YAML."""
    try:
        raw_contract = yaml.safe_load(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise ContractValidationError(f"could not read contract: {exc}") from exc
    except yaml.YAMLError as exc:
        raise ContractValidationError(f"invalid YAML: {exc}") from exc

    try:
        return AgentContract.model_validate(raw_contract)
    except ValidationError as exc:
        raise ContractValidationError(_format_validation_error(exc)) from exc
