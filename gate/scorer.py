"""
Layer 2 — Risk Scorer

Assigns a risk score (0.0–1.0) to a tool call using three behavioral signals
derived from the Layer 1 audit log and the baseline model.

Signals (additive, capped at 1.0):

  1. Gap zone signal (weight: 0.40)
     Tool has previously appeared as no_matching_rule. The policy never
     anticipated it — that silence is itself a risk indicator.

  2. Frequency signal (weight: 0.35)
     Current call rate exceeds the tool's baseline rate by a configurable
     multiplier. A rate spike is a behavioral anomaly regardless of whether
     the tool is whitelisted.

  3. Sequence signal (weight: 0.25)
     The immediately preceding call in the audit log was a denied attempt.
     Probing the gate with a denied call followed immediately by an allowed
     call is a recognized escalation pattern.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path

from gate.baseline import BaselineModel
from gate.models import ToolCall


@dataclass
class RiskScore:
    """The scored output for a single tool call."""
    tool_name: str
    score: float                  # 0.0 (no risk) to 1.0 (maximum risk)
    gap_signal: float
    frequency_signal: float
    sequence_signal: float

    @property
    def signals(self) -> dict[str, float]:
        return {
            "gap":       self.gap_signal,
            "frequency": self.frequency_signal,
            "sequence":  self.sequence_signal,
        }


# Signal weights — must sum to 1.0
_W_GAP       = 0.40
_W_FREQUENCY = 0.35
_W_SEQUENCE  = 0.25


class RiskScorer:
    """
    Scores tool calls against a behavioral baseline.

    Layer 1 determines allow/deny.
    The RiskScorer determines how anomalous an allowed call looks.
    """

    def __init__(
        self,
        baseline: BaselineModel,
        db_path: str | Path,
        rate_multiplier: float = 3.0,
    ) -> None:
        self._baseline = baseline
        self._db_path = Path(db_path)
        self._rate_multiplier = rate_multiplier

    # ------------------------------------------------------------------
    # Scoring
    # ------------------------------------------------------------------

    def score(self, tool_call: ToolCall) -> RiskScore:
        """Compute a risk score for the given tool call."""
        gap       = self._gap_signal(tool_call.name)
        frequency = self._frequency_signal(tool_call.name)
        sequence  = self._sequence_signal()

        total = min(
            (_W_GAP * gap) + (_W_FREQUENCY * frequency) + (_W_SEQUENCE * sequence),
            1.0,
        )

        return RiskScore(
            tool_name=tool_call.name,
            score=round(total, 4),
            gap_signal=gap,
            frequency_signal=frequency,
            sequence_signal=sequence,
        )

    # ------------------------------------------------------------------
    # Signals
    # ------------------------------------------------------------------

    def _gap_signal(self, tool_name: str) -> float:
        """1.0 if tool has ever hit no_matching_rule, else 0.0."""
        return 1.0 if self._baseline.is_gap_tool(tool_name) else 0.0

    def _frequency_signal(self, tool_name: str) -> float:
        """
        Scales from 0.0 to 1.0 based on how far the recent rate exceeds
        the baseline rate. Returns 0.0 if there is no baseline to compare.
        """
        baseline_rate = self._baseline.baseline_rate(tool_name)
        if baseline_rate == 0.0:
            # No baseline — tool is new or unseen. Treat as moderate risk.
            return 0.5

        recent = self._baseline.recent_rate(tool_name)
        ratio  = recent / baseline_rate

        if ratio <= 1.0:
            return 0.0
        elif ratio >= self._rate_multiplier:
            return 1.0
        else:
            # Linear interpolation between 1x and the spike threshold
            return (ratio - 1.0) / (self._rate_multiplier - 1.0)

    def _sequence_signal(self) -> float:
        """
        1.0 if the most recent audit log entry was a denied call.
        Probing with a denied call immediately before this one is suspicious.
        """
        with sqlite3.connect(self._db_path) as conn:
            row = conn.execute(
                "SELECT outcome FROM audit_log ORDER BY id DESC LIMIT 1"
            ).fetchone()
        if row and row[0] == "denied":
            return 1.0
        return 0.0

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def explain(self, risk_score: RiskScore) -> str:
        """Return a human-readable breakdown of the score."""
        lines = [
            f"tool:      {risk_score.tool_name}",
            f"score:     {risk_score.score:.4f}",
            f"  gap      ({_W_GAP:.0%} weight): {risk_score.gap_signal:.2f}",
            f"  freq     ({_W_FREQUENCY:.0%} weight): {risk_score.frequency_signal:.2f}",
            f"  sequence ({_W_SEQUENCE:.0%} weight): {risk_score.sequence_signal:.2f}",
        ]
        return "\n".join(lines)
