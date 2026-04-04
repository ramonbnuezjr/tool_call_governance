"""
Layer 2 test harness — behavioral risk scoring and anomaly detection.

Scenarios:
  1. Known safe tool scores low risk
  2. Gap zone tool (no_matching_rule history) scores elevated risk
  3. Frequency spike on a whitelisted tool triggers anomaly
  4. Denied call immediately preceding raises sequence signal
  5. Full stack: Layer 1 decision + Layer 2 risk score logged together
"""

from __future__ import annotations

from pathlib import Path

import pytest

from gate import (
    AnomalyDetector,
    AuditLogger,
    BaselineModel,
    GovernanceGate,
    RiskScorer,
    ToolCall,
)
from gate.models import Outcome

POLICY_PATH = Path(__file__).parents[1] / "policy" / "rules.yaml"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture()
def db(tmp_path):
    return tmp_path / "test_layer2.db"


@pytest.fixture()
def logger(db):
    return AuditLogger(db_path=db)


@pytest.fixture()
def gate():
    return GovernanceGate(policy_path=POLICY_PATH)


@pytest.fixture()
def baseline(db, logger, gate):
    """Seed the audit log with a known call history, then build the baseline."""
    # Seed: read_file called 10x, web_search 5x, rename_file 2x (gap zone)
    for _ in range(10):
        logger.log(gate.evaluate(ToolCall(name="read_file", input={})))
    for _ in range(5):
        logger.log(gate.evaluate(ToolCall(name="web_search", input={})))
    for _ in range(2):
        logger.log(gate.evaluate(ToolCall(name="rename_file", input={})))

    model = BaselineModel(db_path=db, window_minutes=60)
    model.build()
    return model


@pytest.fixture()
def scorer(baseline, db):
    return RiskScorer(baseline=baseline, db_path=db, rate_multiplier=3.0)


@pytest.fixture()
def detector(db):
    return AnomalyDetector(db_path=db, threshold=0.65)


# ---------------------------------------------------------------------------
# Scenario 1 — Known safe tool scores low risk
# ---------------------------------------------------------------------------

def test_known_safe_tool_low_risk(scorer):
    """read_file has a strong baseline — no gap, no spike. Score should be low."""
    risk = scorer.score(ToolCall(name="read_file", input={}))

    assert risk.gap_signal == 0.0
    assert risk.score < 0.65
    print(f"\n  read_file risk score: {risk.score:.4f}")


# ---------------------------------------------------------------------------
# Scenario 2 — Gap zone tool scores elevated risk
# ---------------------------------------------------------------------------

def test_gap_tool_elevated_risk(scorer):
    """rename_file has appeared as no_matching_rule — gap signal fires."""
    risk = scorer.score(ToolCall(name="rename_file", input={}))

    assert risk.gap_signal == 1.0
    assert risk.score >= 0.40   # gap signal weight alone is 0.40
    print(f"\n  rename_file risk score: {risk.score:.4f}")


# ---------------------------------------------------------------------------
# Scenario 3 — Unknown tool (never seen) gets moderate frequency signal
# ---------------------------------------------------------------------------

def test_unseen_tool_moderate_risk(scorer):
    """A brand-new tool has no baseline — frequency signal defaults to 0.5."""
    risk = scorer.score(ToolCall(name="export_data", input={}))

    assert risk.frequency_signal == 0.5
    print(f"\n  export_data risk score: {risk.score:.4f}")


# ---------------------------------------------------------------------------
# Scenario 4 — Sequence signal fires after a denied call
# ---------------------------------------------------------------------------

def test_sequence_signal_after_denial(gate, logger, scorer):
    """A denied call immediately before this one raises the sequence signal."""
    # Log a denied call first
    logger.log(gate.evaluate(ToolCall(name="execute_shell", input={})))

    risk = scorer.score(ToolCall(name="read_file", input={}))
    assert risk.sequence_signal == 1.0
    print(f"\n  read_file after execute_shell: sequence={risk.sequence_signal}, score={risk.score:.4f}")


# ---------------------------------------------------------------------------
# Scenario 5 — Full stack: Layer 1 + Layer 2 logged together
# ---------------------------------------------------------------------------

def test_full_stack_logged(gate, logger, scorer, detector):
    """
    Full stack pass-through:
    Layer 1 evaluates → Layer 2 scores → anomaly check → all logged together.
    """
    tool_call = ToolCall(name="rename_file", input={"src": "/a", "dst": "/b"})

    # Layer 1
    decision = gate.evaluate(tool_call)
    assert decision.outcome == Outcome.DENIED

    # Layer 2
    risk  = scorer.score(tool_call)
    alert = detector.evaluate(risk)

    # Log with risk score attached
    logger.log(decision, risk_score=risk, anomaly=alert.is_anomaly)

    # Verify audit row contains risk data
    rows = logger.fetch_all()
    assert rows[0]["risk_score"] == risk.score
    assert rows[0]["anomaly"] == (1 if alert.is_anomaly else 0)

    print(f"\n  rename_file — outcome={decision.outcome.value}, "
          f"score={risk.score:.4f}, anomaly={alert.is_anomaly}")
