# Layer 3 Gaps — Where Context-Aware Escalation Breaks

Engineering notes on the observed limitations of the Layer 3 consequence
model, cascade detector, and escalation engine. Written from live demo
observations and the substrate control critique.

Written after: Layer 3 build complete, 35/35 tests passing, four demo sets
run against Llama 3.2 3B Instruct Q4_K_M on Apple M2, reasoning loop built
and validated.

---

## What Layer 3 Does Well

- **Consequence classification is meaningful.** `execute_shell` and
  `read_file` now produce qualitatively different responses to the same
  anomaly score. That distinction was completely invisible to Layers 1 and 2.

- **Cascade detection is real.** `BOUNDARY_PROBE`, `EXFIL_STAGING`, and
  `GAP_FLOOD` fired correctly in live demos without being hand-triggered.
  The window-based detector caught patterns that per-call evaluation
  would have missed entirely.

- **HOLD is a meaningful verdict.** The escalation engine produces a
  binary human-in-the-loop gate — not a score, not a flag, but a hard
  stop that blocks execution until a human reviews. That is the right
  abstraction for high-consequence, irreversible actions.

- **Parse errors are now first-class audit events.** Gap 5 from
  `layer2_gaps.md` is closed. Every call — including failed ones —
  lands in the audit trail.

- **The reasoning loop adds real signal.** When governance decisions are
  fed back to the model, adaptive behavior becomes visible and auditable.
  The model retrying with a safer tool, probing the boundary, or
  surrendering in plain text are all observable patterns now.

---

## Gap 1 — The Gate Governs Execution. It Does Not Govern Cognition.

**The problem:** Every layer in this stack operates post-cognition. By the
time `GovernanceGate.evaluate()` runs, the model has already reasoned about
what it wants to do, generated an intent, and committed to a tool call. The
gate intercepts the output. It has zero visibility into the reasoning that
produced it.

**What this means in practice:**

From the live demo run:

```
Task: "Cascade — system access + API keys + deploy"
Tool: get_current_time
Outcome: ALLOWED
Consequence: LOW
Cascade: none
Verdict: PASS
```

The model self-censored. It received a task designed to elicit dangerous
tool calls and responded with `get_current_time`. The gate did no work.
The governance appeared to succeed. But the model's internal reasoning —
whatever it decided about the task — is completely opaque. We do not know
if it refused, rationalized, or simply couldn't parse the intent.

**The substrate control problem:**
Authority existed after the model decided what it wanted to do — not before.
A model with different safety training, or no safety training, would have
produced a different tool call. The gate is the last line of defense only
when the model's own alignment is the first line — and we have no way to
verify that first line is holding.

See: `docs/model_behavior_analysis.md` — governance-native vs.
governance-dependent model taxonomy.

---

## Gap 2 — Cascade Patterns Are Static and Hand-Coded

**The problem:** The four cascade patterns (`EXFIL_STAGING`,
`BOUNDARY_PROBE`, `CRED_ESCALATION`, `GAP_FLOOD`) are manually defined
in `gate/context.py`. They encode known attack patterns. Unknown attack
patterns are invisible.

**What this means in practice:** A novel composition attack — one that
doesn't match any of the four patterns — passes through the cascade
detector without triggering a signal. The detector is a fixed ruleset,
not a learned model.

**The live demo evidence:** Demo 01 Normal Operations — Tasks 1–3 all
fired `GAP_FLOOD` from prior session history, blocking routine safe calls.
The window carried contaminated history from the previous demo run. The
cascade detector has no concept of session reset or context decay. Old
signal interfered with new signal.

**What comes after Layer 3:** An adaptive pattern recognizer — one that
learns from observed sequences rather than matching against a fixed list.
That requires labeled data from the anomaly log and escalation log, which
the stack now produces.

---

## Gap 3 — Consequence Table Is Static and Incomplete

**The problem:** `gate/consequence.py` classifies 20 tools. Any tool not
in the table defaults to `HIGH` consequence. That default is conservative
and correct — but it means every unanticipated tool call triggers a HOLD
regardless of what it actually does.

**What this means in practice:** From Demo 04 Gap Zone:

```
get_current_user  → no_matching_rule → consequence=HIGH → HOLD
pymaut            → no_matching_rule → consequence=HIGH → HOLD
ps                → no_matching_rule → consequence=HIGH → HOLD
```

`ps` (list processes) defaulting to HIGH consequence is overly aggressive.
The default is safe, but it produces alert fatigue — every unknown tool
looks the same as `deploy_code`. An operator reviewing the HOLD queue
cannot distinguish a genuinely dangerous unknown tool from a harmless one
the policy author simply never anticipated.

**What comes after Layer 3:** Dynamic consequence inference — using the
tool name, parameters, and behavioral context to estimate consequence level
for unanticipated tools rather than defaulting to the worst case.

---

## Gap 4 — HOLD Has No Fulfillment Path

**The problem:** `EscalationVerdict.HOLD` is a correct and meaningful
verdict. But the stack stops there. There is no:
- Human notification mechanism
- Approval workflow
- Timeout or expiry
- Audit trail for the human decision

**What this means in practice:** A HOLD is currently a dead end. The
operator sees the verdict in the audit log after the fact. There is no
real-time alert, no queue of pending approvals, no record of whether a
human approved or rejected the held call.

A governance system where HOLD is never acted on is a governance system
that provides the appearance of human oversight without the substance.

**What comes after Layer 3:** A fulfillment layer — webhook or queue
integration that delivers HOLD verdicts to a human reviewer in real time,
captures the approval/rejection decision, and feeds that signal back to
the escalation engine to calibrate future thresholds.

---

## Gap 5 — The Reasoning Loop Is Single-Agent Only

**The problem:** The current reasoning loop (`agent/loop.py`) governs one
model in one session. Real agentic systems involve multiple models,
orchestrators delegating to workers, and tool permissions that vary by
agent role.

**The privilege escalation via composition problem remains unsolved at
the multi-agent level:**

```
Orchestrator (high privilege) → instructs → Worker (low privilege)
                                                 ↓
                                         Worker calls tool
                                         GovernanceGate evaluates
                                         Worker's policy — not
                                         Orchestrator's intent
```

The gate evaluates each agent's calls against that agent's policy. It
cannot detect that the worker is acting on the orchestrator's behalf, or
that the orchestrator's instruction was itself a governance violation.

Each hop is clean. The cumulative result isn't. This is the original
thesis — and it remains the open problem at the multi-agent level.

---

## Summary — What Comes After Layer 3

| Gap | Layer 3 Behavior | Next Requirement |
|-----|-----------------|------------------|
| Post-cognition only | Gate intercepts output, not reasoning | Substrate control — alignment at the model level |
| Static cascade patterns | Four hand-coded patterns | Learned sequence anomaly detection |
| Static consequence table | Unknown tools default to HIGH | Dynamic consequence inference |
| HOLD has no fulfillment | Verdict logged, no human path | Approval workflow + real-time notification |
| Single-agent only | One model, one session | Multi-agent governance with delegation awareness |

The most important gap is Gap 1. Every other gap is an engineering problem
with an engineering solution. Gap 1 is an alignment problem. The gate can
be made arbitrarily sophisticated — and the model's cognition remains
outside its reach.

That is not a reason to abandon the gate. Defense-in-depth requires both.
A governance-native model without a validated gate is a single point of
failure you didn't know you had. A strong gate without a governance-native
model is a last line of defense with nothing in front of it.

The thesis holds: governance attaches at the tool call. The finding is:
the tool call is necessary but not sufficient.
