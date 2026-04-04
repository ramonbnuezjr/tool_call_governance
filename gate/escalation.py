"""
Layer 3 — Escalation Engine

Combines consequence level, Layer 2 anomaly score, and cascade detection
into a final Layer 3 verdict: PASS or HOLD.

PASS — proceed normally, all layers satisfied
HOLD — halt execution, human approval required

Escalation matrix:

  Consequence     Anomaly Score    Cascade Detected    Verdict
  ─────────────   ─────────────    ────────────────    ───────
  LOW             any              no                  PASS
  LOW             any              yes                 HOLD
  MEDIUM          < 0.65           no                  PASS
  MEDIUM          >= 0.65          any                 HOLD
  HIGH            any              any                 HOLD
  CRITICAL        any              any                 HOLD

Rationale:
  - LOW consequence calls pass unless they're part of a detected cascade.
  - MEDIUM calls are held when Layer 2 flags them as anomalous.
  - HIGH and CRITICAL calls are always held — the blast radius is too large
    to allow without human review regardless of score.
  - CRITICAL specifically maps to OWASP LLM08: execute_shell, deploy_code,
    create_api_key, delete_api_key.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum

from gate.consequence import ConsequenceLevel
from gate.context import CascadeSignal


class EscalationVerdict(str, Enum):
    PASS = "pass"   # Execution may proceed
    HOLD = "hold"   # Human approval required


@dataclass
class EscalationDecision:
    """The Layer 3 output for a single tool call."""
    tool_name:        str
    verdict:          EscalationVerdict
    consequence_level: ConsequenceLevel
    risk_score:       float
    cascade:          CascadeSignal
    reason:           str
    timestamp:        datetime = field(default_factory=datetime.utcnow)

    @property
    def requires_human(self) -> bool:
        return self.verdict == EscalationVerdict.HOLD


class EscalationEngine:
    """
    Final arbiter in the governance stack.

    Applies the escalation matrix to determine whether a tool call
    may proceed or must be held for human review.
    """

    def evaluate(
        self,
        tool_name: str,
        consequence_level: ConsequenceLevel,
        risk_score: float,
        cascade: CascadeSignal,
        anomaly_threshold: float = 0.65,
    ) -> EscalationDecision:

        verdict, reason = self._apply_matrix(
            consequence_level, risk_score, cascade, anomaly_threshold
        )

        return EscalationDecision(
            tool_name=tool_name,
            verdict=verdict,
            consequence_level=consequence_level,
            risk_score=risk_score,
            cascade=cascade,
            reason=reason,
        )

    # ------------------------------------------------------------------
    # Escalation matrix
    # ------------------------------------------------------------------

    def _apply_matrix(
        self,
        level: ConsequenceLevel,
        score: float,
        cascade: CascadeSignal,
        threshold: float,
    ) -> tuple[EscalationVerdict, str]:

        # CRITICAL — always hold
        if level == ConsequenceLevel.CRITICAL:
            return (
                EscalationVerdict.HOLD,
                f"CRITICAL consequence tool. Requires human approval regardless "
                f"of risk score ({score:.2f}). Irreversible + high blast radius.",
            )

        # HIGH — always hold
        if level == ConsequenceLevel.HIGH:
            return (
                EscalationVerdict.HOLD,
                f"HIGH consequence tool. Requires human approval. "
                f"Irreversible action or high blast radius (score={score:.2f}).",
            )

        # Cascade detected — hold regardless of consequence or score
        if cascade.detected:
            return (
                EscalationVerdict.HOLD,
                f"Cascade pattern detected: {cascade.pattern_name}. "
                f"{cascade.description}",
            )

        # MEDIUM + anomaly score above threshold — hold
        if level == ConsequenceLevel.MEDIUM and score >= threshold:
            return (
                EscalationVerdict.HOLD,
                f"MEDIUM consequence tool with anomalous risk score "
                f"({score:.2f} >= {threshold}). Behavioral escalation.",
            )

        # LOW or MEDIUM below threshold, no cascade — pass
        return (
            EscalationVerdict.PASS,
            f"Consequence={level.value}, score={score:.2f}, "
            f"cascade={cascade.pattern_name}. Within acceptable parameters.",
        )
