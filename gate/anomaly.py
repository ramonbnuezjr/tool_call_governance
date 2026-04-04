"""
Layer 2 — Anomaly Detector

Compares a RiskScore against a threshold and emits an AnomalyAlert when
the score exceeds it. Persists all alerts to a dedicated SQLite table so
they can be queried independently of the Layer 1 audit log.

Default threshold: 0.65
  - Score < 0.65  → normal, no alert
  - Score >= 0.65 → anomaly, alert logged and returned
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from gate.scorer import RiskScore


_DDL = """
CREATE TABLE IF NOT EXISTS anomaly_log (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp     TEXT    NOT NULL,
    tool_name     TEXT    NOT NULL,
    score         REAL    NOT NULL,
    gap_signal    REAL    NOT NULL,
    frequency_signal REAL NOT NULL,
    sequence_signal  REAL NOT NULL,
    threshold     REAL    NOT NULL
);
"""

DEFAULT_THRESHOLD = 0.65


@dataclass
class AnomalyAlert:
    """Emitted when a tool call's risk score exceeds the threshold."""
    tool_name: str
    score: float
    threshold: float
    gap_signal: float
    frequency_signal: float
    sequence_signal: float
    timestamp: datetime = field(default_factory=datetime.utcnow)

    @property
    def is_anomaly(self) -> bool:
        return self.score >= self.threshold


class AnomalyDetector:
    """
    Evaluates RiskScores and persists alerts for anomalous tool calls.

    Sits at the top of the Layer 1 + Layer 2 stack:
      Layer 1  →  GovernanceGate.evaluate()  →  Decision (allow/deny)
      Layer 2  →  RiskScorer.score()         →  RiskScore (0.0–1.0)
      Layer 2  →  AnomalyDetector.evaluate() →  AnomalyAlert (flagged or not)
    """

    def __init__(
        self,
        db_path: str | Path,
        threshold: float = DEFAULT_THRESHOLD,
    ) -> None:
        self._db_path = Path(db_path)
        self._threshold = threshold
        self._init_db()

    # ------------------------------------------------------------------
    # Evaluation
    # ------------------------------------------------------------------

    def evaluate(self, risk_score: RiskScore) -> AnomalyAlert:
        """
        Build an AnomalyAlert from a RiskScore.
        If the score exceeds the threshold, persist it to the anomaly log.
        """
        alert = AnomalyAlert(
            tool_name=risk_score.tool_name,
            score=risk_score.score,
            threshold=self._threshold,
            gap_signal=risk_score.gap_signal,
            frequency_signal=risk_score.frequency_signal,
            sequence_signal=risk_score.sequence_signal,
        )

        if alert.is_anomaly:
            self._log(alert)

        return alert

    # ------------------------------------------------------------------
    # Read
    # ------------------------------------------------------------------

    def fetch_all(self) -> list[dict]:
        """Return all anomaly alerts, newest first."""
        with self._connect() as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT * FROM anomaly_log ORDER BY id DESC"
            ).fetchall()
        return [dict(r) for r in rows]

    def fetch_high_risk(self, above: float = 0.80) -> list[dict]:
        """Return alerts with score above a high-risk threshold."""
        with self._connect() as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT * FROM anomaly_log WHERE score >= ? ORDER BY id DESC",
                (above,),
            ).fetchall()
        return [dict(r) for r in rows]

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------

    def _log(self, alert: AnomalyAlert) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO anomaly_log
                    (timestamp, tool_name, score, gap_signal,
                     frequency_signal, sequence_signal, threshold)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    alert.timestamp.isoformat(),
                    alert.tool_name,
                    alert.score,
                    alert.gap_signal,
                    alert.frequency_signal,
                    alert.sequence_signal,
                    alert.threshold,
                ),
            )

    def _init_db(self) -> None:
        with self._connect() as conn:
            conn.execute(_DDL)

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(self._db_path)
