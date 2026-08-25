# AgentCompat CI — Engineering Instructions

## Purpose

AgentCompat CI is a behavioural compatibility testing framework for AI coding agents.

Its purpose is to answer:

Given the same repository, engineering instructions and task, does a candidate coding agent preserve the behaviour and engineering constraints demonstrated by a baseline agent?

Current v0.1 scope is narrower: deterministic contract checks are applied to the
candidate workspace. The baseline is an execution prerequisite and report input, not
yet a semantic oracle for candidate equivalence.

This is NOT a generic LLM evaluation framework.

## MVP

The initial version supports:

- Codex as a baseline or candidate agent.
- Gemini CLI as a baseline or candidate agent.
- Kiro CLI as a baseline or candidate agent.
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

```bash
source .venv/bin/activate
ruff check .
mypy agentcompat
pytest -q
```

Fix failures before reporting completion.

## Live compatibility runs

Do not start real coding-agent sessions as routine project validation. They can take
up to the configured agent and verification timeouts, consume provider tokens, and
execute trusted tools. Run them only when explicitly required.

For a maintainer checkout that contains the local orders API fixture, the canonical
Codex-baseline/Kiro-candidate command is:

```bash
source .venv/bin/activate
agentcompat validate agent-contract.yaml

PYTHONDONTWRITEBYTECODE=1 \
PYTEST_ADDOPTS="-p no:cacheprovider" \
agentcompat run \
  --repo fixtures/repos/orders-api \
  --baseline codex \
  --candidate kiro \
  --contract agent-contract.yaml \
  --task fixtures/tasks/add-pagination.yaml \
  --json-output results-codex-vs-kiro-run-01.json
```

The fixture repository is maintainer-local and is not present in a fresh clone. Read
the README runbook before a live run. It documents authentication, repository
preconditions, sequential execution, buffered output, timeout budgets, monitoring,
exit codes, result scope, and security limitations.

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

- README.md
- docs/product-spec.md
- docs/architecture.md

when relevant.

Treat them as the source of truth.
