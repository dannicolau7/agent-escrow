from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Dict

import yaml

POLICY_PATH = Path(__file__).parent / "policies.yaml"


def load_policies() -> Dict[str, Any]:
    with open(POLICY_PATH, "r", encoding="utf-8") as fh:
        return yaml.safe_load(fh) or {}


POLICIES = load_policies()

DEFAULT_AUTO_APPROVE_PREFIXES = ("get_", "list_", "search_", "read_", "describe_")

# Ordered by severity: first match wins.  Mutations are evaluated BEFORE safe
# prefixes so that a name like "get_and_delete_user" is not auto-approved.
DEFAULT_MUTATION_PATTERNS = (
    (".*delete.*|.*drop.*|.*destroy.*|.*truncate.*|.*purge.*", "CRITICAL", "DATABASE"),
    (".*refund.*|.*charge.*|.*transfer.*|.*pay.*", "HIGH", "FINANCIAL"),
    (".*send.*", "MEDIUM", "COMMUNICATION"),
    (".*update.*|.*write.*|.*push.*|.*create.*|.*insert.*|.*modify.*|.*edit.*|.*upload.*", "MEDIUM", "GENERIC_WRITE"),
)


def classify_tool_call(tool_name: str):
    """Return a risk classification dictionary for a tool name."""
    policies = load_policies() if not POLICIES else POLICIES
    rules = policies.get("rules", {})

    # 1. Check mutation patterns FIRST — a "get_and_delete_user" must be HOLD,
    #    not AUTO_APPROVE just because it starts with a safe prefix.
    mutation_rules = rules.get("mutations", [])
    if not mutation_rules:
        mutation_rules = [
            {"pattern": pattern, "risk_level": risk_level, "category": category}
            for pattern, risk_level, category in DEFAULT_MUTATION_PATTERNS
        ]

    for rule in mutation_rules:
        pattern = rule.get("pattern")
        if pattern and re.search(pattern, tool_name, re.IGNORECASE):
            return {
                "action": "HOLD",
                "risk_level": rule.get("risk_level", "MEDIUM"),
                "category": rule.get("category", "GENERIC_WRITE"),
            }

    # 2. Only then check safe prefixes.
    auto_approve_prefixes = rules.get("auto_approve_prefixes", list(DEFAULT_AUTO_APPROVE_PREFIXES))
    for prefix in auto_approve_prefixes:
        if str(tool_name).startswith(prefix):
            return {"action": "AUTO_APPROVE", "risk_level": "LOW", "category": "READ"}

    legacy_mutating = policies.get("mutating_tools", [])
    if tool_name in legacy_mutating:
        return {"action": "HOLD", "risk_level": "MEDIUM", "category": "GENERIC_WRITE"}

    legacy_safe = policies.get("safe_tools", [])
    if tool_name in legacy_safe:
        return {"action": "AUTO_APPROVE", "risk_level": "LOW", "category": "READ"}

    return {"action": "HOLD", "risk_level": "MEDIUM", "category": "GENERIC_WRITE"}
