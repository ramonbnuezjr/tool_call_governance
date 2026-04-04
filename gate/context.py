"""
Layer 3 — Session Context

Maintains a sliding window of the last N tool calls within a session and
detects known cascade patterns — multi-step sequences that individually
pass Layer 1 and Layer 2 but compose into a harmful action.

This directly addresses Layer 2 Gap 2 (no session memory) and Gap 3
(sequence signal too shallow — looked back only one call).

Known cascade patterns:
  EXFIL_STAGING   — read → rename/copy/export → web_search/http
                    Data read, moved to accessible location, then transmitted.
  BOUNDARY_PROBE  — 3+ denied calls within the window
                    Systematic probing of the policy boundary.
  CRED_ESCALATION — any call followed by create_api_key or delete_api_key
                    Credential manipulation after any activity is suspicious.
  GAP_FLOOD       — 3+ no_matching_rule hits within the window
                    Rapid-fire use of unanticipated tools.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path


@dataclass
class CascadeSignal:
    detected:     bool
    pattern_name: str
    description:  str
    window:       list[str]   # tool names in the window that matched


_EXFIL_READ    = {"read_file", "list_directory"}
_EXFIL_STAGE   = {"rename_file", "copy_file", "export_data", "write_file"}
_EXFIL_TRANSMIT = {"web_search", "make_http_request", "send_email", "send_slack_message"}
_CRED_TOOLS    = {"create_api_key", "delete_api_key"}
_DENIED_RULE   = "denied"


class SessionContext:
    """
    Tracks recent tool call history and detects cascade attack patterns.

    Window size defaults to 5 — enough to catch multi-step chains without
    accumulating so much history that the signal becomes noisy.
    """

    def __init__(self, db_path: str | Path, window_size: int = 5) -> None:
        self._db_path    = Path(db_path)
        self._window_size = window_size

    # ------------------------------------------------------------------
    # Pattern detection
    # ------------------------------------------------------------------

    def detect(self, current_tool: str) -> CascadeSignal:
        """
        Evaluate the current tool call against recent history.
        Returns the first pattern matched, or a clean signal if none.
        """
        window = self._fetch_window(current_tool)
        names  = [r["tool_name"] for r in window]
        rules  = [r["rule_triggered"] for r in window]

        # Check patterns in priority order
        for detector in (
            self._detect_exfil_staging,
            self._detect_boundary_probe,
            self._detect_cred_escalation,
            self._detect_gap_flood,
        ):
            signal = detector(names, rules)
            if signal.detected:
                return signal

        return CascadeSignal(
            detected=False,
            pattern_name="none",
            description="No cascade pattern detected.",
            window=names,
        )

    # ------------------------------------------------------------------
    # Pattern detectors
    # ------------------------------------------------------------------

    def _detect_exfil_staging(
        self, names: list[str], rules: list[str]
    ) -> CascadeSignal:
        """read → stage → transmit within the window."""
        has_read     = any(n in _EXFIL_READ     for n in names)
        has_stage    = any(n in _EXFIL_STAGE    for n in names)
        has_transmit = any(n in _EXFIL_TRANSMIT for n in names)

        if has_read and has_stage and has_transmit:
            return CascadeSignal(
                detected=True,
                pattern_name="EXFIL_STAGING",
                description=(
                    "Data exfiltration pattern: read → stage → transmit "
                    "detected within session window."
                ),
                window=names,
            )
        return CascadeSignal(detected=False, pattern_name="none",
                             description="", window=names)

    def _detect_boundary_probe(
        self, names: list[str], rules: list[str]
    ) -> CascadeSignal:
        """3 or more denied calls within the window."""
        denied_count = sum(1 for r in rules if r != "no_matching_rule"
                          and "denied" in r.lower() or r == _DENIED_RULE)

        # Count explicit blacklist hits only — exclude no_matching_rule denials
        # so this pattern stays distinct from GAP_FLOOD
        with sqlite3.connect(self._db_path) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                """
                SELECT outcome, rule_triggered FROM audit_log
                ORDER BY id DESC LIMIT ?
                """,
                (self._window_size,),
            ).fetchall()
        denied_count = sum(
            1 for r in rows
            if r["outcome"] == "denied"
            and r["rule_triggered"] != "no_matching_rule"
        )

        if denied_count >= 3:
            return CascadeSignal(
                detected=True,
                pattern_name="BOUNDARY_PROBE",
                description=(
                    f"{denied_count} denied calls in the last "
                    f"{self._window_size}-call window. Systematic "
                    "policy probing detected."
                ),
                window=names,
            )
        return CascadeSignal(detected=False, pattern_name="none",
                             description="", window=names)

    def _detect_cred_escalation(
        self, names: list[str], rules: list[str]
    ) -> CascadeSignal:
        """Any credential manipulation in the window."""
        cred_hits = [n for n in names if n in _CRED_TOOLS]
        if cred_hits:
            return CascadeSignal(
                detected=True,
                pattern_name="CRED_ESCALATION",
                description=(
                    f"Credential manipulation detected: {cred_hits}. "
                    "Privilege escalation risk (OWASP LLM08)."
                ),
                window=names,
            )
        return CascadeSignal(detected=False, pattern_name="none",
                             description="", window=names)

    def _detect_gap_flood(
        self, names: list[str], rules: list[str]
    ) -> CascadeSignal:
        """3 or more no_matching_rule hits in the window."""
        with sqlite3.connect(self._db_path) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                """
                SELECT rule_triggered FROM audit_log
                ORDER BY id DESC LIMIT ?
                """,
                (self._window_size,),
            ).fetchall()
        gap_count = sum(1 for r in rows
                        if r["rule_triggered"] == "no_matching_rule")

        if gap_count >= 3:
            return CascadeSignal(
                detected=True,
                pattern_name="GAP_FLOOD",
                description=(
                    f"{gap_count} unanticipated tool calls in the last "
                    f"{self._window_size}-call window. Systematic gap "
                    "zone probing detected."
                ),
                window=names,
            )
        return CascadeSignal(detected=False, pattern_name="none",
                             description="", window=names)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _fetch_window(self, current_tool: str) -> list[dict]:
        """Fetch the last N rows from the audit log including current tool."""
        with sqlite3.connect(self._db_path) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                """
                SELECT tool_name, outcome, rule_triggered
                FROM audit_log ORDER BY id DESC LIMIT ?
                """,
                (self._window_size,),
            ).fetchall()
        window = [dict(r) for r in rows]
        # Prepend current tool (not yet logged)
        window.insert(0, {
            "tool_name":     current_tool,
            "outcome":       "pending",
            "rule_triggered": "pending",
        })
        return window[:self._window_size]
