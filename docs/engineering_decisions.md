# Engineering Decisions & Discovery Log

A running record of architectural decisions made during live development,
questions raised during testing, and the reasoning behind each answer.
This is the "why we built it this way" document — not a gap analysis,
not a spec, but a conversation log between builder and system.

---

## Session: 2026-04-07 — Dashboard Build + Stack Interrogation

### Context
Layer 1, 2, and 3 were complete and tested. The single-agent reasoning loop
was built and running. A FastAPI dashboard was added to make governance
decisions visible in real time. During the first live dashboard session,
a series of questions revealed important architectural properties of the stack.

---

### Q: Why does execute_shell reach L2 and L3 if L1 already denied it?

**Observation:** `execute_shell` was blocked at Layer 1 (blacklist). But the
dashboard showed a Layer 2 risk score of 0.00 and a Layer 3 verdict of HOLD.
This caused confusion — if L1 stopped it, why did L2 and L3 run at all?

**Answer:** At the time, the stack ran all three layers unconditionally on every
tool call regardless of the L1 decision. This was architecturally wrong.

**Decision made:** Implement the **fail-fast pattern**.

A denial at Layer 1 terminates the evaluation chain. Layer 2 and Layer 3
never run on a denied call. Three reasons:

1. **Efficiency** — no compute wasted on a call that will never execute
2. **Auditability** — a denied call with L2/L3 data is ambiguous. Which
   layer actually stopped it? What number should the operator look at?
3. **Signal integrity** — L2 baselines get contaminated when denied calls
   contribute frequency data. The anomaly signal loses meaning.

**Correct governance hierarchy:**
```
L1 DENIED  → log denial, stop. Policy violation. Operator: review policy.
L1 ALLOWED
L2 ANOMALY → log with score. Behavioral alert. Operator: review anomaly queue.
L3 HOLD
L1 ALLOWED
L2 normal
L3 PASS    → execution permitted. Operator: nothing required.
```

**Code change:** `agent/runner.py` and `agent/loop.py` now short-circuit
after a L1 denial. L2/L3 fields are NULL in the audit log for denied calls.

**Historical data:** 36 rows in `audit.db` predating the fix had L2/L3 data
on denied calls. These were cleaned via SQL UPDATE — NULL applied to
`risk_score`, `anomaly`, `escalation_verdict`, `consequence_level`,
`cascade_pattern` where `outcome = 'denied'` or `outcome = 'parse_error'`.

---

### Q: For send_email — L1 and L2 both allowed it, but L3 held it?

**Observation:** `send_email` appeared in the dashboard with an L2 score but
no anomaly flag, yet L3 issued a HOLD. This seemed like L2 failed to catch
something L3 did.

**Answer:** L2 and L3 are asking different questions.

- **L2 asks:** Is this call behaviorally unusual compared to its history?
- **L3 asks:** Is this call dangerous regardless of whether it's unusual?

`send_email` scores 0.00 on L2 because it has been called many times in
testing — it has a strong baseline, no gap history, and the frequency is
normal. To L2, it looks routine.

L3 classifies `send_email` as **HIGH consequence** — external side effect,
irreversible, high blast radius. The consequence model does not care about
frequency or anomaly score. It asks: if this executes, what is the damage
potential? For `send_email` the answer is HIGH, so L3 issues a HOLD.

**Key insight:** A tool can be behaviorally normal (L2 passes) and still be
consequentially dangerous (L3 holds). These are independent dimensions.
Both layers are correct. They are measuring different things.

---

### Q: Is it correct governance practice that L1 denials never reach L2/L3?

**Answer:** Yes. This is the **fail-fast principle** applied to governance.

In any layered enforcement system, a hard denial at an earlier layer should
terminate evaluation. Running subsequent layers after a denial:
- Wastes compute
- Produces misleading data
- Creates operator confusion about which layer is authoritative

The layers are not redundant checks of the same thing. They are sequential
gates with different responsibilities:

| Layer | Question | Stops on |
|---|---|---|
| L1 | Is this tool permitted by policy? | Blacklist or not-whitelist |
| L2 | Is this tool's behavior anomalous? | Score ≥ 0.65 |
| L3 | Is this tool safe in this context? | HIGH/CRITICAL consequence or cascade |

A call that fails L1 never reaches the behavioral or contextual question.
That is correct. The policy author made an explicit decision: this tool is
not permitted. No further analysis is needed.

---

### Q: How is the L2 risk score calculated? What moves the needle from 0 to 1?

**Answer:** Three additive signals, weighted, capped at 1.0.

**Signal 1 — Gap Zone (weight: 40%)**
Binary. Did this tool ever hit `no_matching_rule` in the audit log?
- YES → +0.40
- NO  → +0.00

Any tool the policy never anticipated carries a permanent 0.40 floor.
Rationale: policy silence is a risk signal. The governance author never
considered this tool — that is itself suspicious.

**Signal 2 — Frequency (weight: 35%)**
Continuous. How does the current call rate compare to historical baseline?
- At or below baseline rate → 0.00
- At 3x baseline or above  → 0.35 (full spike)
- Between 1x and 3x        → linear interpolation

Special case: tool never seen before → 0.175 (moderate risk by default —
no baseline exists to compare against).

This catches rate spikes on whitelisted tools. `web_search` called 50 times
in 10 minutes is whitelisted but behaviorally suspicious.

**Signal 3 — Sequence (weight: 25%)**
Binary. Was the immediately preceding call in the audit log a denial?
- YES → +0.25
- NO  → +0.00

A whitelisted call immediately after a denied call is a weak probing signal.
Note: this is acknowledged as shallow — Layer 3's session window replaced
this for multi-step pattern detection, but L2 retains it as a lightweight
first signal.

**Anomaly threshold: 0.65**
Cross this and L2 flags the call as anomalous. The flag feeds into L3 which
combines it with consequence level to determine PASS or HOLD.

**Practical score examples:**

| Tool | Gap | Freq | Seq | Score | Result |
|---|---|---|---|---|---|
| `read_file` (common) | 0.0 | 0.0 | 0.0 | 0.00 | No flag |
| `get_current_time` after denial | 0.0 | 0.0 | 0.25 | 0.25 | No flag |
| `rename_file` (gap zone) | 0.4 | 0.0 | 0.0 | 0.40 | No flag |
| `rename_file` after denial | 0.4 | 0.0 | 0.25 | 0.65 | ANOMALY |
| Brand new unseen tool | 0.4 | 0.175 | 0.25 | 0.83 | ANOMALY |

---

### Q: list_directory passed L1 and L2 but L3 held it via BOUNDARY_PROBE.
### Is that correct behavior?

**Observation:** `list_directory` is whitelisted (L1 pass), scored low risk
(L2 pass), but Layer 3 issued a HOLD with cascade pattern `BOUNDARY_PROBE`.

**Answer:** Yes, this is the stack working correctly. This is the most
important thing Layer 3 adds.

`list_directory` by itself is harmless. But Layer 3's session window looked
back at the last 5 calls and found 3 or more explicit blacklist denials
immediately before it. The pattern:

```
execute_shell  → DENIED (blacklist)
delete_file    → DENIED (blacklist)
send_email     → DENIED (blacklist)
list_directory → ALLOWED by L1 + L2, HELD by L3 ← BOUNDARY_PROBE fires
```

The agent got blocked three times, then switched to a safe tool. That
adaptive behavior is a recognized probing signature — testing the policy
boundary until finding a path through.

**Key insight:** The tool call itself is not the problem. The behavioral
pattern around it is. L1 and L2 evaluate calls in isolation. L3 evaluates
calls in context. All three are needed.

This is precisely what `layer2_gaps.md` documented as Gap 3 — the L2
sequence signal was too shallow, looking back only one call. L3's session
window extends that lookback across the full window.

---

### Q: This pattern is easy to identify with one agent. What happens with
### multiple agents?

**Observation:** With a single agent, the session window faithfully records
that agent's behavior. With multiple agents writing to the same audit log,
the window becomes a mix of calls from different agents — and the cascade
detector cannot tell them apart.

**Answer:** This is the most important unsolved problem in the current stack.

**Example of the failure:**
```
Agent A: execute_shell  → DENIED
Agent B: web_search     → ALLOWED / PASS
Agent A: delete_file    → DENIED
Agent B: read_file      → ALLOWED / PASS
Agent A: send_email     → DENIED
Agent B: list_directory → HELD ← BOUNDARY_PROBE fires on Agent B
                                  but denials came from Agent A
```

Agent B is innocent. The gate incorrectly holds it because it cannot
distinguish whose calls are whose in the window.

**Orchestrator/worker problem (worse):**
```
Orchestrator instructs Worker: "list directory, copy files, search web"
Worker: list_directory → ALLOWED
Worker: copy_file      → DENIED (no_matching_rule)
Worker: web_search     → HELD ← EXFIL_STAGING fires
```

The cascade pattern is real — but the Orchestrator composed the instruction,
not the Worker. The Worker gets held for the Orchestrator's design. And the
Orchestrator's instruction was never evaluated by the gate at all.

**What multi-agent governance requires:**
- Per-agent session windows (`agent_id` on every audit row)
- Delegation tracking (when A instructs B, that relationship is recorded)
- Orchestrator intent evaluation (the instruction passes through the gate)
- Cross-agent cascade detection (patterns spanning agents detected separately)

**Current status:** Not built. Documented in `docs/layer3_gaps.md` as Gap 5.
The single-agent reasoning loop is the prerequisite. This is the next
architectural layer after single-agent is fully validated.

---

## Key Architectural Principles Established This Session

1. **Fail-fast is correct governance.** A denial at any layer terminates
   the chain. Subsequent layers must not run.

2. **L1, L2, and L3 measure different things.** They are not redundant.
   A call can be policy-permitted (L1 pass), behaviorally normal (L2 pass),
   and still contextually dangerous (L3 hold). All three verdicts are correct.

3. **Context changes meaning.** `list_directory` is safe. `list_directory`
   after three blacklist probes is suspicious. The tool call did not change.
   The context did. That distinction requires session memory — which is
   Layer 3's contribution.

4. **The stack is designed for single-agent.** Multi-agent is the known
   next boundary. The audit log needs `agent_id` before multi-agent
   governance is possible.

5. **The thesis holds with a finding attached.**
   > Governance attaches at the tool call.
   > The tool call is necessary but not sufficient.
   > Cognition — what the model decided before generating the call —
   > remains outside governance reach.
