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

---

## Session: 2026-04-07 — Layer Architecture Interrogation

### Context
Follow-on session after the dashboard build. Questions focused on understanding
the internal mechanics of L2 and L3 — specifically how the risk score is
computed, what "all math" means in practice, and what inputs the cascade
signal actually uses.

---

### Q: How is the L2 risk score generated? Is it based on whitelist/blacklist status?

**Answer:** Partially right, but with one important correction — the blacklist
never reaches L2.

L1 writes every decision to the SQLite audit log. L2 reads that log and does
arithmetic on it. The blacklist is irrelevant to the score because blacklisted
calls are denied at L1 and the chain stops there (fail-fast). L2 only ever
evaluates calls that L1 allowed.

The three signals:

| Signal | Weight | What triggers it |
|---|---|---|
| Gap | 40% | Tool has ever hit `no_matching_rule` in audit history |
| Frequency | 35% | Recent call rate exceeds baseline by ≥3x |
| Sequence | 25% | The immediately preceding audit log entry was denied |

Being whitelisted does not lower your score. A whitelisted tool called 10x
its baseline rate immediately after a denied call scores high regardless.

The gap signal carries a permanent floor: a tool that was ever in the gap zone
retains a 0.40 baseline on every future call — even if later added to the
whitelist. Policy silence in its history is a structural risk indicator.

---

### Q: What does "all math" mean for L2? What does the process actually look like?

**Answer:** L2 has no opinion about what a tool does. It has no understanding
of intent. The full evaluation is three independent arithmetic operations
against the audit log.

Step-by-step when a tool call arrives at L2:

**Step 1 — Build the baseline** (reads the entire audit log once)

`BaselineModel` scans every row ever written and produces:
- A `Counter`: `{"read_file": 42, "web_search": 17, "send_email": 8, ...}`
- A set of gap tools: any tool that ever hit `no_matching_rule`

No model. Standard library `Counter`.

**Step 2 — Score three signals independently**

*Gap signal* — one set lookup:
```
is "rename_file" in gap_tools?  →  YES → signal = 1.0
```

*Frequency signal* — two SQL queries and a division:
```
baseline_rate = (lifetime_count / total_calls) * window_minutes
recent_rate   = SELECT COUNT(*) WHERE timestamp >= now - 60min
ratio         = recent_rate / baseline_rate
ratio >= 3x   → signal = 1.0 (full spike)
ratio < 1x    → signal = 0.0
between       → linear interpolation
```

*Sequence signal* — one SQL query:
```
SELECT outcome FROM audit_log ORDER BY id DESC LIMIT 1
→ "denied"  →  signal = 1.0
```

**Step 3 — Add and cap:**
```
score = min((0.40 × gap) + (0.35 × freq) + (0.25 × seq), 1.0)
```

Threshold is 0.65. Cross it and L2 flags the call as anomalous.

**Key point:** L1 asks "is this tool on a list?" L2 asks "is this tool
behaving differently than it has historically?" Different questions.
Different data sources.

---

### Q: Is L3 just human-in-the-loop?

**Answer:** The human-in-the-loop is the *output* of L3, not L3 itself.
L3 is two sub-components of deterministic logic that run before a human
ever sees anything.

**Sub-component 1 — Consequence Model** (`consequence.py`)

A dict lookup. Every known tool is pre-classified on two dimensions:
- Blast radius: LOW / MEDIUM / HIGH
- Reversibility: REVERSIBLE / IRREVERSIBLE

Those two produce a consequence level: LOW, MEDIUM, HIGH, or CRITICAL.
Hard-coded. No math, no model. If the tool isn't in the table, it defaults
to HIGH — unknown impact is treated as potentially severe.

**Sub-component 2 — Escalation Engine** (`escalation.py`)

Takes three inputs and runs a decision matrix:

```
CRITICAL consequence  →  always HOLD
HIGH consequence      →  always HOLD
Cascade detected      →  always HOLD
MEDIUM + score ≥ 0.65 →  HOLD
everything else       →  PASS
```

If the verdict is HOLD, that's when a human is required.

**Corrected mental model for the full stack:**

| Layer | Mechanism | Question asked |
|---|---|---|
| L1 | YAML file lookup | Is this tool permitted by policy? |
| L2 | SQLite counters + arithmetic | Is this tool behaving abnormally vs. history? |
| L3 | Dict lookup + decision matrix | Is this tool dangerous in this context? |
| Human | Approval gate | Should this HOLD be released or blocked? |

No LLM anywhere in the stack. The agent generating the tool call may be an
LLM — the governance stack evaluating it is entirely deterministic.

---

### Q: Is the cascade signal built from the consequence level and L2 risk score?

**Answer:** No. The cascade signal is a third independent input to the
escalation engine — computed entirely from the audit log history, specifically
the last 5 rows. It has nothing to do with consequence level or risk score.

The escalation engine receives three separate inputs:

```
consequence_level  ←  consequence table dict lookup (tool name)
risk_score         ←  L2 arithmetic (three-signal math)
cascade            ←  SessionContext audit log window scan
```

The cascade detector scans the last 5 audit log rows and checks for four
named patterns:

| Pattern | What it looks for |
|---|---|
| `EXFIL_STAGING` | read → stage (rename/copy/export) → transmit (email/http) |
| `BOUNDARY_PROBE` | 3+ blacklist denials in the window |
| `CRED_ESCALATION` | any `create_api_key` or `delete_api_key` in the window |
| `GAP_FLOOD` | 3+ `no_matching_rule` hits in the window |

The cascade signal can trigger a HOLD on its own — independent of what
consequence level or risk score say. This is why `list_directory` gets held
after three blacklist probes: LOW consequence, 0.00 risk score, but
`BOUNDARY_PROBE` fires from the window and forces HOLD regardless.

**Why three independent inputs matter:** Each input catches a different failure
mode. A tool can be policy-permitted (L1 pass), behaviorally normal (L2 pass),
low consequence, and still be part of a dangerous sequence (cascade HOLD). No
single input is sufficient. All three are needed.

---

## Key Architectural Principles Established This Session

1. **The blacklist is L1-only.** Denied calls never contribute to L2 scores.
   They contribute to L2's *inputs* only as history — specifically the
   sequence signal (was the last call denied?) and BOUNDARY_PROBE
   (how many denials in the window?).

2. **L2 is behavioral, not semantic.** It has no understanding of what a
   tool does. It only knows how often it has been called and under what
   conditions. This is both its strength (fast, auditable) and its limit
   (blind to intent).

3. **L3's three inputs are fully independent.** Consequence level, risk score,
   and cascade signal are computed from different sources and answer different
   questions. Any one of them can force a HOLD without the others.

4. **No LLM in the governance stack.** The agent is an LLM. The gate is not.
   This is deliberate — deterministic enforcement cannot depend on a model
   that can be substituted, fine-tuned, or prompted differently.

---

## Session: 2026-04-07 — Cascade Patterns, Exfil Staging, and the Role of Each Layer

### Context
Follow-on session. Questions focused on how allowed tool calls flow through L2
and L3, why low-risk tools still appear in L3, what tool chaining means in this
context, and how the EXFIL_STAGING cascade pattern works and is implemented.

---

### Q: Allowed tool calls like calculate, web_search, get_current_time show up
### in L3 but not visibly in L2. Is that normal?

**Answer:** Yes. All three tools do go through L2 — the risk score is computed
and written to the audit log. But because they are common, well-behaved tools
with no gap history, no rate spike, and no post-denial sequence, they score
0.00. L2 runs and finds nothing interesting, producing no anomaly flag. If the
dashboard highlights only anomalies, L2 appears silent even though it evaluated
the call.

L3 always shows something for allowed calls because it writes two values that
are never null: `consequence_level` (these tools are LOW) and
`escalation_verdict` (PASS). So the audit row for a clean `calculate` call is:

```
outcome:            allowed
risk_score:         0.0      ← L2 ran, scored it, nothing flagged
anomaly:            0        ← L2 found nothing
consequence_level:  low      ← L3 classified it
cascade_pattern:    none     ← L3 checked, no pattern
escalation_verdict: pass     ← L3 verdict
```

**Key distinction:** L2 and L3 are not running in order to document — they
are running in order to decide. The audit log is what they write after
deciding. The historical record is a side effect of evaluation, not the
purpose of it.

---

### Q: Is tool chaining what makes the multi-layer evaluation valuable?
### Like, web_search is allowed at L1 but L2 and L3 catch it for other reasons?

**Answer:** Almost — with one clarification on what "tool chaining" means here.

Tool chaining is what the **agent** does — calling `web_search`, then
`read_file`, then `send_email` across multiple turns. Each is a separate,
individual tool call. The governance stack evaluates them one at a time.

But L3's cascade detector is exactly what sees the chain even though the stack
evaluates calls individually. L3 looks back at the last 5 audit log rows
before deciding on the current call. So:

```
Turn 1: read_file   → L1 allowed, L2 clean, L3 pass
Turn 2: copy_file   → L1 allowed, L2 clean, L3 pass
Turn 3: web_search  → L1 allowed, L2 clean, L3 — HOLD (EXFIL_STAGING)
```

`web_search` by itself is fine. But the window contained `read_file` →
`copy_file` → `web_search`, which matches the exfil pattern.

**Correct mental model — what each layer catches:**
- **L1** — is this tool permitted by policy? (per call, no history)
- **L2** — is this tool's call rate suspicious? (per tool, rate history)
- **L3** — is this sequence of calls suspicious? (session window, pattern match)

L1 allowing something is never the final word.

---

### Q: What is EXFIL_STAGING? Is it a library?

**Answer:** No library. Hand-coded pattern encoded as three Python sets.

"Exfil" is short for data exfiltration — an agent stealing data out of the
system. EXFIL_STAGING is the three-step sequence present in nearly every
real data theft:

```
Step 1 — READ:     read_file, list_directory
          Find and access the data.

Step 2 — STAGE:    copy_file, rename_file, export_data, write_file
          Move it somewhere accessible or in a portable format.

Step 3 — TRANSMIT: send_email, make_http_request, web_search, send_slack_message
          Send it out of the system.
```

Each step individually is innocent and whitelisted. All three in sequence
within the session window is a recognized attack pattern.

The implementation in `gate/context.py`:

```python
_EXFIL_READ     = {"read_file", "list_directory"}
_EXFIL_STAGE    = {"rename_file", "copy_file", "export_data", "write_file"}
_EXFIL_TRANSMIT = {"web_search", "make_http_request", "send_email", "send_slack_message"}

def _detect_exfil_staging(self, names, rules):
    has_read     = any(n in _EXFIL_READ     for n in names)
    has_stage    = any(n in _EXFIL_STAGE    for n in names)
    has_transmit = any(n in _EXFIL_TRANSMIT for n in names)

    if has_read and has_stage and has_transmit:
        → EXFIL_STAGING detected
```

Three sets. Three membership checks. If all three buckets are represented
in the window — pattern fires.

The pattern names (`EXFIL_STAGING`, `BOUNDARY_PROBE`, `CRED_ESCALATION`,
`GAP_FLOOD`) come from security research and OWASP LLM08. The implementation
is domain knowledge encoded manually. No library, no ML, no external
dependency.

**Known limitation:** The patterns are only as good as what was anticipated
when writing the code. A novel exfil sequence using tools not in those sets
would pass undetected. This is the fundamental constraint of hand-coded
pattern detection — it cannot anticipate what it was never designed to see.

---

## Key Architectural Principles Established This Session

1. **L1 allowing a call is never the final word.** It means the tool is
   permitted by policy. L2 and L3 still evaluate it independently.

2. **L2 and L3 run to decide, not to document.** The audit record is a
   side effect of evaluation. Every allowed call gets a full row regardless
   of whether the verdict is interesting.

3. **The cascade detector sees sequences the other layers cannot.** L1 and
   L2 evaluate each call in isolation. L3 evaluates each call in the context
   of what preceded it. Both perspectives are required.

4. **Hand-coded patterns are fast and auditable but not adaptive.** EXFIL_STAGING
   fires reliably on known sequences. It is blind to novel compositions that
   use unanticipated tools. That gap is the argument for a probabilistic or
   learned detection layer beyond what exists here.
