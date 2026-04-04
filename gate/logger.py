from __future__ import annotations

import sqlite3
from pathlib import Path

from datetime import datetime

from gate.escalation import EscalationDecision
from gate.models import Decision, Outcome
from gate.scorer import RiskScore

_DDL = """
CREATE TABLE IF NOT EXISTS audit_log (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp      TEXT    NOT NULL,
    tool_name      TEXT    NOT NULL,
    input_hash     TEXT    NOT NULL,
    outcome        TEXT    NOT NULL,
    rule_triggered TEXT    NOT NULL,
    risk_score     REAL    DEFAULT NULL,
    anomaly        INTEGER DEFAULT 0
);
"""

_MIGRATE = """
ALTER TABLE audit_log ADD COLUMN risk_score REAL    DEFAULT NULL;
"""
_MIGRATE_ANOMALY = """
ALTER TABLE audit_log ADD COLUMN anomaly    INTEGER DEFAULT 0;
"""
_MIGRATE_ESCALATION = """
ALTER TABLE audit_log ADD COLUMN escalation_verdict TEXT DEFAULT NULL;
"""
_MIGRATE_CASCADE = """
ALTER TABLE audit_log ADD COLUMN cascade_pattern    TEXT DEFAULT NULL;
"""
_MIGRATE_CONSEQUENCE = """
ALTER TABLE audit_log ADD COLUMN consequence_level  TEXT DEFAULT NULL;
"""


class AuditLogger:
    """Persists every Decision to a SQLite audit log."""

    def __init__(self, db_path: str | Path) -> None:
        self._db_path = Path(db_path)
        self._init_db()

    def _init_db(self) -> None:
        with self._connect() as conn:
            conn.execute(_DDL)
            for stmt in (
                _MIGRATE, _MIGRATE_ANOMALY,
                _MIGRATE_ESCALATION, _MIGRATE_CASCADE, _MIGRATE_CONSEQUENCE,
            ):
                try:
                    conn.execute(stmt)
                except Exception:
                    pass  # Column already exists — safe to ignore

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(self._db_path)

    # ------------------------------------------------------------------
    # Write
    # ------------------------------------------------------------------

    def log(
        self,
        decision: Decision,
        risk_score: RiskScore | None = None,
        anomaly: bool = False,
        escalation: EscalationDecision | None = None,
    ) -> None:
        """Insert one Decision row into the audit log.

        Backwards compatible — Layer 1-only callers omit all optional args.
        Layer 3 callers pass escalation to capture the full stack verdict.
        """
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO audit_log
                    (timestamp, tool_name, input_hash, outcome, rule_triggered,
                     risk_score, anomaly, escalation_verdict,
                     cascade_pattern, consequence_level)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    decision.timestamp.isoformat(),
                    decision.tool_name,
                    decision.input_hash,
                    decision.outcome.value,
                    decision.rule_triggered,
                    risk_score.score if risk_score else None,
                    1 if anomaly else 0,
                    escalation.verdict.value if escalation else None,
                    escalation.cascade.pattern_name if escalation else None,
                    escalation.consequence_level.value if escalation else None,
                ),
            )

    def log_parse_error(self, tool_name: str, raw_response: str) -> None:
        """
        Log a parse error as a first-class audit event.
        Fixes Layer 2 Gap 5 — parse errors were previously silent.
        """
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO audit_log
                    (timestamp, tool_name, input_hash, outcome, rule_triggered)
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    datetime.utcnow().isoformat(),
                    tool_name or "unknown",
                    "parse_error",
                    "parse_error",
                    "parse_error",
                ),
            )

    # ------------------------------------------------------------------
    # Read
    # ------------------------------------------------------------------

    def fetch_all(self) -> list[dict]:
        """Return all audit rows as dicts, newest first."""
        with self._connect() as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT * FROM audit_log ORDER BY id DESC"
            ).fetchall()
        return [dict(row) for row in rows]

    def fetch_denied(self) -> list[dict]:
        """Return only denied decisions, newest first."""
        with self._connect() as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT * FROM audit_log WHERE outcome = 'denied' ORDER BY id DESC"
            ).fetchall()
        return [dict(row) for row in rows]

    def fetch_held(self) -> list[dict]:
        """Return decisions where Layer 3 issued a HOLD verdict."""
        with self._connect() as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                """
                SELECT * FROM audit_log
                WHERE escalation_verdict = 'hold'
                ORDER BY id DESC
                """
            ).fetchall()
        return [dict(row) for row in rows]

    def fetch_cascades(self) -> list[dict]:
        """Return decisions where a cascade pattern was detected."""
        with self._connect() as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                """
                SELECT * FROM audit_log
                WHERE cascade_pattern IS NOT NULL
                AND cascade_pattern != 'none'
                ORDER BY id DESC
                """
            ).fetchall()
        return [dict(row) for row in rows]

    def fetch_gaps(self) -> list[dict]:
        """Return decisions that hit the default-deny (no_matching_rule), newest first."""
        with self._connect() as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                """
                SELECT * FROM audit_log
                WHERE rule_triggered = 'no_matching_rule'
                ORDER BY id DESC
                """
            ).fetchall()
        return [dict(row) for row in rows]
