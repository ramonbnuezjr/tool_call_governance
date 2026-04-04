# tool-call-governance

A 3-layer governance enforcement framework for agentic AI systems.

> "Governance doesn't attach at the policy document. It attaches at the tool call."

The thesis: individually-scoped components can compose into privilege escalation
without any single hop violating its rules. Each step is clean. The cumulative
result isn't. Enforcement must live at the execution boundary.

---

## What This Is

A Python middleware library called `GovernanceGate` — an interceptor that sits
between an AI agent and tool execution. Every tool call passes through the gate
before it runs.

This is a personal learning lab and prototype, not a production system. It is a
structured way to stress-test the engineering challenges of governing agentic
behavior at the execution boundary — layer by layer.

---

## Architecture

### Layer 1 — Deterministic (complete)
Policy-as-code enforcement. Hard allow/deny rules encoded in YAML. Default
posture: **DENY**. If a tool call is not explicitly on the whitelist, it is
denied. Consistent, auditable, fast.

Known limitation: cannot detect privilege escalation via tool chaining.
Sequential composition attacks are a Layer 2 concern.

### Layer 2 — Probabilistic (planned)
Behavior monitoring and drift detection. Telemetry from Layer 1 feeds a
baseline model. Risk scores per tool call. Anomaly alerts when behavior
deviates from baseline.

### Layer 3 — Dynamic / Context (planned)
Real-time context-awareness. Cascade detection. Human-in-the-loop escalation
for high-consequence, irreversible actions.

---

## Project Structure

```
tool-call-governance/
├── policy/
│   └── rules.yaml              # Governance policy (whitelist + blacklist)
├── gate/
│   ├── __init__.py             # Public API
│   ├── engine.py               # GovernanceGate class
│   ├── logger.py               # Audit logging to SQLite
│   └── models.py               # ToolCall, Decision, Rule dataclasses
├── cli/
│   └── gate.py                 # CLI entrypoint
├── tests/
│   ├── test_engine.py          # Layer 1 test harness (4/4 passing)
│   └── fixtures/
│       └── sample_calls.yaml   # Simulated tool calls
└── docs/
    └── layer1_gaps.md          # Engineering notes on deterministic rule limits
```

---

## Quickstart

**Dependencies**

```bash
pip install pyyaml pytest
```

**Run the tests**

```bash
pytest tests/ -v
```

**Evaluate a tool call**

```bash
python cli/gate.py check --tool read_file --input '{"path": "/tmp/report.txt"}'
python cli/gate.py check --tool execute_shell --input '{"command": "rm -rf /"}'
python cli/gate.py check --tool rename_file --input '{"src": "/tmp/a.txt", "dst": "/tmp/b.txt"}'
```

**Inspect the audit log**

```bash
python cli/gate.py audit               # all decisions
python cli/gate.py audit --denied      # denied only
python cli/gate.py audit --gaps        # no_matching_rule hits (the Layer 2 signal)
```

---

## Policy

Rules are defined in `policy/rules.yaml`. The policy is grounded in:

- **OWASP LLM Top 10 (2025), LLM08: Excessive Agency** — AI agents must not
  have more permissions than the task requires.
- **Principle of Least Privilege** — NIST SP 800-53 Rev 5, AC-6. Explicit
  permission is required, not explicit prohibition.

| List | Criteria |
|------|----------|
| Whitelist | Read-only, local, reversible, no external side effects |
| Blacklist | Writes, deletes, network calls, irreversible actions, high blast radius |
| Neither | Denied by default as `no_matching_rule` — the Layer 2 signal |

---

## Tool Call Schema

Anthropic format:

```python
ToolCall(name="read_file", input={"path": "/tmp/report.txt"})
```

---

## Using GovernanceGate in Code

```python
from gate import GovernanceGate, AuditLogger, ToolCall

gate   = GovernanceGate(policy_path="policy/rules.yaml")
logger = AuditLogger(db_path="audit.db")

tool_call = ToolCall(name="read_file", input={"path": "/tmp/report.txt"})
decision  = gate.evaluate(tool_call)
logger.log(decision)

print(decision.outcome)        # Outcome.ALLOWED
print(decision.rule_triggered) # read_file
```

---

## Layer 1 Limitations

See [`docs/layer1_gaps.md`](docs/layer1_gaps.md) for the full engineering
analysis. Summary:

| Gap | Description |
|-----|-------------|
| Parameter blindness | Rules match tool name only — input values are not inspected |
| Tool chaining | No session memory — sequential composition attacks are invisible |
| Policy completeness | Whitelist must be manually maintained as tool registry grows |
| Frequency / rate | Every call treated equally regardless of volume or timing |
| Semantic equivalence | `execute_shell` and `run_command` are treated as unrelated tools |

---

## Tech Stack

- Python 3.11+
- PyYAML — policy parsing
- SQLite (`sqlite3` stdlib) — audit log
- pytest — test harness
- No external dependencies in Layer 1

---

## Intellectual Foundation

LinkedIn post: *"Governance doesn't attach at the policy document. It attaches
at the tool call."* — Posted 2026-04-07

Core insight: the enforcement boundary must be the execution boundary. Policy
documents, system prompts, and agent instructions are all advisory. The gate
is not.
