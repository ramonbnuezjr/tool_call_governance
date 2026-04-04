from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Literal


class Outcome(str, Enum):
    ALLOWED = "allowed"
    DENIED = "denied"


class RuleList(str, Enum):
    WHITELIST = "whitelist"
    BLACKLIST = "blacklist"


@dataclass
class ToolCall:
    """Anthropic tool use format: {name: str, input: dict}."""
    name: str
    input: dict = field(default_factory=dict)

    def input_hash(self) -> str:
        serialized = json.dumps(self.input, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(serialized.encode()).hexdigest()[:16]


@dataclass
class Rule:
    """A single entry in the policy whitelist or blacklist."""
    name: str
    tool: str
    list: RuleList


@dataclass
class Decision:
    """The result of evaluating a ToolCall against policy."""
    tool_name: str
    input_hash: str
    outcome: Outcome
    rule_triggered: str
    timestamp: datetime = field(default_factory=datetime.utcnow)
