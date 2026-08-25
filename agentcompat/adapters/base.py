"""Agent adapter interface."""

from abc import ABC, abstractmethod
from pathlib import Path

from agentcompat.models import AgentExecutionResult, TaskDefinition


class AgentAdapter(ABC):
    """Minimal interface implemented by every coding-agent adapter."""

    @abstractmethod
    async def prepare(self, workspace: Path) -> None:
        """Prepare an isolated workspace for this agent."""
        raise NotImplementedError

    @abstractmethod
    async def execute(
        self,
        task: TaskDefinition,
        workspace: Path,
    ) -> AgentExecutionResult:
        """Execute a task in an isolated workspace and return its result."""
        raise NotImplementedError
