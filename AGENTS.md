# AgentCompat CI — Engineering Instructions

## Purpose

AgentCompat CI is a behavioural compatibility testing framework for AI coding agents.

Its purpose is to answer:

Given the same repository, engineering instructions and task, does a candidate coding agent preserve the behaviour and engineering constraints demonstrated by a baseline agent?

This is NOT a generic LLM evaluation framework.

## MVP

The initial version supports:

- Codex as a baseline or candidate agent.
- Gemini CLI as a baseline or candidate agent.
- YAML-based task definitions.
- YAML-based engineering contracts.
- Disposable Git workspaces.
- Deterministic compatibility evaluators.
- CLI reporting.

Do not implement a web UI, database, SaaS backend or cloud infrastructure unless explicitly requested.

## Architecture

Keep agent-specific behaviour behind the AgentAdapter interface.

Core modules:

- agentcompat/models.py
- agentcompat/contracts.py
- agentcompat/runner.py
- agentcompat/evaluators.py
- agentcompat/adapters/

The compatibility engine must not contain hard-coded agent-specific logic.

## Evaluation philosophy

Prefer deterministic evaluation.

Priority:

1. Unit/integration tests
2. Build command
3. Git diff analysis
4. File path rules
5. Dependency rules
6. Schema validation
7. Static analysis

Do not use an LLM-as-judge in MVP v0.1.

## Workspace safety

Never execute a candidate coding agent directly against the main repository working tree.

Every compatibility execution must run against a disposable copy or Git worktree.

## Python

Use Python 3.12+.

Use:

- Pydantic for schemas
- Typer for CLI
- pathlib instead of string paths
- asyncio for agent execution abstraction where appropriate
- type hints throughout

Keep functions small and testable.

## Validation

Before completing any implementation task, run:

ruff check .
mypy agentcompat
pytest -q

Fix failures before reporting completion.

## Development workflow

Work in small tasks.

Do not perform broad refactors unless explicitly requested.

Before modifying code:

1. Inspect the relevant files.
2. Explain the implementation approach briefly.
3. Implement the smallest complete change.
4. Add tests.
5. Run all required validation.

## Product documentation

Read:

- docs/product-spec.md
- docs/architecture.md

when relevant.

Treat them as the source of truth.