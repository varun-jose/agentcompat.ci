# AgentCompat CI Product Specification

## Problem

AI coding-agent ecosystems are becoming portable at the file and protocol level, but portable instructions do not guarantee portable behaviour.

The same engineering task may produce materially different behaviour under Codex, Gemini, Copilot, Claude Code or another agent.

AgentCompat CI verifies behavioural compatibility.

## Core Question

Given:

- repository R
- task T
- engineering contract C
- baseline agent A
- candidate agent B

determine whether B satisfies the configured deterministic engineering contract. The
v0.1 baseline is executed and reported, but is not yet a semantic oracle whose diff or
runtime behaviour is compared with B.

## MVP

Inputs:

1. Repository
2. Task YAML
3. Contract YAML
4. Baseline agent
5. Candidate agent

Outputs:

- build result
- test result
- changed files
- separately reported Git-ignored artifacts
- forbidden file violations
- required file checks
- max changed-file check
- trusted direct dependency additions and removals
- context-aware dependency version and hashed source changes
- supported lockfile changes and required co-updates
- compatibility percentage
- final PASS / FAIL verdict

## Non-goals for v0.1

Do not build:

- SaaS dashboard
- authentication
- billing
- PostgreSQL
- AWS infrastructure
- LLM judge
- enterprise RBAC
- MCP compatibility
- Agent Skills compatibility

Those come later.

## Initial Agents

v0.1:

- Codex
- Gemini CLI
- Kiro CLI

v0.2:

- GitHub Copilot CLI

v0.3:

- Claude Code

## Product principle

A compatibility score must never hide a critical violation.

If a blocking contract rule fails, the overall result is FAIL regardless of aggregate score.

## Current CLI operation

One `run` invocation accepts one committed source repository, one contract, one declared
task, one baseline, and one candidate. It does not run every candidate listed in a
contract, execute a task matrix, or evaluate an existing pull-request diff.

In a maintainer checkout containing the local orders API fixture, the canonical
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

The fixture repository is maintainer-local and is not part of a fresh clone. A different
source repository requires a matching contract and task. The source must be a Git
working tree with at least one commit; only committed `HEAD` is cloned, so uncommitted
source changes are not inputs to the run.

Baseline and candidate execute sequentially. Each agent currently has a 15-minute
timeout, and each test or build verification command has a separate 5-minute timeout.
Agent output is buffered. Interactive terminals show stage progress, while non-TTY
execution can remain silent until completion. The JSON report is written only after the
entire run and cleanup complete; each run should use a fresh output filename.

The Python environment flags reduce common cache artifacts but do not guarantee a clean
diff. Ordinary untracked files are reported as additions and Git-ignored untracked
files are reported separately. Ignored paths remain auditable and subject to forbidden
path rules, but do not consume the changed-file budget unless
`changed_files.include_ignored` is enabled. Contract exclusion globs affect only that
budget. Exit `0` means all blocking checks passed, exit `1` means a completed contract
failure, and exit `2` means a configuration or agent-execution error.

The resulting verdict is candidate contract compliance, not semantic equivalence with
the baseline. Baseline test/build metadata are recorded but are not separate blocking
findings, and project verification runs after each agent inside its writable workspace.

## Changed-file policy

New contracts configure the budget under `rules.changed_files`:

```yaml
changed_files:
  max: 8
  include_ignored: false
  exclude:
    - ".pytest_cache/**"
    - "**/__pycache__/**"
```

The legacy `rules.max_changed_files` scalar remains accepted. If both forms are
present, their maximums must be equal. Repository and task maximums merge to the lower
value. A task can enable ignored-file counting but cannot turn off a repository
requirement. Task exclusion lists may only remove literal patterns already present in
the repository contract; adding exclusions would weaken the team policy and is
rejected.

Exclusion and ignore policies apply only to `ChangedFileCountEvaluator`. Every path is
still recorded in the terminal/JSON report, and `ForbiddenPathEvaluator` checks ignored
and excluded paths. `RequiredPathEvaluator` accepts only surviving ordinary Git
changes, not ignored artifacts or deletions.

## Dependency policy scope

Dependency observations are produced by AgentCompat itself rather than accepted from
agent adapter metadata. The Python scanner supports static PEP 621 and ordered PEP 735
declarations, validated Poetry/PDM subsets, a bounded fail-closed subset of UTF-8 pip
requirements/constraints, and bounded version-gated parsers for `pylock.toml`,
`poetry.lock`, `pdm.lock`, `Pipfile.lock`, and `uv.lock`.
The accepted gates are pylock 1.0, Poetry 1.1/2.0/2.1, PDM 4.0.0–4.5.1,
Pipfile spec 6, and uv lock version 1 revisions 0–3.

Forbidden dependency additions apply only to install-root declarations. Constraint-only
and transitive locked packages remain separate observations. Version, source, removal,
lockfile immutability, and resolver-input/lockfile co-update checks are opt-in contract
rules. Resolver inputs cover supported declarations, constraints, group topology,
integrity settings, and package-source policy. Source locators are represented by
hashes so credentials are not reported.

Lockfile co-update evidence proves only that configured lockfile bytes changed with a
resolver-input change; it does not prove that a resolver regenerated a semantically
fresh lock. Configured lockfiles must exist. The scanner does not fetch artifact
contents, construct a complete resolution graph, or evaluate markers for a concrete
target environment. Poetry constraint strings are deterministic syntax fingerprints,
not full Poetry-core semantic normalization. Unsupported manifest forms, lock schemas,
and future format versions fail closed when a dependency rule is configured.
