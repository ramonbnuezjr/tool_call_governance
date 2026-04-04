"""
Layer 3 test harness — consequence model, cascade detection, escalation.

Scenarios:
  1.  LOW consequence tool → PASS
  2.  CRITICAL consequence tool → HOLD regardless of score
  3.  HIGH consequence tool → HOLD regardless of score
  4.  MEDIUM + anomaly score >= 0.65 → HOLD
  5.  MEDIUM + score < 0.65 → PASS
  6.  Cascade: EXFIL_STAGING pattern detected → HOLD
  7.  Cascade: BOUNDARY_PROBE (3+ denials in window) → HOLD
  8.  Cascade: GAP_FLOOD (3+ no_matching_rule in window) → HOLD
  9.  Parse error logged as first-class audit event
  10. Full stack: Layer 1 + 2 + 3 logged in single enriched row
"""

from __future__ import annotations

from pathlib import Path

import pytest

from gate import (
    AuditLogger,
    BaselineModel,
    CascadeSignal,
    ConsequenceModel,
    EscalationEngine,
    EscalationVerdict,
    GovernanceGate,
    RiskScorer,
    SessionContext,
    ToolCall,
)
from gate.consequence import ConsequenceLevel

POLICY_PATH = Path(__file__).parents[1] / "policy" / "rules.yaml"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture()
def db(tmp_path):
    return tmp_path / "test_layer3.db"


@pytest.fixture()
def logger(db):
    return AuditLogger(db_path=db)


@pytest.fixture()
def gate():
    return GovernanceGate(policy_path=POLICY_PATH)


@pytest.fixture()
def consequence():
    return ConsequenceModel()


@pytest.fixture()
def escalation():
    return EscalationEngine()


@pytest.fixture()
def context(db):
    return SessionContext(db_path=db, window_size=5)


@pytest.fixture()
def scorer(db, gate, logger):
    """Seed baseline with known history."""
    for _ in range(10):
        logger.log(gate.evaluate(ToolCall(name="read_file", input={})))
    for _ in range(2):
        logger.log(gate.evaluate(ToolCall(name="rename_file", input={})))
    baseline = BaselineModel(db_path=db)
    baseline.build()
    return RiskScorer(baseline=baseline, db_path=db)


def _no_cascade() -> CascadeSignal:
    return CascadeSignal(detected=False, pattern_name="none",
                         description="", window=[])


def _cascade(name: str) -> CascadeSignal:
    return CascadeSignal(detected=True, pattern_name=name,
                         description=f"{name} detected", window=[])


# ---------------------------------------------------------------------------
# Consequence model
# ---------------------------------------------------------------------------

def test_low_consequence_pass(escalation, consequence):
    c = consequence.classify("read_file")
    d = escalation.evaluate("read_file", c.level, 0.10, _no_cascade())
    assert d.verdict == EscalationVerdict.PASS

def test_critical_consequence_always_hold(escalation, consequence):
    c = consequence.classify("execute_shell")
    d = escalation.evaluate("execute_shell", c.level, 0.00, _no_cascade())
    assert d.verdict == EscalationVerdict.HOLD
    assert c.level == ConsequenceLevel.CRITICAL

def test_high_consequence_always_hold(escalation, consequence):
    c = consequence.classify("delete_file")
    d = escalation.evaluate("delete_file", c.level, 0.00, _no_cascade())
    assert d.verdict == EscalationVerdict.HOLD
    assert c.level == ConsequenceLevel.HIGH

def test_medium_above_threshold_hold(escalation):
    d = escalation.evaluate("rename_file", ConsequenceLevel.MEDIUM, 0.65, _no_cascade())
    assert d.verdict == EscalationVerdict.HOLD

def test_medium_below_threshold_pass(escalation):
    d = escalation.evaluate("rename_file", ConsequenceLevel.MEDIUM, 0.40, _no_cascade())
    assert d.verdict == EscalationVerdict.PASS


# ---------------------------------------------------------------------------
# Cascade detection → escalation
# ---------------------------------------------------------------------------

def test_exfil_staging_hold(escalation):
    d = escalation.evaluate("web_search", ConsequenceLevel.LOW, 0.10,
                            _cascade("EXFIL_STAGING"))
    assert d.verdict == EscalationVerdict.HOLD
    assert d.cascade.pattern_name == "EXFIL_STAGING"

def test_boundary_probe_hold(gate, logger, context, escalation, db):
    """Seed 3 denied calls then check cascade fires."""
    for tool in ["execute_shell", "delete_file", "deploy_code"]:
        logger.log(gate.evaluate(ToolCall(name=tool, input={})))
    signal = context.detect("read_file")
    assert signal.detected
    assert signal.pattern_name == "BOUNDARY_PROBE"
    d = escalation.evaluate("read_file", ConsequenceLevel.LOW, 0.10, signal)
    assert d.verdict == EscalationVerdict.HOLD

def test_gap_flood_hold(gate, logger, context, escalation, db):
    """Seed 3 no_matching_rule calls then check cascade fires."""
    for tool in ["rename_file", "copy_file", "export_data"]:
        logger.log(gate.evaluate(ToolCall(name=tool, input={})))
    signal = context.detect("get_user_info")
    assert signal.detected
    assert signal.pattern_name == "GAP_FLOOD"
    d = escalation.evaluate("get_user_info", ConsequenceLevel.HIGH, 0.42, signal)
    assert d.verdict == EscalationVerdict.HOLD


# ---------------------------------------------------------------------------
# Parse error logging
# ---------------------------------------------------------------------------

def test_parse_error_logged(logger):
    """Parse errors must appear in the audit log — Gap 5 fix."""
    logger.log_parse_error(tool_name="unknown", raw_response="not valid json")
    rows = logger.fetch_all()
    assert len(rows) == 1
    assert rows[0]["outcome"] == "parse_error"
    assert rows[0]["rule_triggered"] == "parse_error"


# ---------------------------------------------------------------------------
# Full stack
# ---------------------------------------------------------------------------

def test_full_stack_enriched_row(gate, logger, scorer, context, consequence,
                                  escalation):
    """Single audit row contains all three layers of data."""
    tool_call = ToolCall(name="rename_file", input={"src": "/a", "dst": "/b"})

    from gate import AnomalyDetector
    from gate.models import Outcome

    detector = AnomalyDetector(db_path=logger._db_path, threshold=0.65)

    decision    = gate.evaluate(tool_call)
    risk        = scorer.score(tool_call)
    alert       = detector.evaluate(risk)
    cons        = consequence.classify(tool_call.name)
    cascade     = context.detect(tool_call.name)
    esc         = escalation.evaluate(tool_call.name, cons.level,
                                      risk.score, cascade)

    logger.log(decision, risk_score=risk, anomaly=alert.is_anomaly,
               escalation=esc)

    rows = logger.fetch_all()
    # Skip any parse_error rows from earlier fixture seeding
    real = [r for r in rows if r["outcome"] != "parse_error"
            and r["rule_triggered"] not in ("parse_error",)
            and r["tool_name"] == "rename_file"
            and r["consequence_level"] is not None]
    assert len(real) >= 1
    row = real[0]
    assert row["consequence_level"] == cons.level.value
    assert row["escalation_verdict"] == esc.verdict.value
    assert row["cascade_pattern"] == cascade.pattern_name
