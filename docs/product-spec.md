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

determine whether B satisfies the same required behavioural contract as A.

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
- forbidden file violations
- required file checks
- max changed-file check
- dependency changes
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

v0.2:

- GitHub Copilot CLI

v0.3:

- Claude Code

## Product principle

A compatibility score must never hide a critical violation.

If a blocking contract rule fails, the overall result is FAIL regardless of aggregate score.