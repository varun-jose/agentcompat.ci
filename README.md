![AgentCompat CI](docs/assets/agentcompat-ci-hero.png)

# AgentCompat CI

**Cross-agent behavioural compatibility testing for AI coding agents.**

> Same repository. Same task. Different agent. Measurable compatibility.

![Python](https://img.shields.io/badge/Python-3.12%2B-blue)
![Status](https://img.shields.io/badge/status-experimental-orange)
![License](https://img.shields.io/badge/license-Apache--2.0-green)
![Version](https://img.shields.io/badge/version-0.1.0-blueviolet)

---

## Why AgentCompat CI?

AI coding-agent ecosystems are becoming increasingly portable.

Projects can now share instructions, tasks, skills, plugins, and development conventions across different coding agents.

But there is an important problem:

> **Portable instructions do not guarantee portable behaviour.**

The same repository, task, and engineering instructions may produce materially different outcomes when executed by different AI coding agents.

One agent may:

- pass all tests,
- preserve architecture boundaries,
- respect protected files,
- and make only the required changes.

Another agent, given exactly the same task, may:

- modify unrelated files,
- add unnecessary dependencies,
- ignore repository constraints,
- fail tests,
- or violate engineering policies.

AgentCompat CI makes those differences measurable.

---

## How It Works

![How AgentCompat CI Works](docs/assets/agentcompat-ci-workflow.png)

AgentCompat executes the same engineering task against a baseline agent and one or more candidate agents using isolated repository workspaces.

It then evaluates the resulting behaviour using deterministic checks.

```text
Repository
   +
Task
   +
Engineering Contract
        │
        ▼
┌─────────────────┐
│ Baseline Agent  │
└────────┬────────┘
         │
         │
┌────────▼────────┐
│ Candidate Agent │
└────────┬────────┘
         │
         ▼
┌────────────────────────┐
│ Deterministic Evaluator│
├────────────────────────┤
│ Tests                  │
│ Build                  │
│ Git diff               │
│ Forbidden paths        │
│ Required paths         │
│ Dependency changes     │
│ Repository policies    │
└───────────┬────────────┘
            │
            ▼
     Compatibility Report
```

---

## Core Idea

Traditional AI evaluation often asks:

> Which model generated the better answer?

AgentCompat asks a different question:

> **Does a candidate coding agent preserve the engineering behaviour required by this repository?**

The project focuses on behavioural compatibility rather than subjective code preference.

---

## Example

Suppose the same task is executed by Codex and Gemini.

```bash
agentcompat run \
  --repo fixtures/repos/orders-api \
  --baseline codex \
  --candidate gemini \
  --contract agent-contract.yaml \
  --task fixtures/tasks/add-pagination.yaml
```

AgentCompat may produce:

```text
                    AgentCompat CI

Baseline                    codex
Candidate                   gemini
Task                        add-pagination

Build                       PASS
Tests                       PASS
Required paths              PASS
Forbidden paths             FAIL
Changed file limit          PASS

Compatibility              87.5%

Critical violation:

Candidate modified:

.github/workflows/ci.yml

The engineering contract prohibits changes
to CI configuration.

Final verdict:

FAIL
```

The candidate may have implemented the requested feature correctly while still violating the repository's engineering contract.

AgentCompat makes that distinction visible.

---

## What AgentCompat Evaluates

AgentCompat currently focuses on deterministic engineering signals.

### Functional Compatibility

- Build success
- Unit tests
- Integration tests
- Expected runtime behaviour

### Repository Compatibility

- Required files
- Forbidden files
- Maximum changed files
- Unexpected file creation
- Unexpected deletion

### Dependency Compatibility

- Added dependencies
- Removed dependencies
- Forbidden dependencies
- Dependency drift

### Policy Compatibility

- Protected paths
- Repository-specific rules
- Required commands
- Engineering constraints

The goal is to use deterministic evidence whenever possible.

AgentCompat does **not** rely on an LLM judge for its core PASS/FAIL decision.

---

## Example Contract

Agent behaviour is defined using a YAML contract.

```yaml
version: 1

project:
  name: sample-fastapi-api

baseline:
  agent: codex

candidates:
  - gemini

rules:

  tests_must_pass: true

  build_must_pass: true

  forbidden_paths:
    - ".github/workflows/**"
    - "infra/production/**"

  required_paths:
    - "src/orders/**"

  max_changed_files: 8

  forbidden_dependencies:
    - requests

tasks:
  - fixtures/tasks/add-pagination.yaml
```

This contract becomes the behavioural boundary against which agents are evaluated.

---

## Example Task

```yaml
name: add-pagination

prompt: |
  Add cursor-based pagination to GET /orders.

  Requirements:

  - preserve the existing response envelope
  - maximum page size must be 100
  - add appropriate tests
  - do not modify CI configuration
  - do not introduce new runtime dependencies

verification:

  test_command: pytest -q

rules:

  forbidden_paths:
    - ".github/workflows/**"

  required_paths:
    - "src/orders/**"

  max_changed_files: 6
```

---

## Installation

AgentCompat CI currently targets Python 3.12+.

Clone the repository:

```bash
git clone https://github.com/YOUR_GITHUB_USERNAME/agentcompat-ci.git
cd agentcompat-ci
```

Create a virtual environment:

```bash
python3.12 -m venv .venv
source .venv/bin/activate
```

Install the project:

```bash
pip install -e .
```

Install development dependencies if required:

```bash
pip install -e ".[dev]"
```

Verify:

```bash
agentcompat --version
```

---

## Agent Requirements

AgentCompat invokes supported coding-agent CLIs from isolated workspaces.

For the initial release, the target integrations are:

- OpenAI Codex CLI
- Google Gemini CLI

Check availability:

```bash
codex --version
gemini --version
```

Each CLI must be separately installed and authenticated according to its provider's instructions.

AgentCompat does not store provider credentials.

---

## Validate a Contract

Before running an experiment:

```bash
agentcompat validate agent-contract.yaml
```

Example:

```text
AgentCompat Contract

Project       sample-fastapi-api
Baseline      codex
Candidates    gemini
Tasks         1

Contract valid.
```

---

## Run a Compatibility Test

```bash
agentcompat run \
  --repo fixtures/repos/orders-api \
  --baseline codex \
  --candidate gemini \
  --contract agent-contract.yaml \
  --task fixtures/tasks/add-pagination.yaml
```

Optional JSON report:

```bash
agentcompat run \
  --repo fixtures/repos/orders-api \
  --baseline codex \
  --candidate gemini \
  --contract agent-contract.yaml \
  --task fixtures/tasks/add-pagination.yaml \
  --json-output results.json
```

---

## Exit Codes

AgentCompat is designed for CI/CD use.

| Code | Meaning |
|---:|---|
| `0` | Compatibility checks passed |
| `1` | Behavioural compatibility failure |
| `2` | Configuration or execution error |

This makes AgentCompat suitable for release gates and automated pipelines.

---

## Execution States

AgentCompat distinguishes between different kinds of failure.

### PASS

The evaluator executed successfully and the contract was satisfied.

### FAIL

The evaluator executed successfully and detected a compatibility violation.

### NOT_RUN

The evaluator could not run because an earlier prerequisite failed.

Example:

```text
Candidate agent authentication failed.

Build       NOT_RUN
Tests       NOT_RUN
```

### ERROR

The evaluator itself encountered an unexpected error.

This distinction prevents infrastructure failures from being incorrectly reported as agent compatibility failures.

---

## Architecture

The architecture intentionally separates agent-specific execution from the compatibility engine.

```text
                       AgentCompat CLI
                              │
                              ▼
                       Contract Loader
                              │
                              ▼
                         Task Runner
                              │
               ┌──────────────┴──────────────┐
               │                             │
               ▼                             ▼
       Baseline AgentAdapter         Candidate AgentAdapter
               │                             │
               ▼                             ▼
       Disposable Workspace          Disposable Workspace
               │                             │
               └──────────────┬──────────────┘
                              │
                              ▼
                     Evaluator Engine
                              │
          ┌───────────────────┼───────────────────┐
          │                   │                   │
          ▼                   ▼                   ▼
        Tests              Git Diff             Policy
          │                   │                   │
          └───────────────────┼───────────────────┘
                              │
                              ▼
                     Compatibility Result
```

Agent-specific logic stays behind a common adapter interface.

This allows additional agents to be introduced without changing the core evaluation engine.

---

## Design Principles

AgentCompat follows several principles.

### Deterministic First

If compatibility can be measured through:

- tests,
- builds,
- schemas,
- Git diffs,
- static analysis,
- repository policies,

those signals should be preferred over LLM-based evaluation.

### Isolated Execution

Candidate agents must never execute directly against the developer's primary working tree.

Each run receives an isolated disposable workspace.

### Reproducibility

Compatibility runs should record enough information to reproduce an experiment:

```text
repository SHA
task version
contract version
agent
agent version
model where known
result
```

### Critical Rules Override Scores

A high aggregate score must never hide a serious violation.

For example:

```text
Compatibility score: 98%

Critical violation:
production infrastructure modified
```

must still produce:

```text
FAIL
```

---

## Supported Agents

### Current / Experimental

| Agent | Status |
|---|---|
| OpenAI Codex CLI | Experimental |
| Google Gemini CLI | Experimental |

### Planned

| Agent | Status |
|---|---|
| GitHub Copilot CLI | Planned |
| Claude Code | Planned |
| Cursor | Planned |
| Kiro | Planned |

---

## Roadmap

### v0.1

- [x] Contract-as-code
- [x] Task definitions
- [x] Disposable workspaces
- [x] Agent adapter architecture
- [x] Codex integration
- [x] Gemini integration
- [x] Deterministic evaluators
- [x] CLI compatibility report

### v0.2

- [ ] GitHub Actions integration
- [ ] JUnit output
- [ ] SARIF output
- [ ] Baseline regression snapshots
- [ ] GitHub PR compatibility reports

### v0.3

- [ ] GitHub Copilot adapter
- [ ] Claude Code adapter
- [ ] Multi-candidate comparison
- [ ] Compatibility matrix

### v0.4

- [ ] Agent Skills compatibility
- [ ] Agent Plugins compatibility
- [ ] Instruction portability analysis
- [ ] Agent-specific compatibility adapters

### Future

- [ ] Hosted compatibility dashboard
- [ ] Organisation-wide agent compatibility baselines
- [ ] Private enterprise runners
- [ ] Historical regression tracking
- [ ] Agent migration recommendations
- [ ] Community compatibility benchmark

---

## The Longer-Term Vision

Agent ecosystems are beginning to standardise:

- instructions,
- skills,
- plugins,
- tools,
- protocols.

But interoperability at the file or protocol level does not guarantee behavioural equivalence.

AgentCompat aims to develop a practical engineering discipline around:

# Agent Compatibility Engineering

Just as web teams test applications across browsers, engineering teams may eventually need to test autonomous development workflows across coding agents.

```text
Works with Agent A

        ≠

Works with Agent B
```

AgentCompat exists to measure that difference.

---

## Project Status

AgentCompat CI is currently:

> **Experimental — v0.1**

The project is under active development.

Interfaces, contracts, and CLI behaviour may change before the first stable release.

Feedback, experiments, compatibility reports, and contributions are welcome.

---

## Contributing

Contributions are welcome.

Please read [CONTRIBUTING.md](CONTRIBUTING.md) before submitting a pull request.

Areas where contributions will be particularly useful:

- additional agent adapters,
- deterministic evaluators,
- reproducible benchmark tasks,
- portability test cases,
- documentation,
- CI integrations.

---

## Security

AgentCompat executes autonomous coding agents.

Although execution is designed to occur inside disposable workspaces, users should treat agent execution as potentially unsafe.

Do not provide benchmark agents with:

- production credentials,
- production cloud access,
- sensitive repositories,
- unrestricted network access,

unless you have independently established an appropriate containment environment.

Please see [SECURITY.md](SECURITY.md).

---

## Contributing Compatibility Results

One long-term goal is to create reproducible evidence of cross-agent behaviour.

If you discover an interesting portability failure, consider contributing:

```text
repository fixture
task
contract
agent versions
expected behaviour
observed behaviour
```

Do not submit proprietary code or confidential data.

---

## License

Licensed under the Apache License 2.0.

See [LICENSE](LICENSE).

---

## Acknowledgements

AgentCompat CI is an independent open-source project exploring behavioural compatibility across AI coding agents.

It is not affiliated with, endorsed by, or sponsored by OpenAI, Google, GitHub, Anthropic, Cursor, Amazon, or other agent vendors referenced in project documentation.

Product and company names are used only to identify supported interoperability targets.

---

## Final Thought

> **Portable instructions do not guarantee portable behaviour.**

AgentCompat CI exists to turn that uncertainty into measurable engineering evidence.