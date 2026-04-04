# Layer 2 Gaps — Where Probabilistic Scoring Breaks

Engineering notes on the observed limitations of the Layer 2 risk scorer
and anomaly detector. These gaps are the evidence base for why Layer 3 exists.

Written after: Layer 2 build complete, 5/5 tests passing, live agent run
verified against Llama 3.2 3B Instruct Q4_K_M on Apple M2.

---

## What Layer 2 Does Well

- Gap zone tools surface immediately. `rename_file` scored 0.65 and triggered
  an anomaly on the first live agent run — exactly as intended.
- The frequency signal differentiates tools with strong baselines from
  unanticipated ones. `read_file` scored 0.00; `get_current_time` scored 0.25
  due to prior test volume — the baseline is reading real history.
- The sequence signal fires correctly when a denied call immediately precedes
  an allowed one — a known probing pattern.
- Every decision, score, and anomaly flag lands in a single enriched audit row.
  The log is the truth.

---

## Gap 1 — Scores Are Static Weights, Not a Trained Model

**The problem:** The three signal weights (gap 40%, frequency 35%, sequence 25%)
and the anomaly threshold (0.65) are hand-tuned estimates, not values derived
from observed data.

**What this means in practice:** The weights worked correctly on our synthetic
test data and the first live run. They may not hold as the agent generates more
varied behavior. A tool that is genuinely low-risk could score high because the
weights are miscalibrated for that tool class. A genuinely dangerous composition
could score low because no single signal fires strongly enough.

**What Layer 3 needs:** A mechanism to adjust signal weights and thresholds
based on observed outcomes — either supervised feedback (human labels on
anomaly alerts) or unsupervised drift detection over time.

---

## Gap 2 — No Inter-Session Memory

**The problem:** The baseline model is rebuilt from the full audit log on each
`BaselineModel.build()` call. But the scorer and detector have no concept of
a *session* — a bounded period of agent activity with a start and end.

**What this means in practice:** Anomalous behavior in one session bleeds into
the baseline for the next. If an agent probes the gate aggressively in session 1,
those denied calls raise the baseline call rate for those tools in session 2,
making future probing harder to detect as a spike.

**The live run evidence:** `get_current_time` scored 0.25 on the first live run
because prior test calls inflated its baseline rate. The score is not wrong —
but it's noisy. A clean session should score closer to 0.00 for a known safe tool.

---

## Gap 3 — Sequence Signal Is Too Shallow

**The problem:** The sequence signal looks back exactly one call — the most
recent audit row. It fires if that call was denied.

**What this means in practice:** The `rename_file` exfiltration chain from
`layer1_gaps.md` looks like this:

```
read_file("/etc/passwd")          → allowed  (sequence signal: 0.0)
rename_file("/etc/passwd", "/tmp/public/")  → denied (sequence signal: 0.0)
web_search("...")                 → allowed  (sequence signal: 1.0 — fires here)
```

The signal fires on `web_search` — the third call, not the second. By that
point the damage is already done. The signal is one step too late and points
at the wrong tool.

A meaningful sequence signal needs a window of N calls, not just the
immediately preceding one. It needs to ask: "does this sequence of calls,
taken together, look like a known attack pattern?"

---

## Gap 4 — No Concept of Consequence or Reversibility

**The problem:** The scorer assigns the same weight structure to every tool
regardless of what the tool actually does if it executes. A 0.65 anomaly score
on `rename_file` and a 0.65 anomaly score on `deploy_code` trigger the same
alert — but the real-world consequence of those two calls is not the same.

**What this means in practice:** The gate currently treats all anomalies as
equal. An operator reviewing the anomaly log has no signal about which alerts
represent low-consequence events versus high-consequence, irreversible actions.

**What Layer 3 needs:** A consequence model — a classification of tools by
reversibility and blast radius that modulates the response to an anomaly.
A high-score anomaly on a reversible, low-blast-radius tool is a warning.
A high-score anomaly on an irreversible, high-blast-radius tool is a halt.

---

## Gap 5 — Parse Errors Are a Silent Governance Gap

**The problem:** When the agent produces a response that cannot be parsed as
a valid tool call, `AgentRunner` returns `outcome="parse_error"` and logs
nothing to the audit log. The call never reaches the gate.

**What this means in practice:** A model that is prompted adversarially — or
that simply hallucinates non-JSON output — creates a gap in the audit trail.
The governance stack is only as complete as the parser. An unparseable response
is an unaudited response.

**What Layer 3 needs:** Parse errors must be logged as first-class governance
events. An agent that repeatedly produces unparseable output is behaving
anomalously — that pattern is invisible to Layer 2.

---

## Summary — What Layer 3 Must Address

| Gap | Layer 2 Behavior | Layer 3 Requirement |
|-----|-----------------|---------------------|
| Static weights | Hand-tuned, not adaptive | Feedback loop to calibrate signal weights |
| No session memory | Baseline contaminated across sessions | Session-scoped behavior windows |
| Shallow sequence signal | Looks back one call | N-call window, pattern matching |
| No consequence model | All anomalies treated equally | Reversibility + blast radius classification |
| Parse errors unlogged | Silent gap in audit trail | Parse errors as first-class audit events |

The anomaly log's flagged rows are the primary input for Layer 3.
High-consequence anomalies — those on irreversible, high-blast-radius tools —
are the cases that require human-in-the-loop escalation.
That is the Layer 3 mandate.
