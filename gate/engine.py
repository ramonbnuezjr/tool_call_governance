from __future__ import annotations

from pathlib import Path

import yaml

from gate.models import Decision, Outcome, Rule, RuleList, ToolCall

_NO_MATCHING_RULE = "no_matching_rule"


class GovernanceGate:
    """
    Middleware interceptor that evaluates every tool call against a YAML policy
    before execution is permitted.

    Evaluation order:
      1. Whitelist — if the tool matches, return ALLOWED.
      2. Blacklist — if the tool matches, return DENIED.
      3. Default   — no rule matched, return DENIED with 'no_matching_rule'.
    """

    def __init__(self, policy_path: str | Path) -> None:
        self._rules: list[Rule] = []
        self._load_policy(Path(policy_path))

    # ------------------------------------------------------------------
    # Policy loading
    # ------------------------------------------------------------------

    def _load_policy(self, path: Path) -> None:
        with path.open() as fh:
            raw = yaml.safe_load(fh) or {}

        for name in raw.get("whitelist", []):
            self._rules.append(Rule(name=name, tool=name, list=RuleList.WHITELIST))

        for name in raw.get("blacklist", []):
            self._rules.append(Rule(name=name, tool=name, list=RuleList.BLACKLIST))

    # ------------------------------------------------------------------
    # Evaluation
    # ------------------------------------------------------------------

    def evaluate(self, tool_call: ToolCall) -> Decision:
        """Return a Decision for the given ToolCall without executing it."""
        whitelist_match = self._find(tool_call.name, RuleList.WHITELIST)
        if whitelist_match:
            return Decision(
                tool_name=tool_call.name,
                input_hash=tool_call.input_hash(),
                outcome=Outcome.ALLOWED,
                rule_triggered=whitelist_match.name,
            )

        blacklist_match = self._find(tool_call.name, RuleList.BLACKLIST)
        if blacklist_match:
            return Decision(
                tool_name=tool_call.name,
                input_hash=tool_call.input_hash(),
                outcome=Outcome.DENIED,
                rule_triggered=blacklist_match.name,
            )

        return Decision(
            tool_name=tool_call.name,
            input_hash=tool_call.input_hash(),
            outcome=Outcome.DENIED,
            rule_triggered=_NO_MATCHING_RULE,
        )

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _find(self, tool_name: str, list_type: RuleList) -> Rule | None:
        for rule in self._rules:
            if rule.list is list_type and rule.tool == tool_name:
                return rule
        return None
