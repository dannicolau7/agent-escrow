from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import yaml

POLICY_PATH = Path(__file__).parent / "policies.yaml"

# Loaded once at import time; call load_policy() to force a reload (e.g. tests).
_POLICIES: dict[str, Any] = {}

# Words that chain a second operation onto a read ("get_and_delete_user").
# A tool name containing one of these is never auto-approved.
_DEFAULT_CHAIN_WORDS = ("and", "then", "or", "also", "plus")


def load_policy() -> dict[str, Any]:
    """Read policies.yaml and cache the result.  Safe to call multiple times."""
    global _POLICIES
    with open(POLICY_PATH, encoding="utf-8") as fh:
        _POLICIES = yaml.safe_load(fh) or {}
    return _POLICIES


def _policies() -> dict[str, Any]:
    if not _POLICIES:
        load_policy()
    return _POLICIES


def tokenize(tool_name: str) -> list[str]:
    """Split a tool name into lowercase words.

    Handles snake_case, kebab-case, dotted.names and camelCase:
    "getAndDeleteUser" → ["get", "and", "delete", "user"].
    """
    spaced = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", str(tool_name))
    spaced = re.sub(r"([A-Z]+)([A-Z][a-z])", r"\1 \2", spaced)
    return [w for w in re.split(r"[^A-Za-z0-9]+", spaced.lower()) if w]


def _read_verbs(rules: dict[str, Any]) -> list[str]:
    verbs = list(rules.get("auto_approve_verbs", []))
    # Back-compat: "get_" style prefixes → "get"
    for prefix in rules.get("auto_approve_prefixes", []):
        verbs.append(str(prefix).rstrip("_-.").lower())
    return [v for v in verbs if v]


# ---------------------------------------------------------------------------
# Classification
# ---------------------------------------------------------------------------
# Precedence (highest → lowest):
#   1. always_hold list   → HOLD/MEDIUM/GENERIC_WRITE
#   2. always_allow list  → AUTO_APPROVE/LOW/READ
#   3. Read verb as the FIRST word and no chaining word ("and", "then", …)
#      → AUTO_APPROVE/LOW/READ, unless an `override_read` rule matches.
#   4. rules.mutations    → HOLD with declared risk/category (first match wins).
#      Each rule matches whole words of the tool name (`words:`) and/or a
#      regex (`pattern:`).  Whole words avoid false positives such as
#      "get_credit_score" matching "edit".
#   5. Default            → HOLD/MEDIUM/GENERIC_WRITE (fail-safe)
# ---------------------------------------------------------------------------


def classify_tool_call(tool_name: str) -> dict[str, str]:
    """Return {"action", "risk_level", "category"} for a tool name."""
    p = _policies()
    rules = p.get("rules", {})
    words = tokenize(tool_name)

    # 1. Explicit hold list (overrides everything)
    if tool_name in p.get("always_hold", []):
        return {"action": "HOLD", "risk_level": "MEDIUM", "category": "GENERIC_WRITE"}

    # 2. Explicit allow list
    if tool_name in p.get("always_allow", []):
        return {"action": "AUTO_APPROVE", "risk_level": "LOW", "category": "READ"}

    word_set = set(words)
    mutations = rules.get("mutations", [])

    def _matches(rule: dict[str, Any]) -> bool:
        rule_words = {str(w).lower() for w in rule.get("words", [])}
        pattern = rule.get("pattern")
        return bool(
            (rule_words and rule_words & word_set) or (pattern and re.search(pattern, tool_name, re.IGNORECASE))
        )

    def _hold(rule: dict[str, Any]) -> dict[str, str]:
        return {
            "action": "HOLD",
            "risk_level": rule.get("risk_level", "MEDIUM"),
            "category": rule.get("category", "GENERIC_WRITE"),
        }

    # 3. Read verb first, with nothing chained after it.  Only rules marked
    #    `override_read: true` (destructive words) can still hold it, so
    #    "get_user_delete" is held but "read_email" / "get_post" are not.
    chain_words = set(rules.get("chain_words", _DEFAULT_CHAIN_WORDS))
    if words and words[0] in _read_verbs(rules) and not (chain_words & word_set):
        for rule in mutations:
            if rule.get("override_read") and _matches(rule):
                return _hold(rule)
        return {"action": "AUTO_APPROVE", "risk_level": "LOW", "category": "READ"}

    # 4. Mutation rules (first match wins)
    for rule in mutations:
        if _matches(rule):
            return _hold(rule)

    # 5. Default
    return {"action": "HOLD", "risk_level": "MEDIUM", "category": "GENERIC_WRITE"}


load_policy()
