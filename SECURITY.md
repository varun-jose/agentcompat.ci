# AgentCompat CI Security

AgentCompat executes autonomous coding-agent CLIs. A disposable Git clone protects the
source working tree from ordinary repository edits, but it is not a container, virtual
machine, or complete security boundary.

## Execution model

- Codex runs non-interactively with its `workspace-write` sandbox.
- Gemini uses non-interactive approval.
- Kiro runs with `--trust-all-tools` and does not ask for tool confirmation.
- Agent processes inherit the invoking user's environment, provider authentication,
  network access, and any credentials available to those CLIs.
- Source repository remotes are removed from disposable clones, but this does not
  disable process-level network access.

Use a dedicated low-privilege environment for untrusted tasks. Do not expose live runs
to production credentials, cloud-admin sessions, signing keys, sensitive repositories,
or unrestricted production networks.

## Safer local fixture run

Before running the maintainer-local Codex-versus-Kiro fixture, confirm that the source
is the intended disposable test repository and that both agents use test-only accounts:

```bash
source .venv/bin/activate

git -C fixtures/repos/orders-api rev-parse --show-toplevel
git -C fixtures/repos/orders-api rev-parse --verify HEAD
codex login status
kiro-cli whoami
agentcompat validate agent-contract.yaml
```

Start the experiment only after those checks succeed:

```bash
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

The fixture path is maintainer-local and is not available in a fresh clone. For another
repository, create a matching contract and task instead of reusing the pagination
contract blindly.

## Output and cancellation

Agent stdout and stderr are buffered until the complete run returns. Non-interactive
terminals can remain silent while an agent is active. On macOS or Linux, inspect the
processes from a separate terminal with:

```bash
pgrep -lf 'agentcompat|codex|kiro-cli'
```

Press `Ctrl+C` in the original terminal to cancel normally. Avoid force-killing the
runner unless normal cancellation fails, because normal cancellation terminates the
active child and cleans up disposable workspaces.

JSON reports contain captured agent output, changed paths, command diagnostics, and
local filesystem paths. Treat them as potentially sensitive. Use a fresh output name
for every run, review the file before sharing it, and never publish proprietary prompts,
source fragments, credentials, or internal repository details.

## Reporting a vulnerability

Do not include credentials, private source code, or exploit data in a public issue.
Use the repository host's private security-reporting channel when available.
