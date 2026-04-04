"""
Layer 2 — Baseline Model

Builds a frequency distribution of tool calls from the Layer 1 audit log.
Answers two questions:
  1. Have we seen this tool before, and how often?
  2. Is the current call rate for this tool above its historical norm?

This is the reference model that the risk scorer measures deviation against.
"""

from __future__ import annotations

import sqlite3
from collections import Counter
from datetime import datetime, timedelta
from pathlib import Path


class BaselineModel:
    """
    Derives normal behavior from the Layer 1 audit log.

    Tracks:
      - Overall call counts per tool (lifetime frequency)
      - Recent call rate per tool (calls within a sliding time window)
      - Gap zone tools (tools that have ever hit no_matching_rule)
    """

    NO_MATCHING_RULE = "no_matching_rule"

    def __init__(self, db_path: str | Path, window_minutes: int = 60) -> None:
        self._db_path = Path(db_path)
        self._window_minutes = window_minutes

        # Populated by build()
        self._lifetime_counts: Counter[str] = Counter()
        self._gap_tools: set[str] = set()
        self._built = False

    # ------------------------------------------------------------------
    # Build
    # ------------------------------------------------------------------

    def build(self) -> None:
        """Load the full audit log and compute the baseline distribution."""
        with self._connect() as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT tool_name, rule_triggered FROM audit_log"
            ).fetchall()

        self._lifetime_counts.clear()
        self._gap_tools.clear()

        for row in rows:
            self._lifetime_counts[row["tool_name"]] += 1
            if row["rule_triggered"] == self.NO_MATCHING_RULE:
                self._gap_tools.add(row["tool_name"])

        self._built = True

    # ------------------------------------------------------------------
    # Queries
    # ------------------------------------------------------------------

    def lifetime_count(self, tool_name: str) -> int:
        """Total number of times this tool has been called historically."""
        self._require_built()
        return self._lifetime_counts.get(tool_name, 0)

    def is_known(self, tool_name: str) -> bool:
        """True if this tool has ever appeared in the audit log."""
        self._require_built()
        return tool_name in self._lifetime_counts

    def is_gap_tool(self, tool_name: str) -> bool:
        """True if this tool has ever hit no_matching_rule."""
        self._require_built()
        return tool_name in self._gap_tools

    def recent_rate(self, tool_name: str) -> int:
        """Number of times this tool was called within the sliding window."""
        with self._connect() as conn:
            cutoff = (
                datetime.utcnow() - timedelta(minutes=self._window_minutes)
            ).isoformat()
            row = conn.execute(
                """
                SELECT COUNT(*) as cnt FROM audit_log
                WHERE tool_name = ? AND timestamp >= ?
                """,
                (tool_name, cutoff),
            ).fetchone()
        return row[0] if row else 0

    def baseline_rate(self, tool_name: str) -> float:
        """
        Expected calls per window based on lifetime history.

        Computed as: (lifetime_count / total_lifetime_calls) * window_minutes
        Returns 0.0 if the tool has never been seen.
        """
        self._require_built()
        total = sum(self._lifetime_counts.values())
        if total == 0:
            return 0.0
        share = self._lifetime_counts.get(tool_name, 0) / total
        return share * self._window_minutes

    def top_tools(self, n: int = 10) -> list[tuple[str, int]]:
        """Return the n most frequently called tools by lifetime count."""
        self._require_built()
        return self._lifetime_counts.most_common(n)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(self._db_path)

    def _require_built(self) -> None:
        if not self._built:
            raise RuntimeError(
                "BaselineModel.build() must be called before querying."
            )
