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

## Workspace

Every agent receives an independent disposable repository workspace.

Baseline and candidate must never share writable state.

The workspace manager captures the source repository commit and creates a separate
temporary local clone for each run. Each workspace reports its Git changes and is
removed explicitly or when its context manager exits.

## Compatibility runner

The runner loads one contract and task, resolves adapters from an injected registry,
and creates independent disposable workspaces before executing the baseline and
candidate sequentially. Adapter exceptions are captured as structured run errors so
the other agent can still execute. Candidate Git changes and execution metadata feed
the deterministic evaluators, while failure of either agent is a blocking result.

After each adapter finishes, the runner executes the task's optional verification
commands directly in that agent's disposable workspace. It captures stdout, stderr,
exit code, and timeout state as `tests_passed` and `build_passed` execution metadata.
Dependency evaluation consumes an `added_dependencies` list. Missing required
observations fail closed.

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

Evaluators are deterministic and return structured evidence. No evaluator uses an
LLM.

## Verdict

Blocking violation → FAIL.

Otherwise aggregate evaluation results into compatibility metrics.
