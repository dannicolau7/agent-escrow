from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional, Set, Tuple

import yaml

POLICIES_PATH: Path = Path(__file__).parent / "policies.yaml"


@dataclass
class Policy:
    mutating_tools: Set[str] = field(default_factory=set)
    safe_tools: Set[str] = field(default_factory=set)
    default_action: str = "hold"  # "hold" | "allow"


_policy: Optional[Policy] = None


def load_policy(path: Path = POLICIES_PATH) -> Policy:
    global _policy
    with open(path) as fh:
        data = yaml.safe_load(fh) or {}
    _policy = Policy(
        mutating_tools=set(data.get("mutating_tools", [])),
        safe_tools=set(data.get("safe_tools", [])),
        default_action=data.get("default_action", "hold"),
    )
    return _policy


def get_policy() -> Policy:
    global _policy
    if _policy is None:
        load_policy()
    return _policy


def classify(tool_name: str) -> Tuple[bool, Optional[str]]:
    """Return (is_mutating, reason_string)."""
    policy = get_policy()
    if tool_name in policy.mutating_tools:
        return True, f"'{tool_name}' is listed as mutating"
    if tool_name in policy.safe_tools:
        return False, None
    if policy.default_action == "hold":
        return True, f"'{tool_name}' is unlisted; default policy is hold"
    return False, None
