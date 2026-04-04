from __future__ import annotations

import sqlite3
from pathlib import Path

from gate.models import Decision

_DDL = """
CREATE TABLE IF NOT EXISTS audit_log (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp     TEXT    NOT NULL,
    tool_name     TEXT    NOT NULL,
    input_hash    TEXT    NOT NULL,
    outcome       TEXT    NOT NULL,
    rule_triggered TEXT   NOT NULL
);
"""


class AuditLogger:
    """Persists every Decision to a SQLite audit log."""

    def __init__(self, db_path: str | Path) -> None:
        self._db_path = Path(db_path)
        self._init_db()

    def _init_db(self) -> None:
        with self._connect() as conn:
            conn.execute(_DDL)

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(self._db_path)

    # ------------------------------------------------------------------
    # Write
    # ------------------------------------------------------------------

    def log(self, decision: Decision) -> None:
        """Insert one Decision row into the audit log."""
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO audit_log
                    (timestamp, tool_name, input_hash, outcome, rule_triggered)
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    decision.timestamp.isoformat(),
                    decision.tool_name,
                    decision.input_hash,
                    decision.outcome.value,
                    decision.rule_triggered,
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
