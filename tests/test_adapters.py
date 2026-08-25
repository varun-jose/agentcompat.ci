"""Tests for the agent adapter abstraction and execution models."""

from datetime import UTC, datetime
from pathlib import Path

import pytest
from pydantic import ValidationError

from agentcompat.adapters.base import AgentAdapter
from agentcompat.models import AgentExecutionResult, TaskDefinition

STARTED_AT = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
COMPLETED_AT = datetime(2026, 1, 1, 12, 1, tzinfo=UTC)


def execution_result(**overrides: object) -> AgentExecutionResult:
    """Build an execution result for adapter tests."""
    values: dict[str, object] = {
        "agent_name": "test-agent",
        "started_at": STARTED_AT,
        "completed_at": COMPLETED_AT,
        "exit_code": 0,
        "stdout": "task complete",
        "stderr": "",
    }
    values.update(overrides)
    return AgentExecutionResult.model_validate(values)


class StubAdapter(AgentAdapter):
    """Small concrete adapter used to exercise the abstract interface."""

    def __init__(self) -> None:
        self.prepared_workspace: Path | None = None

    async def prepare(self, workspace: Path) -> None:
        self.prepared_workspace = workspace

    async def execute(
        self,
        task: TaskDefinition,
        workspace: Path,
    ) -> AgentExecutionResult:
        return execution_result(
            metadata={
                "task": task.model_dump(),
                "workspace": str(workspace),
            }
        )


class IncompleteAdapter(AgentAdapter):
    """Adapter intentionally missing execute for abstraction testing."""

    async def prepare(self, workspace: Path) -> None:
        return None


@pytest.mark.asyncio
async def test_concrete_adapter_prepares_and_executes(tmp_path: Path) -> None:
    adapter = StubAdapter()
    task = TaskDefinition.model_validate(
        {"name": "add-pagination", "prompt": "Add pagination."}
    )

    await adapter.prepare(tmp_path)
    result = await adapter.execute(task, tmp_path)

    assert adapter.prepared_workspace == tmp_path
    assert result.agent_name == "test-agent"
    assert result.exit_code == 0
    assert result.metadata["task"] == {
        "name": "add-pagination",
        "prompt": "Add pagination.",
        "verification": None,
    }
    assert result.metadata["workspace"] == str(tmp_path)


def test_incomplete_adapter_cannot_be_instantiated() -> None:
    with pytest.raises(TypeError, match="abstract method 'execute'"):
        IncompleteAdapter()


def test_adapter_interface_contains_only_prepare_and_execute() -> None:
    assert AgentAdapter.__abstractmethods__ == frozenset({"prepare", "execute"})
    assert not hasattr(AgentAdapter, "collect_result")


def test_execution_result_records_all_available_details() -> None:
    result = execution_result(
        commands_executed=["pytest -q", "ruff check ."],
        estimated_cost=0.25,
        metadata={"model": "example-model"},
    )

    assert result.agent_name == "test-agent"
    assert result.started_at == STARTED_AT
    assert result.completed_at == COMPLETED_AT
    assert result.exit_code == 0
    assert result.stdout == "task complete"
    assert result.stderr == ""
    assert result.commands_executed == ["pytest -q", "ruff check ."]
    assert result.estimated_cost == 0.25
    assert result.metadata == {"model": "example-model"}


def test_execution_result_defaults_optional_details() -> None:
    result = execution_result()

    assert result.commands_executed == []
    assert result.estimated_cost is None
    assert result.metadata == {}


def test_execution_result_requires_agent_name() -> None:
    with pytest.raises(ValidationError, match="agent_name"):
        AgentExecutionResult(
            started_at=STARTED_AT,
            completed_at=COMPLETED_AT,
            exit_code=0,
            stdout="",
            stderr="",
        )
