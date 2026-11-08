from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Dict

import yaml

POLICY_PATH = Path(__file__).parent / "policies.yaml"

# Loaded once at import time; call load_policy() to force a reload (e.g. tests).
_POLICIES: Dict[str, Any] = {}


def load_policy() -> Dict[str, Any]:
    """Read policies.yaml and cache the result.  Safe to call multiple times."""
    global _POLICIES
    with open(POLICY_PATH, "r", encoding="utf-8") as fh:
        _POLICIES = yaml.safe_load(fh) or {}
    return _POLICIES


def _policies() -> Dict[str, Any]:
    if not _POLICIES:
        load_policy()
    return _POLICIES


# ---------------------------------------------------------------------------
# Classification
# ---------------------------------------------------------------------------
# Precedence (highest → lowest):
#   1. always_hold list   → HOLD/MEDIUM/GENERIC_WRITE
#   2. always_allow list  → AUTO_APPROVE/LOW/READ
#   3. rules.mutations    → HOLD with declared risk/category (first match wins)
#   4. rules.auto_approve_prefixes → AUTO_APPROVE/LOW/READ
#   5. default_action     → HOLD/MEDIUM/GENERIC_WRITE
# ---------------------------------------------------------------------------

def classify_tool_call(tool_name: str) -> Dict[str, str]:
    """Return {"action", "risk_level", "category"} for a tool name."""
    p = _policies()
    rules = p.get("rules", {})

    # 1. Explicit hold list (overrides everything, including safe prefixes)
    if tool_name in p.get("always_hold", []):
        return {"action": "HOLD", "risk_level": "MEDIUM", "category": "GENERIC_WRITE"}

    # 2. Explicit allow list (overrides mutation patterns)
    if tool_name in p.get("always_allow", []):
        return {"action": "AUTO_APPROVE", "risk_level": "LOW", "category": "READ"}

    # 3. Mutation patterns (checked before safe prefixes — bypass prevention)
    for rule in rules.get("mutations", []):
        pattern = rule.get("pattern")
        if pattern and re.search(pattern, tool_name, re.IGNORECASE):
            return {
                "action": "HOLD",
                "risk_level": rule.get("risk_level", "MEDIUM"),
                "category": rule.get("category", "GENERIC_WRITE"),
            }

    # 4. Safe-read prefixes
    for prefix in rules.get("auto_approve_prefixes", []):
        if tool_name.startswith(prefix):
            return {"action": "AUTO_APPROVE", "risk_level": "LOW", "category": "READ"}

    # 5. Default
    return {"action": "HOLD", "risk_level": "MEDIUM", "category": "GENERIC_WRITE"}


load_policy()
