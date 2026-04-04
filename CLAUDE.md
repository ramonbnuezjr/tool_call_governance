# tool-call-governance

## Purpose

A personal learning lab and prototype for a 3-layer governance enforcement framework for agentic AI systems. The thesis: governance doesn't attach at the policy document — it attaches at the tool call. This project builds and stress-tests that thesis layer by layer.

This is not a production system. It is a structured way to learn the engineering challenges of governing agentic behavior at the execution boundary.

## Domain

[GOV] / [1P] — AI governance research + solopreneur capability building

## Intellectual Foundation

Source: LinkedIn post — "Governance doesn't attach at the policy document. It attaches at the tool call." (Posted 2026-04-07)

Core insight: individually-scoped components can compose into privilege escalation without any single hop violating its rules. Each step is clean. The cumulative result isn't. Enforcement must live at the execution boundary.

## What Is Being Built

A Python middleware library called `GovernanceGate` — an interceptor that sits between an AI agent/orchestrator and tool execution. Every tool call passes through the gate before it runs.

## Architecture: 3-Layer Framework

### Layer 1 — Deterministic (BUILD FIRST)
Policy-as-code enforcement. Hard allow/deny/require_approval rules encoded in YAML. Consistent, auditable, fast. Known limitation: brittle — cannot anticipate harmful compositions across multiple steps.

**Default posture: DENY.** If a tool call is not explicitly on the whitelist, it is denied. There is no default-allow. This is the correct governance posture — it forces the policy author to be explicit about what is permitted, rather than relying on a blacklist to be complete.

**Layer 1 deliverables:**
- `GovernanceGate` class: accepts tool call (name + params), evaluates against policy, returns decision
- Policy file format: YAML with two explicit lists — `whitelist` (allow) and `blacklist` (deny)
- Audit logger: every decision logged with timestamp, tool name, params hash, decision, rule triggered
- CLI: `python gate.py audit` to inspect decision log
- Test harness: three required test scenarios (see below)

**Layer 1 test scenarios (all three are required):**

| Test | Tool Call | Expected Result | Decision Logged |
|---|---|---|---|
| 1 | On whitelist | `allowed` | rule name that matched |
| 2 | On blacklist | `denied` | rule name that matched |
| 3 | Neither list | `denied` | `no_matching_rule` |

Test 3 is the most important. It is the proof that a deterministic policy has blind spots — a tool call the policy never anticipated still gets through (as a deny), but the gap is now visible in the audit log. This is the evidence that drives Layer 2.

**Layer 1 learning objective:** Find the specific edge cases where deterministic rules break. Document them in `docs/layer1_gaps.md`. That gap is why Layer 2 exists.

### Layer 2 — Probabilistic (BUILD SECOND)
Behavior monitoring and drift detection. Telemetry from Layer 1 feeds a baseline model. Risk scores per tool call. Anomaly alerts when behavior deviates from baseline.

**Do not start Layer 2 until Layer 1 is working and its limitations are documented.**

### Layer 3 — Dynamic/Context (BUILD THIRD)
Real-time context-awareness. Is this action safe *right now*, given what else is happening in the system? Cascade detection. Human-in-the-loop escalation for high-consequence, irreversible actions.

**Do not start Layer 3 until Layer 2 is working and its limitations are documented.**

## Tech Stack

- Language: Python 3.11+
- Policy config: YAML
- Audit log: SQLite (via `sqlite3` stdlib — no ORM needed for Layer 1)
- CLI: `argparse` or `click`
- Testing: `pytest`
- No external dependencies in Layer 1 beyond PyYAML

## Project Structure

```
tool-call-governance/
├── CLAUDE.md               ← this file
├── README.md               ← project overview (generate at end of Layer 1)
├── policy/
│   └── rules.yaml          ← governance policy definition
├── gate/
│   ├── __init__.py
│   ├── engine.py           ← GovernanceGate class
│   ├── logger.py           ← audit logging to SQLite
│   └── models.py           ← ToolCall, Decision, Rule dataclasses
├── cli/
│   └── gate.py             ← CLI entrypoint
├── tests/
│   ├── test_engine.py
│   └── fixtures/
│       └── sample_calls.yaml   ← simulated tool calls for testing
└── docs/
    └── layer1_gaps.md      ← document where deterministic rules break (required before Layer 2)
```

## Build Protocol

- Build one layer at a time. Do not jump ahead.
- After each layer, document what broke or surprised you before moving to the next.
- Commit working code at the end of each layer with a clear commit message.
- Keep `docs/layer1_gaps.md` (and later layer2_gaps.md) as honest engineering notes — these are the learning artifacts.

## Session Startup

At the start of each session:
1. Check which layer is active by looking at what exists in `gate/` and `tests/`
2. Check `docs/` for any gap notes from the previous session
3. Resume where the last session left off — do not restart from scratch

## Decisions Log

| Decision | Choice | Rationale |
|---|---|---|
| Default posture | DENY | Governance requires explicit permission, not explicit prohibition. Forces policy completeness. |
| Policy structure | Whitelist + blacklist (separate lists) | Clear separation of intent. Whitelist = explicitly safe. Blacklist = explicitly dangerous. Neither = denied by default. |
| Audit scope | All decisions including `no_matching_rule` | The gaps are the data. Every blind spot must be visible. |
| Tool call schema | Anthropic format: `{name: str, input: dict}` | Aligns with real agentic system usage. |
| Model coupling | None — gate is model-agnostic | The gate intercepts at the execution boundary, not the model layer. It does not care what generated the tool call — Claude, GPT, Llama via Ollama, Mistral, or any other LLM. The only interface is the tool call schema. If a local or third-party model outputs a slightly different schema, a normalization shim handles it without touching gate logic. |

## Open Questions (as of project init)

- Should Layer 1 policy support AND/OR conditions, or keep it flat (single-condition rules)? Recommendation: start flat, complexity is a Layer 2 concern.
- Audit log: file-based SQLite is fine for Layer 1. Revisit for Layer 2 when telemetry volume increases.
