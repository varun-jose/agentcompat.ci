"""Coding-agent adapter interfaces and implementations."""

from agentcompat.adapters.base import AgentAdapter
from agentcompat.adapters.codex import CodexAdapter
from agentcompat.adapters.gemini import GeminiAdapter

__all__ = ["AgentAdapter", "CodexAdapter", "GeminiAdapter"]
