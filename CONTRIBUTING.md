# Contributing to AgentCompat CI

AgentCompat CI uses Python 3.12 or newer. Run commands from the repository root.

## Development setup

```bash
python3.12 -m venv .venv
source .venv/bin/activate
python -m pip install -e ".[dev]"
agentcompat --version
```

Keep `.venv` activated. Some task verification commands invoke `python` by name, so
running `.venv/bin/agentcompat` without activation does not reliably place the virtual
environment's interpreter on `PATH` for child processes.

## Required validation

Before opening a pull request, run:

```bash
source .venv/bin/activate
ruff check .
mypy agentcompat
pytest -q
git diff --check
```

The automated tests use fake adapters or mocked subprocesses and do not require live
Codex, Gemini, or Kiro sessions.

## Optional live Codex-versus-Kiro check

A live comparison is not part of routine contributor validation. It consumes provider
tokens, can take up to the configured timeouts, and invokes autonomous tools. Read
[SECURITY.md](SECURITY.md) and the README runbook before starting it.

The exact example below works only in a maintainer checkout that already contains the
local `fixtures/repos/orders-api` Git repository. That repository is not currently
distributed in a fresh clone.

```bash
source .venv/bin/activate

codex login status
kiro-cli whoami
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

Use a new JSON filename for every live run. Agent output is buffered, the report is
written only after both agents finish, and ignored build artifacts are reported
separately. They count toward the changed-file maximum only when
`changed_files.include_ignored` is enabled. Do not commit reports without reviewing
them for prompts, source details, filesystem paths, or other sensitive information.

## Change scope

- Keep agent-specific behavior behind `AgentAdapter`.
- Add or update deterministic tests for behavioral changes.
- Do not use live agent runs as a substitute for the required validation suite.
- Update the README, product specification, and architecture document when CLI or
  execution semantics change.
