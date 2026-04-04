"""
Layer 3 — Consequence Model

Classifies every tool call by two dimensions:
  - Blast radius  : how wide is the potential damage? (LOW / MEDIUM / HIGH)
  - Reversibility : can the action be undone?         (REVERSIBLE / IRREVERSIBLE)

These two dimensions together determine the consequence level, which modulates
how the EscalationEngine responds to an anomaly.

A 0.65 anomaly score on read_file and a 0.65 anomaly score on deploy_code are
not the same event. The consequence model makes that distinction explicit.

Classification basis:
  OWASP LLM08 / Excessive Agency — tools with real-world impact, external
  side effects, or irreversible state changes carry the highest consequence.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class BlastRadius(str, Enum):
    LOW    = "low"     # local, bounded, no external effects
    MEDIUM = "medium"  # local mutation or limited external reach
    HIGH   = "high"    # broad, external, or system-wide impact


class Reversibility(str, Enum):
    REVERSIBLE   = "reversible"
    IRREVERSIBLE = "irreversible"


class ConsequenceLevel(str, Enum):
    LOW      = "low"      # LOW blast + reversible
    MEDIUM   = "medium"   # MEDIUM blast or irreversible local action
    HIGH     = "high"     # HIGH blast or irreversible + external impact
    CRITICAL = "critical" # HIGH blast + irreversible


@dataclass(frozen=True)
class Consequence:
    tool_name:    str
    blast_radius: BlastRadius
    reversibility: Reversibility
    level:        ConsequenceLevel
    rationale:    str


# ---------------------------------------------------------------------------
# Classification table
# Basis: OWASP LLM08 + Principle of Least Privilege
# ---------------------------------------------------------------------------

_CLASSIFICATIONS: dict[str, Consequence] = {
    # -- LOW consequence (read-only, local, reversible) --------------------
    "read_file": Consequence(
        tool_name="read_file", blast_radius=BlastRadius.LOW,
        reversibility=Reversibility.REVERSIBLE, level=ConsequenceLevel.LOW,
        rationale="Read-only. No mutation. Locally bounded.",
    ),
    "list_directory": Consequence(
        tool_name="list_directory", blast_radius=BlastRadius.LOW,
        reversibility=Reversibility.REVERSIBLE, level=ConsequenceLevel.LOW,
        rationale="Read-only directory listing. No side effects.",
    ),
    "get_current_time": Consequence(
        tool_name="get_current_time", blast_radius=BlastRadius.LOW,
        reversibility=Reversibility.REVERSIBLE, level=ConsequenceLevel.LOW,
        rationale="Pure read. No state change.",
    ),
    "calculate": Consequence(
        tool_name="calculate", blast_radius=BlastRadius.LOW,
        reversibility=Reversibility.REVERSIBLE, level=ConsequenceLevel.LOW,
        rationale="Pure computation. No I/O.",
    ),
    "web_search": Consequence(
        tool_name="web_search", blast_radius=BlastRadius.LOW,
        reversibility=Reversibility.REVERSIBLE, level=ConsequenceLevel.LOW,
        rationale="Read-only external query. No mutation.",
    ),
    "get_weather": Consequence(
        tool_name="get_weather", blast_radius=BlastRadius.LOW,
        reversibility=Reversibility.REVERSIBLE, level=ConsequenceLevel.LOW,
        rationale="Read-only external API. No mutation.",
    ),

    # -- MEDIUM consequence (local mutation, limited reach) ----------------
    "write_file": Consequence(
        tool_name="write_file", blast_radius=BlastRadius.MEDIUM,
        reversibility=Reversibility.REVERSIBLE, level=ConsequenceLevel.MEDIUM,
        rationale="Mutates local filesystem. Reversible via backup/restore.",
    ),
    "rename_file": Consequence(
        tool_name="rename_file", blast_radius=BlastRadius.MEDIUM,
        reversibility=Reversibility.REVERSIBLE, level=ConsequenceLevel.MEDIUM,
        rationale="Moves or renames local file. Reversible but can stage exfil.",
    ),
    "copy_file": Consequence(
        tool_name="copy_file", blast_radius=BlastRadius.MEDIUM,
        reversibility=Reversibility.REVERSIBLE, level=ConsequenceLevel.MEDIUM,
        rationale="Duplicates data. Source intact but copy can be exfiltrated.",
    ),
    "export_data": Consequence(
        tool_name="export_data", blast_radius=BlastRadius.MEDIUM,
        reversibility=Reversibility.REVERSIBLE, level=ConsequenceLevel.MEDIUM,
        rationale="Creates a copy of data in a new format. Staging risk.",
    ),
    "send_slack_message": Consequence(
        tool_name="send_slack_message", blast_radius=BlastRadius.MEDIUM,
        reversibility=Reversibility.IRREVERSIBLE, level=ConsequenceLevel.MEDIUM,
        rationale="External side effect. Message delivered cannot be recalled.",
    ),

    # -- HIGH consequence (irreversible, external, or wide blast radius) ---
    "delete_file": Consequence(
        tool_name="delete_file", blast_radius=BlastRadius.MEDIUM,
        reversibility=Reversibility.IRREVERSIBLE, level=ConsequenceLevel.HIGH,
        rationale="Irreversible data destruction. No undo without backup.",
    ),
    "send_email": Consequence(
        tool_name="send_email", blast_radius=BlastRadius.HIGH,
        reversibility=Reversibility.IRREVERSIBLE, level=ConsequenceLevel.HIGH,
        rationale="External side effect. Cannot be recalled once delivered.",
    ),
    "make_http_request": Consequence(
        tool_name="make_http_request", blast_radius=BlastRadius.HIGH,
        reversibility=Reversibility.IRREVERSIBLE, level=ConsequenceLevel.HIGH,
        rationale="Unconstrained external network call. Data may leave system.",
    ),
    "write_to_database": Consequence(
        tool_name="write_to_database", blast_radius=BlastRadius.HIGH,
        reversibility=Reversibility.IRREVERSIBLE, level=ConsequenceLevel.HIGH,
        rationale="Mutates persistent shared state. Rollback requires migration.",
    ),

    # -- CRITICAL consequence (HIGH blast + irreversible) ------------------
    "execute_shell": Consequence(
        tool_name="execute_shell", blast_radius=BlastRadius.HIGH,
        reversibility=Reversibility.IRREVERSIBLE, level=ConsequenceLevel.CRITICAL,
        rationale="Arbitrary code execution. Maximum blast radius. No bounds.",
    ),
    "deploy_code": Consequence(
        tool_name="deploy_code", blast_radius=BlastRadius.HIGH,
        reversibility=Reversibility.IRREVERSIBLE, level=ConsequenceLevel.CRITICAL,
        rationale="Irreversible infrastructure change. System-wide blast radius.",
    ),
    "create_api_key": Consequence(
        tool_name="create_api_key", blast_radius=BlastRadius.HIGH,
        reversibility=Reversibility.IRREVERSIBLE, level=ConsequenceLevel.CRITICAL,
        rationale="Creates persistent credential. Privilege escalation vector.",
    ),
    "delete_api_key": Consequence(
        tool_name="delete_api_key", blast_radius=BlastRadius.HIGH,
        reversibility=Reversibility.IRREVERSIBLE, level=ConsequenceLevel.CRITICAL,
        rationale="Destroys credential. Potential denial of service.",
    ),
}

_DEFAULT_UNKNOWN = Consequence(
    tool_name="unknown", blast_radius=BlastRadius.MEDIUM,
    reversibility=Reversibility.IRREVERSIBLE, level=ConsequenceLevel.HIGH,
    rationale="Tool not in consequence table. Defaulting to HIGH — unknown impact.",
)


class ConsequenceModel:
    """
    Looks up the consequence classification for any tool call.
    Tools not in the classification table default to HIGH consequence —
    unknown impact is treated as potentially severe.
    """

    def classify(self, tool_name: str) -> Consequence:
        return _CLASSIFICATIONS.get(tool_name, _DEFAULT_UNKNOWN)

    def level(self, tool_name: str) -> ConsequenceLevel:
        return self.classify(tool_name).level
