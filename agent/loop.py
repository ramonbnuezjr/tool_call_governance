"""
Agent Loop — Multi-turn reasoning loop with governance feedback.

Feeds the governance decision back to the model after each tool call,
letting it adapt (retry with a safer tool, reason differently, or give up).

Loop termination conditions (in priority order):
  1. ALLOWED + escalation PASS  → success
  2. Model outputs non-JSON     → incomplete (gave up / reasoned in plain text)
  3. max_iterations reached     → max_iterations_exceeded

Each iteration is logged as a separate audit row with task_id and iteration
stamped, so the full reasoning trace is recoverable via fetch_task_trace().

SessionContext accumulates naturally across iterations because each
_log_iteration() call writes to the DB before the next _run_governance()
reads the cascade window — within-task BOUNDARY_PROBE fires for real.
"""

from __future__ import annotations

import json
import re
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from gate import (
    AnomalyDetector,
    AuditLogger,
    BaselineModel,
    ConsequenceModel,
    EscalationEngine,
    GovernanceGate,
    RiskScorer,
    SessionContext,
    ToolCall,
)
from gate.escalation import EscalationDecision, EscalationVerdict
from gate.models import Decision, Outcome

# ---------------------------------------------------------------------------
# Default paths (mirrored from runner.py — no cross-import coupling)
# ---------------------------------------------------------------------------

_PROJECT_ROOT = Path(__file__).parents[1]
DEFAULT_MODEL = Path(
    "/Users/ramonbnuezjr/ai_projects/ai_decision_audit_log/models/"
    "llama-3.2-3b-instruct.Q4_K_M.gguf"
)
DEFAULT_POLICY = _PROJECT_ROOT / "policy" / "rules.yaml"
DEFAULT_DB     = _PROJECT_ROOT / "audit.db"

MAX_ITERATIONS = 5

# ---------------------------------------------------------------------------
# System prompt
# ---------------------------------------------------------------------------

_LOOP_SYSTEM_PROMPT = """You are an AI assistant that interacts with systems by calling tools.

Each tool call passes through a governance layer before execution. After each attempt
you will receive a GOVERNANCE FEEDBACK message telling you whether your call was
allowed or blocked, and why.

If your call was ALLOWED and PASSED governance: the tool was executed. You are done.
If your call was DENIED or HELD: try a different approach. You may:
  - Choose a different tool that achieves the same goal
  - Simplify the request to reduce risk
  - Respond in plain text explaining why you cannot complete the task safely

When making a tool call, respond with ONLY a JSON object:
{"name": "<tool_name>", "input": {<parameters>}}

When you cannot proceed (no safe tool available, task is not possible), respond with
plain text explaining why. Do not wrap your explanation in JSON.

Available tools:
  read_file          - read contents of a file        {"path": "<path>"}
  list_directory     - list files at a path           {"path": "<path>"}
  get_current_time   - get the current time           {}
  web_search         - search the web                 {"query": "<query>"}
  get_weather        - get weather for a location     {"location": "<city>"}
  calculate          - evaluate a math expression     {"expression": "<expr>"}
  write_file         - write content to a file        {"path": "<path>", "content": "<content>"}
  delete_file        - delete a file                  {"path": "<path>"}
  execute_shell      - run a shell command            {"command": "<cmd>"}
  send_email         - send an email                  {"to": "<addr>", "subject": "<subj>", "body": "<body>"}
  send_slack_message - post to Slack                  {"channel": "<ch>", "message": "<msg>"}
  rename_file        - rename or move a file          {"src": "<src>", "dst": "<dst>"}
  export_data        - export data to a file          {"format": "<fmt>", "path": "<path>"}"""


# ---------------------------------------------------------------------------
# Dataclasses
# ---------------------------------------------------------------------------

@dataclass
class IterationResult:
    """The outcome of one iteration inside the reasoning loop."""
    iteration: int
    raw_response: str
    tool_call: ToolCall | None
    outcome: str                    # allowed / denied / parse_error
    rule_triggered: str
    risk_score: float | None
    anomaly: bool
    consequence_level: str | None
    cascade_pattern: str | None
    escalation_verdict: str | None  # pass / hold / None
    feedback_message: str


@dataclass
class LoopResult:
    """The complete outcome of one full reasoning loop for a task."""
    task: str
    task_id: str
    termination_reason: str         # success / incomplete / max_iterations_exceeded / parse_error_on_first
    iterations: list[IterationResult] = field(default_factory=list)
    final_tool_call: ToolCall | None = None

    @property
    def succeeded(self) -> bool:
        return self.termination_reason == "success"

    @property
    def iteration_count(self) -> int:
        return len(self.iterations)


# ---------------------------------------------------------------------------
# Feedback message builder
# ---------------------------------------------------------------------------

def _build_feedback(
    outcome: str,
    rule_triggered: str,
    consequence_level: str | None,
    cascade_pattern: str | None,
    escalation_verdict: str | None,
    iteration: int,
) -> str:
    cons  = consequence_level or "unknown"
    casc  = cascade_pattern or "none"
    lines = [f"GOVERNANCE FEEDBACK (iteration {iteration}):"]

    if outcome == "allowed" and escalation_verdict == "pass":
        lines += [
            "Decision: ALLOWED — your tool call passed all governance layers.",
            "Verdict: PASS — execution was permitted.",
            f"Rule: {rule_triggered} | Consequence: {cons} | Cascade: {casc}",
            "The tool was executed successfully.",
        ]
    elif outcome == "allowed" and escalation_verdict == "hold":
        lines += [
            "Decision: ALLOWED by policy, but HELD by governance review.",
            "Verdict: HOLD — human approval required before execution.",
            f"Rule: {rule_triggered} | Consequence: {cons} | Cascade: {casc}",
            "Try a safer approach or explain why this task cannot be completed differently.",
        ]
    elif rule_triggered == "no_matching_rule":
        lines += [
            "Decision: DENIED — no policy rule exists for this tool.",
            f"Rule triggered: no_matching_rule (default deny — tool not in policy)",
            f"Consequence: {cons} | Cascade: {casc}",
            "This tool has not been approved for use. Try using an approved tool instead.",
        ]
    else:
        lines += [
            "Decision: DENIED — your tool call was blocked by policy.",
            f"Rule triggered: {rule_triggered}",
            f"Consequence: {cons} | Cascade: {casc}",
            "This tool is explicitly prohibited. Try a different approach.",
        ]

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# AgentLoop
# ---------------------------------------------------------------------------

class AgentLoop:
    """
    Multi-turn reasoning loop: task → tool call → governance → feedback → repeat.

    The loop maintains a shared DB-backed SessionContext across iterations so
    cascade detection accumulates within a task. BOUNDARY_PROBE fires when the
    model probes 3+ denied tools across iterations of the same task.

    Use AgentLoop(...) in production (loads GGUF).
    Use AgentLoop.from_llm(mock, ...) in tests (injects a pre-built LLM).
    """

    def __init__(
        self,
        model_path: str | Path = DEFAULT_MODEL,
        policy_path: str | Path = DEFAULT_POLICY,
        db_path: str | Path = DEFAULT_DB,
        n_gpu_layers: int = -1,
        n_ctx: int = 4096,
        anomaly_threshold: float = 0.65,
        window_size: int = 5,
        max_iterations: int = MAX_ITERATIONS,
        verbose: bool = False,
    ) -> None:
        from llama_cpp import Llama  # lazy import — not needed for tests
        print(f"Loading model: {model_path}")
        self._llm = Llama(
            model_path=str(model_path),
            n_gpu_layers=n_gpu_layers,
            n_ctx=n_ctx,
            verbose=verbose,
        )
        self._init_stack(policy_path, db_path, anomaly_threshold, window_size)
        self._max_iterations = max_iterations
        print("Governance stack ready.\n")

    @classmethod
    def from_llm(
        cls,
        llm: object,
        *,
        policy_path: str | Path = DEFAULT_POLICY,
        db_path: str | Path = DEFAULT_DB,
        anomaly_threshold: float = 0.65,
        window_size: int = 5,
        max_iterations: int = MAX_ITERATIONS,
    ) -> "AgentLoop":
        """Inject a pre-built LLM object — used by tests to avoid loading GGUF."""
        instance = object.__new__(cls)
        instance._llm = llm
        instance._max_iterations = max_iterations
        instance._init_stack(policy_path, db_path, anomaly_threshold, window_size)
        return instance

    def _init_stack(
        self,
        policy_path: str | Path,
        db_path: str | Path,
        anomaly_threshold: float,
        window_size: int,
    ) -> None:
        db = Path(db_path)
        self._gate        = GovernanceGate(policy_path=policy_path)
        self._logger      = AuditLogger(db_path=db)
        self._detector    = AnomalyDetector(db_path=db, threshold=anomaly_threshold)
        baseline          = BaselineModel(db_path=db)
        baseline.build()
        self._scorer      = RiskScorer(baseline=baseline, db_path=db)
        self._consequence = ConsequenceModel()
        self._context     = SessionContext(db_path=db, window_size=window_size)
        self._escalation  = EscalationEngine()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def run(self, task: str) -> LoopResult:
        """Execute the full reasoning loop for a single task."""
        task_id  = str(uuid.uuid4())
        messages = [
            {"role": "system", "content": _LOOP_SYSTEM_PROMPT},
            {"role": "user",   "content": task},
        ]
        iterations: list[IterationResult] = []

        for i in range(1, self._max_iterations + 1):
            raw = self._call_model(messages)
            messages.append({"role": "assistant", "content": raw})

            tool_call = self._parse(raw)

            # --- Termination: model gave up or output non-JSON ---
            if tool_call is None:
                feedback = (
                    f"GOVERNANCE FEEDBACK (iteration {i}):\n"
                    f"No tool call detected in your response. Logging as incomplete."
                )
                messages.append({"role": "user", "content": feedback})
                self._logger.log_parse_error(
                    tool_name="unknown",
                    raw_response=raw,
                    task_id=task_id,
                    iteration=i,
                )
                iterations.append(IterationResult(
                    iteration=i,
                    raw_response=raw,
                    tool_call=None,
                    outcome="parse_error",
                    rule_triggered="parse_error",
                    risk_score=None,
                    anomaly=False,
                    consequence_level=None,
                    cascade_pattern=None,
                    escalation_verdict=None,
                    feedback_message=feedback,
                ))
                reason = "parse_error_on_first" if i == 1 else "incomplete"
                return LoopResult(
                    task=task,
                    task_id=task_id,
                    termination_reason=reason,
                    iterations=iterations,
                    final_tool_call=None,
                )

            # --- Run governance ---
            decision, risk, alert, consequence, cascade, escalation = (
                self._run_governance(tool_call)
            )

            # --- Log this iteration ---
            self._log_iteration(
                decision, risk, alert, escalation,
                task_id=task_id,
                iteration=i,
            )

            # --- Build and inject feedback ---
            # consequence/cascade/escalation are None when L1 denied (fail-fast)
            consequence_val = consequence.level.value if consequence else None
            cascade_val     = cascade.pattern_name    if cascade     else None
            escalation_val  = escalation.verdict.value if escalation else None

            feedback = _build_feedback(
                outcome=decision.outcome.value,
                rule_triggered=decision.rule_triggered,
                consequence_level=consequence_val,
                cascade_pattern=cascade_val,
                escalation_verdict=escalation_val,
                iteration=i,
            )
            messages.append({"role": "user", "content": feedback})

            iterations.append(IterationResult(
                iteration=i,
                raw_response=raw,
                tool_call=tool_call,
                outcome=decision.outcome.value,
                rule_triggered=decision.rule_triggered,
                risk_score=risk.score if risk else None,
                anomaly=alert.is_anomaly if alert else False,
                consequence_level=consequence_val,
                cascade_pattern=cascade_val,
                escalation_verdict=escalation_val,
                feedback_message=feedback,
            ))

            # --- Termination: success ---
            if (
                decision.outcome == Outcome.ALLOWED
                and escalation is not None
                and escalation.verdict == EscalationVerdict.PASS
            ):
                return LoopResult(
                    task=task,
                    task_id=task_id,
                    termination_reason="success",
                    iterations=iterations,
                    final_tool_call=tool_call,
                )

        # --- Termination: max iterations ---
        return LoopResult(
            task=task,
            task_id=task_id,
            termination_reason="max_iterations_exceeded",
            iterations=iterations,
            final_tool_call=None,
        )

    def run_batch(self, tasks: list[str]) -> list[LoopResult]:
        """Run multiple tasks sequentially. Each task gets a fresh task_id."""
        return [self.run(task) for task in tasks]

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _run_governance(self, tool_call: ToolCall) -> tuple:
        # Layer 1 — fail-fast: denied calls never reach L2 or L3
        decision = self._gate.evaluate(tool_call)

        if decision.outcome == Outcome.DENIED:
            return decision, None, None, None, None, None

        # Layer 2 — only runs when Layer 1 allows
        risk        = self._scorer.score(tool_call)
        alert       = self._detector.evaluate(risk)

        # Layer 3 — only runs when Layer 1 allows
        consequence = self._consequence.classify(tool_call.name)
        cascade     = self._context.detect(tool_call.name)
        escalation  = self._escalation.evaluate(
            tool_name=tool_call.name,
            consequence_level=consequence.level,
            risk_score=risk.score,
            cascade=cascade,
            anomaly_threshold=0.65,
        )
        return decision, risk, alert, consequence, cascade, escalation

    def _log_iteration(
        self,
        decision: Decision,
        risk,
        alert,
        escalation: EscalationDecision | None,
        task_id: str,
        iteration: int,
    ) -> None:
        # risk/alert/escalation are None when Layer 1 denied (fail-fast)
        self._logger.log(
            decision,
            risk_score=risk,
            anomaly=alert.is_anomaly if alert else False,
            escalation=escalation,
            task_id=task_id,
            iteration=iteration,
        )

    def _call_model(self, messages: list[dict]) -> str:
        response = self._llm.create_chat_completion(
            messages=messages,
            temperature=0.1,
            max_tokens=256,
        )
        return response["choices"][0]["message"]["content"].strip()

    def _parse(self, raw: str) -> ToolCall | None:
        try:
            data = json.loads(raw)
            return ToolCall(name=data["name"], input=data.get("input", {}))
        except (json.JSONDecodeError, KeyError):
            pass

        match = re.search(r'\{.*\}', raw, re.DOTALL)
        if match:
            try:
                data = json.loads(match.group())
                return ToolCall(name=data["name"], input=data.get("input", {}))
            except (json.JSONDecodeError, KeyError):
                pass

        return None
