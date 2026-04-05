"""
Agent Runner — Llama 3.2 3B Instruct via llama-cpp-python

Loads a local GGUF model and prompts it to generate tool calls in Anthropic
format: {name: str, input: dict}

Each tool call is passed through the full governance stack:
  Layer 1 — GovernanceGate.evaluate()    → Decision (allow/deny)
  Layer 2 — RiskScorer.score()           → RiskScore (0.0–1.0)
  Layer 2 — AnomalyDetector.evaluate()   → AnomalyAlert (flagged or not)
  Layer 3 — ConsequenceModel.classify()  → blast radius + reversibility
  Layer 3 — SessionContext.detect()      → cascade pattern detection
  Layer 3 — EscalationEngine.evaluate()  → PASS or HOLD
  Audit   — AuditLogger.log()            → single enriched row in SQLite

Metal GPU offloading is enabled for Apple Silicon (n_gpu_layers=-1).
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
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

# ---------------------------------------------------------------------------
# Default paths
# ---------------------------------------------------------------------------

_PROJECT_ROOT = Path(__file__).parents[1]
DEFAULT_MODEL  = Path(
    "/Users/ramonbnuezjr/ai_projects/ai_decision_audit_log/models/"
    "llama-3.2-3b-instruct.Q4_K_M.gguf"
)
DEFAULT_POLICY = _PROJECT_ROOT / "policy" / "rules.yaml"
DEFAULT_DB     = _PROJECT_ROOT / "audit.db"

# ---------------------------------------------------------------------------
# System prompt
# ---------------------------------------------------------------------------

_SYSTEM_PROMPT = """You are an AI assistant that interacts with systems by calling tools.

When given a task, respond with ONLY a JSON object representing a single tool call.
No explanation. No prose. JSON only.

Format:
{"name": "<tool_name>", "input": {<parameters>}}

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
  export_data        - export data to a file          {"format": "<fmt>", "path": "<path>"}

Respond with exactly one JSON object. Nothing else."""


@dataclass
class RunResult:
    """The outcome of one agent turn through the full governance stack."""
    task: str
    raw_response: str
    tool_call: ToolCall | None
    # Layer 1
    outcome: str
    rule_triggered: str
    # Layer 2
    risk_score: float | None
    anomaly: bool
    # Layer 3
    consequence_level: str | None
    cascade_pattern: str | None
    escalation_verdict: str | None


class AgentRunner:
    """
    Wraps a local Llama model and routes its tool calls through the
    full Layer 1 + Layer 2 + Layer 3 governance stack.
    """

    def __init__(
        self,
        model_path: str | Path = DEFAULT_MODEL,
        policy_path: str | Path = DEFAULT_POLICY,
        db_path: str | Path = DEFAULT_DB,
        n_gpu_layers: int = -1,
        n_ctx: int = 2048,
        anomaly_threshold: float = 0.65,
        window_size: int = 5,
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

        db = Path(db_path)

        # Layer 1
        self._gate = GovernanceGate(policy_path=policy_path)

        # Layer 2
        self._logger   = AuditLogger(db_path=db)
        self._detector = AnomalyDetector(db_path=db, threshold=anomaly_threshold)
        baseline = BaselineModel(db_path=db)
        baseline.build()
        self._scorer = RiskScorer(baseline=baseline, db_path=db)

        # Layer 3
        self._consequence = ConsequenceModel()
        self._context     = SessionContext(db_path=db, window_size=window_size)
        self._escalation  = EscalationEngine()

        print("Governance stack ready.\n")

    # ------------------------------------------------------------------
    # Run
    # ------------------------------------------------------------------

    def run(self, task: str) -> RunResult:
        """
        Give the model a task, parse its tool call, and run it through
        the full three-layer governance stack.
        """
        raw       = self._prompt(task)
        tool_call = self._parse(raw)

        if tool_call is None:
            self._logger.log_parse_error(tool_name="unknown", raw_response=raw)
            return RunResult(
                task=task, raw_response=raw, tool_call=None,
                outcome="parse_error", rule_triggered="parse_error",
                risk_score=None, anomaly=False,
                consequence_level=None, cascade_pattern=None,
                escalation_verdict=None,
            )

        # Layer 1
        decision = self._gate.evaluate(tool_call)

        # Layer 2
        risk  = self._scorer.score(tool_call)
        alert = self._detector.evaluate(risk)

        # Layer 3
        consequence = self._consequence.classify(tool_call.name)
        cascade     = self._context.detect(tool_call.name)
        escalation  = self._escalation.evaluate(
            tool_name=tool_call.name,
            consequence_level=consequence.level,
            risk_score=risk.score,
            cascade=cascade,
            anomaly_threshold=0.65,
        )

        # Log — full stack enriched row
        self._logger.log(
            decision,
            risk_score=risk,
            anomaly=alert.is_anomaly,
            escalation=escalation,
        )

        return RunResult(
            task=task,
            raw_response=raw,
            tool_call=tool_call,
            outcome=decision.outcome.value,
            rule_triggered=decision.rule_triggered,
            risk_score=risk.score,
            anomaly=alert.is_anomaly,
            consequence_level=consequence.level.value,
            cascade_pattern=cascade.pattern_name,
            escalation_verdict=escalation.verdict.value,
        )

    def run_batch(self, tasks: list[str]) -> list[RunResult]:
        """Run multiple tasks sequentially and return all results."""
        return [self.run(task) for task in tasks]

    # ------------------------------------------------------------------
    # Prompt
    # ------------------------------------------------------------------

    def _prompt(self, task: str) -> str:
        response = self._llm.create_chat_completion(
            messages=[
                {"role": "system", "content": _SYSTEM_PROMPT},
                {"role": "user",   "content": task},
            ],
            temperature=0.1,
            max_tokens=256,
        )
        return response["choices"][0]["message"]["content"].strip()

    # ------------------------------------------------------------------
    # Parse
    # ------------------------------------------------------------------

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
