"""
GovernanceGate CLI

Usage:
  python cli/gate.py audit               # all decisions, newest first
  python cli/gate.py audit --denied      # denied decisions only
  python cli/gate.py audit --gaps        # no_matching_rule hits only
  python cli/gate.py check --tool NAME   # evaluate a single tool call against policy
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

# Allow running from the project root without installing the package.
sys.path.insert(0, str(Path(__file__).parents[1]))

from gate import AuditLogger, GovernanceGate, ToolCall

# ---------------------------------------------------------------------------
# Defaults
# ---------------------------------------------------------------------------

DEFAULT_POLICY = Path(__file__).parents[1] / "policy" / "rules.yaml"
DEFAULT_DB     = Path(__file__).parents[1] / "audit.db"

# ---------------------------------------------------------------------------
# Formatting
# ---------------------------------------------------------------------------

_COL_WIDTH = {
    "id":             4,
    "timestamp":     26,
    "tool_name":     22,
    "input_hash":    18,
    "outcome":        8,
    "rule_triggered": 22,
}

_OUTCOME_LABEL = {
    "allowed": "ALLOWED",
    "denied":  "DENIED ",
}


def _header() -> str:
    return "  ".join(k.upper().ljust(v) for k, v in _COL_WIDTH.items())


def _divider() -> str:
    return "  ".join("-" * v for k, v in _COL_WIDTH.items())


def _row(r: dict) -> str:
    outcome_display = _OUTCOME_LABEL.get(r["outcome"], r["outcome"])
    return "  ".join([
        str(r["id"]).ljust(_COL_WIDTH["id"]),
        r["timestamp"].ljust(_COL_WIDTH["timestamp"]),
        r["tool_name"].ljust(_COL_WIDTH["tool_name"]),
        r["input_hash"].ljust(_COL_WIDTH["input_hash"]),
        outcome_display.ljust(_COL_WIDTH["outcome"]),
        r["rule_triggered"].ljust(_COL_WIDTH["rule_triggered"]),
    ])


def _print_table(rows: list[dict], label: str) -> None:
    print(f"\n{label} ({len(rows)} row{'s' if len(rows) != 1 else ''})\n")
    print(_header())
    print(_divider())
    if not rows:
        print("  (no records)")
    else:
        for r in rows:
            print(_row(r))
    print()


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------

def cmd_audit(args: argparse.Namespace) -> None:
    logger = AuditLogger(db_path=args.db)

    if args.gaps:
        rows = logger.fetch_gaps()
        label = "Gap zone — no_matching_rule hits"
    elif args.denied:
        rows = logger.fetch_denied()
        label = "Denied decisions"
    else:
        rows = logger.fetch_all()
        label = "Full audit log"

    _print_table(rows, label)


def cmd_check(args: argparse.Namespace) -> None:
    gate   = GovernanceGate(policy_path=args.policy)
    logger = AuditLogger(db_path=args.db)

    tool_input: dict = {}
    if args.input:
        try:
            tool_input = json.loads(args.input)
        except json.JSONDecodeError as exc:
            print(f"Error: --input must be valid JSON — {exc}", file=sys.stderr)
            sys.exit(1)

    tool_call = ToolCall(name=args.tool, input=tool_input)
    decision  = gate.evaluate(tool_call)
    logger.log(decision)

    outcome_label = _OUTCOME_LABEL.get(decision.outcome.value, decision.outcome.value)
    print(f"\n  tool:          {decision.tool_name}")
    print(f"  outcome:       {outcome_label}")
    print(f"  rule:          {decision.rule_triggered}")
    print(f"  input_hash:    {decision.input_hash}")
    print(f"  timestamp:     {decision.timestamp.isoformat()}")
    print()


# ---------------------------------------------------------------------------
# Argument parser
# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="gate",
        description="GovernanceGate — Layer 1 policy enforcement CLI",
    )
    parser.add_argument(
        "--db",
        type=Path,
        default=DEFAULT_DB,
        metavar="PATH",
        help=f"SQLite audit log path (default: {DEFAULT_DB})",
    )
    parser.add_argument(
        "--policy",
        type=Path,
        default=DEFAULT_POLICY,
        metavar="PATH",
        help=f"Policy YAML path (default: {DEFAULT_POLICY})",
    )

    sub = parser.add_subparsers(dest="command", required=True)

    # audit
    audit_p = sub.add_parser("audit", help="Inspect the audit log")
    audit_group = audit_p.add_mutually_exclusive_group()
    audit_group.add_argument(
        "--denied", action="store_true",
        help="Show denied decisions only",
    )
    audit_group.add_argument(
        "--gaps", action="store_true",
        help="Show no_matching_rule hits only (the Layer 2 signal)",
    )

    # check
    check_p = sub.add_parser("check", help="Evaluate a single tool call against policy")
    check_p.add_argument(
        "--tool", required=True, metavar="NAME",
        help="Tool name to evaluate",
    )
    check_p.add_argument(
        "--input", default=None, metavar="JSON",
        help='Tool input as a JSON string, e.g. \'{"path": "/tmp/a.txt"}\'',
    )

    return parser


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------

def main() -> None:
    parser = build_parser()
    args   = parser.parse_args()

    if args.command == "audit":
        cmd_audit(args)
    elif args.command == "check":
        cmd_check(args)


if __name__ == "__main__":
    main()
