"""
Layer 1 test harness — three required scenarios from CLAUDE.md.

Scenario 1: whitelist hit  -> outcome=allowed,  rule_triggered=<rule name>
Scenario 2: blacklist hit  -> outcome=denied,   rule_triggered=<rule name>
Scenario 3: no match       -> outcome=denied,   rule_triggered=no_matching_rule

Fixtures are loaded from tests/fixtures/sample_calls.yaml.
The GovernanceGate is pointed at policy/rules.yaml.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from gate import AuditLogger, GovernanceGate, ToolCall
from gate.models import Outcome

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

POLICY_PATH = Path(__file__).parents[1] / "policy" / "rules.yaml"
FIXTURES_PATH = Path(__file__).parent / "fixtures" / "sample_calls.yaml"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def load_fixture(scenario: str) -> dict:
    fixtures = yaml.safe_load(FIXTURES_PATH.read_text())
    for entry in fixtures:
        if entry["scenario"] == scenario:
            return entry
    raise KeyError(f"Fixture '{scenario}' not found in sample_calls.yaml")


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def gate():
    return GovernanceGate(policy_path=POLICY_PATH)


@pytest.fixture()
def logger(tmp_path):
    return AuditLogger(db_path=tmp_path / "test_audit.db")


# ---------------------------------------------------------------------------
# Scenario 1 — Whitelist hit
# ---------------------------------------------------------------------------

def test_whitelist_hit(gate, logger):
    fixture = load_fixture("whitelist_hit")
    tool_call = ToolCall(**fixture["tool_call"])

    decision = gate.evaluate(tool_call)
    logger.log(decision)

    assert decision.outcome == Outcome.ALLOWED
    assert decision.rule_triggered == fixture["expected"]["rule_triggered"]
    assert decision.tool_name == tool_call.name

    rows = logger.fetch_all()
    assert len(rows) == 1
    assert rows[0]["outcome"] == "allowed"


# ---------------------------------------------------------------------------
# Scenario 2 — Blacklist hit
# ---------------------------------------------------------------------------

def test_blacklist_hit(gate, logger):
    fixture = load_fixture("blacklist_hit")
    tool_call = ToolCall(**fixture["tool_call"])

    decision = gate.evaluate(tool_call)
    logger.log(decision)

    assert decision.outcome == Outcome.DENIED
    assert decision.rule_triggered == fixture["expected"]["rule_triggered"]
    assert decision.tool_name == tool_call.name

    rows = logger.fetch_denied()
    assert len(rows) == 1
    assert rows[0]["rule_triggered"] == "execute_shell"


# ---------------------------------------------------------------------------
# Scenario 3 — No matching rule (default-deny)
# ---------------------------------------------------------------------------

def test_no_matching_rule(gate, logger):
    fixture = load_fixture("no_matching_rule")
    tool_call = ToolCall(**fixture["tool_call"])

    decision = gate.evaluate(tool_call)
    logger.log(decision)

    assert decision.outcome == Outcome.DENIED
    assert decision.rule_triggered == "no_matching_rule"

    gaps = logger.fetch_gaps()
    assert len(gaps) == 1
    assert gaps[0]["tool_name"] == "rename_file"


# ---------------------------------------------------------------------------
# Audit log integrity
# ---------------------------------------------------------------------------

def test_every_decision_is_logged(gate, logger):
    """All three scenarios produce exactly one audit row each."""
    calls = [
        ToolCall(name="read_file", input={"path": "/tmp/a.txt"}),
        ToolCall(name="execute_shell", input={"command": "ls"}),
        ToolCall(name="rename_file", input={"src": "/a", "dst": "/b"}),
    ]

    for tc in calls:
        logger.log(gate.evaluate(tc))

    rows = logger.fetch_all()
    assert len(rows) == 3

    outcomes = {r["tool_name"]: r["outcome"] for r in rows}
    assert outcomes["read_file"] == "allowed"
    assert outcomes["execute_shell"] == "denied"
    assert outcomes["rename_file"] == "denied"
