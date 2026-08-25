# AgentCompat CI Architecture

## Execution flow

CLI
→ Contract Loader
→ Task Loader
→ Workspace Manager
→ Baseline Agent Adapter
→ Candidate Agent Adapter
→ Evaluator Engine
→ Compatibility Report

## AgentAdapter

Every agent implementation must expose the same interface:

prepare(workspace) → None
execute(task, workspace) → AgentExecutionResult

Both operations are asynchronous. Execution returns its structured result directly
to keep the adapter boundary small.

### CodexAdapter

The Codex adapter uses the installed CLI's non-interactive `codex exec` command.
It passes prompts over standard input, selects the `workspace-write` sandbox, never
requests interactive approval, and only accepts workspaces stamped by the workspace
manager. Authentication is inherited from the local Codex CLI configuration; the
adapter does not store or supply credentials.

### GeminiAdapter

The Gemini adapter uses the installed CLI's `--prompt` option to select
non-interactive headless mode. It supplies the task over standard input, skips the
interactive workspace trust prompt, uses non-interactive approval, and only accepts
workspaces stamped by the workspace manager. Authentication is inherited from the
local Gemini CLI configuration; the adapter does not store or supply credentials.

### KiroAdapter

The Kiro adapter uses the installed CLI's `kiro-cli chat` command with
`--no-interactive` and `--trust-all-tools`. The task is supplied as the command's
documented input argument, but is redacted from execution logs. Output wrapping is
disabled for stable capture. Kiro runs only in a workspace stamped by the workspace
manager, and its local authentication configuration is inherited without storing or
supplying credentials.

## Workspace

Every agent receives an independent disposable repository workspace.

Baseline and candidate must never share writable state.

The workspace manager captures the source repository commit and creates a separate
temporary local clone for each run. Each workspace reports its Git changes and is
removed explicitly or when its context manager exits.

`WorkspaceDiff` separates modified, added, deleted, and ignored paths. Ignored paths
come from `git ls-files --others --ignored --exclude-standard`; a path force-added to
Git is therefore an ordinary addition even if an ignore pattern also matches it.

## Compatibility runner

The runner loads one contract and task, resolves adapters from an injected registry,
and creates independent disposable workspaces before executing the baseline and
candidate sequentially. Adapter exceptions are captured as structured run errors so
the other agent can still execute. Candidate Git changes and execution metadata feed
the deterministic evaluators. An adapter exception or nonzero process exit from either
agent is blocking. Baseline test and build metadata are reported, but the current
verdict does not create separate baseline test/build findings.

After each adapter finishes, the runner executes the task's optional verification
commands directly in that agent's disposable workspace. It captures stdout, stderr,
exit code, and timeout state as `tests_passed` and `build_passed` execution metadata.
Because verification runs after the agent in the same writable workspace, an agent can
modify repository tests before they execute. Immutable external oracle tests are not
implemented in v0.1.
Dependency evaluation uses a runner-generated typed snapshot captured before agent
execution and again from the candidate workspace. Adapter-provided dependency metadata
is removed before trusted observations are attached. Missing observations fail closed
for every configured dependency rule.

The snapshot keeps install-root declarations, constraint-only records, resolver-policy
records, and transitive lock records separate. It parses PEP 508 requirements, static
PEP 621/735 declarations, validated Poetry/PDM subsets, recursive pip requirements,
and supported version-gated Python lockfiles. Conditional version/source identities
include declaration scope, marker, and extras context so correlated swaps are not
flattened. The delta reports direct additions/removals, version/source changes,
complete resolver-input changes, and exact lockfile additions/removals/modifications.
Credential-bearing source values are hashed. Explicit pip entrypoints add a
`requirement` or `constraint` role; root `requirements*.txt` entrypoints remain active.

The scanner reads exact lockfile bytes for fingerprints, bounds files, records,
recursive traces, and report evidence, and rejects unsupported syntax rather than
silently omitting it. A co-update finding means configured lockfile bytes changed with
a supported resolver input; semantic resolver freshness is intentionally not inferred.

## CLI reporting

The `run` command renders the structured compatibility result as a Rich terminal
table and can persist the same result as JSON. The compatibility percentage counts
configured deterministic candidate checks; execution failures remain critical and
cannot be hidden by the percentage. Failed agent stderr is shown in a bounded
execution diagnostic. Exit codes distinguish compatible runs (`0`),
completed compatibility failures (`1`), and configuration or execution errors (`2`).

Interactive terminal runs display an indeterminate Rich spinner with elapsed time.
The runner supplies stage messages through an optional callback, keeping Rich and
all other presentation concerns outside the compatibility engine. Progress output is
disabled automatically when stdout is not an interactive terminal.

Adapter stdout and stderr are captured with `process.communicate()` and are not streamed
while an agent is active. Each supported adapter currently has a 900-second timeout.
Each test or build verification command has its own 300-second timeout. The baseline
and candidate run sequentially, so the current fixture's worst-case configured timeout
budget is approximately 50 minutes. The JSON file is written only after both agent runs,
verification, evaluation, and workspace cleanup complete. Normal `Ctrl+C` cancellation
propagates to the active adapter, which attempts to kill its child process before the
workspace contexts exit.

### Canonical local invocation

The current CLI executes one baseline, one candidate, and one task. In a maintainer
checkout containing the local orders API fixture, run:

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

The fixture repository is maintainer-local and is not distributed in a fresh clone.
For other repositories, all repository, contract, and task inputs must be supplied as a
matching set. Activating `.venv` is required because task verification invokes `python`
by name. The two Python environment flags reduce cache noise but do not prevent agents
from creating other ignored artifacts. Ordinary untracked paths remain additions;
Git-ignored untracked paths are captured separately as `ignored_files`. They stay
visible to reporting and forbidden-path evaluation but are omitted from the
changed-file count by default. Contracts can opt in with
`changed_files.include_ignored` and can apply `changed_files.exclude` globs only to the
counting rule. Ignored paths never satisfy required-path rules.

This invocation measures candidate contract compliance. Candidate checks do not consume
the baseline diff or derive a semantic oracle from the baseline implementation.

## Evaluators

The evaluator engine receives:

- baseline workspace
- candidate workspace
- task
- contract
- agent execution metadata

Evaluators return structured findings.

Initial evaluators:

- BuildMustPassEvaluator
- TestsMustPassEvaluator
- ForbiddenPathEvaluator
- RequiredPathEvaluator
- ChangedFileCountEvaluator
- ForbiddenDependencyEvaluator
- DependencyRemovalEvaluator
- DependencyVersionChangeEvaluator
- DependencySourceChangeEvaluator
- DependencyLockfileChangeEvaluator
- DependencyLockfileUpdateEvaluator

Evaluators are deterministic and return structured evidence. No evaluator uses an
LLM.

The changed-file evaluator reports the effective maximum, repository maximum, task
maximum, counted paths, observed ignored count, and exclusion matches. Repository and
task maximums merge with `min`. Counting ignored files merges with boolean `OR`, so a
task can tighten but not disable that requirement. A task may only retain or remove
literal repository exclusion patterns; it cannot add an exclusion that would weaken
the repository budget.

## Verdict

Blocking violation → FAIL.

Otherwise aggregate evaluation results into compatibility metrics.
