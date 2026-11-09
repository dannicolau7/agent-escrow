"""Argument rules: decisions based on what a tool call *does*, not just its name.

`run_sql("SELECT 1")` and `run_sql("DROP TABLE users")` have the same tool
name.  Argument rules in ``policies.yaml`` look inside ``arguments``:

    argument_rules:
      - name: sql-destructive
        action: hold                 # hold | allow
        risk_level: CRITICAL
        category: DATABASE
        tools: [run_sql]             # exact names, and/or
        tool_pattern: "sql|query"    # a regex searched in the tool name
        when:                        # every condition must match
          query: {regex: "\\b(drop|delete)\\b"}

Condition keys are argument paths (``query``, ``options.force``,
``items.0.amount``) or ``"*"`` (any value anywhere in the arguments; hold
rules only).  Operators: ``regex``, ``not_regex``, ``equals``, ``in``,
``gt``, ``gte``, ``lt``, ``lte``, ``exists``.

Fail-closed semantics
---------------------
* A **hold** rule only ever makes a call *more* restricted.  If the argument
  is present but can't be checked (wrong type, NaN, too long), the condition
  counts as matched, so an agent can't dodge a hold by sending
  ``"amount": "75"`` or ``"amount": [75]``.
* An **allow** rule matches only when every argument is present and has the
  expected type.  Anything odd → no match → normal (usually holding) rules.
* A missing argument never matches either kind, except via ``exists: false``.
* String comparisons ignore case and surrounding whitespace.
* Policy mistakes (unknown keys, bad regex, allow rule without a tool
  selector) raise ``PolicyError`` at load time, so the proxy refuses to start
  rather than silently ignoring a rule.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from typing import Any

MAX_VALUE_CHARS = 100_000  # longer strings are "uncheckable"
RISK_ORDER = {"LOW": 0, "MEDIUM": 1, "HIGH": 2, "CRITICAL": 3}

_OPS = {"regex", "not_regex", "equals", "in", "gt", "gte", "lt", "lte", "exists"}
_RULE_KEYS = {"name", "action", "risk_level", "category", "tools", "tool_pattern", "when"}
_MISSING = object()


class PolicyError(ValueError):
    """policies.yaml contains an invalid argument rule."""


class _Unchecked(Exception):
    """The value is present but can't be compared (wrong type, too long, …)."""


@dataclass
class Condition:
    path: str
    ops: dict[str, Any]
    regexes: dict[str, re.Pattern[str]] = field(default_factory=dict)


@dataclass
class ArgRule:
    name: str
    action: str  # "hold" | "allow"
    risk_level: str
    category: str
    tools: frozenset[str]
    tool_pattern: re.Pattern[str] | None
    conditions: list[Condition]

    def applies_to(self, tool_name: str) -> bool:
        if not self.tools and self.tool_pattern is None:
            return True  # hold rules may apply to every tool
        return tool_name in self.tools or bool(self.tool_pattern and self.tool_pattern.search(tool_name))


# ---------------------------------------------------------------------------
# Loading / validation
# ---------------------------------------------------------------------------


def _compile(pattern: Any, where: str) -> re.Pattern[str]:
    if not isinstance(pattern, str):
        raise PolicyError(f"{where}: regex must be a string")
    try:
        return re.compile(pattern, re.IGNORECASE)
    except re.error as exc:
        raise PolicyError(f"{where}: invalid regex {pattern!r}: {exc}") from exc


def compile_rules(raw: Any) -> list[ArgRule]:
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise PolicyError("argument_rules must be a list")
    rules: list[ArgRule] = []
    for i, item in enumerate(raw):
        where = f"argument_rules[{i}]"
        if not isinstance(item, dict):
            raise PolicyError(f"{where}: must be a mapping")
        unknown = set(item) - _RULE_KEYS
        if unknown:
            raise PolicyError(f"{where}: unknown key(s) {sorted(unknown)}")
        name = str(item.get("name") or f"rule-{i}")
        where = f"argument_rules[{i}] ({name})"

        action = str(item.get("action", "")).lower()
        if action not in ("hold", "allow"):
            raise PolicyError(f"{where}: action must be 'hold' or 'allow'")
        risk = str(item.get("risk_level", "MEDIUM" if action == "hold" else "LOW")).upper()
        if risk not in RISK_ORDER:
            raise PolicyError(f"{where}: risk_level must be one of {sorted(RISK_ORDER)}")
        category = str(item.get("category", "GENERIC_WRITE" if action == "hold" else "READ"))

        tools = item.get("tools") or []
        if not isinstance(tools, list) or not all(isinstance(t, str) for t in tools):
            raise PolicyError(f"{where}: tools must be a list of tool names")
        tool_pattern = _compile(item["tool_pattern"], where) if item.get("tool_pattern") is not None else None
        if action == "allow" and not tools and tool_pattern is None:
            raise PolicyError(f"{where}: an allow rule must name its tools (tools or tool_pattern)")

        when = item.get("when")
        if not isinstance(when, dict) or not when:
            raise PolicyError(f"{where}: 'when' must be a non-empty mapping of argument -> condition")
        conditions: list[Condition] = []
        for path, ops in when.items():
            cwhere = f"{where}.when.{path}"
            if not isinstance(ops, dict) or not ops:
                raise PolicyError(f"{cwhere}: condition must be a mapping like {{regex: ...}}")
            bad = set(ops) - _OPS
            if bad:
                raise PolicyError(f"{cwhere}: unknown operator(s) {sorted(bad)}; use {sorted(_OPS)}")
            if path == "*":
                if action != "hold":
                    raise PolicyError(f"{cwhere}: '*' (any argument) is only allowed in hold rules")
                if set(ops) != {"regex"}:
                    raise PolicyError(f"{cwhere}: '*' supports only the regex operator")
            for op in ("gt", "gte", "lt", "lte"):
                if op in ops and not _is_number(ops[op]):
                    raise PolicyError(f"{cwhere}: {op} needs a number")
            if "in" in ops and not isinstance(ops["in"], list):
                raise PolicyError(f"{cwhere}: in needs a list")
            if "exists" in ops and not isinstance(ops["exists"], bool):
                raise PolicyError(f"{cwhere}: exists needs true or false")
            regexes = {op: _compile(ops[op], f"{cwhere}.{op}") for op in ("regex", "not_regex") if op in ops}
            conditions.append(Condition(str(path), dict(ops), regexes))

        rules.append(ArgRule(name, action, risk, category, frozenset(tools), tool_pattern, conditions))
    return rules


# ---------------------------------------------------------------------------
# Evaluation
# ---------------------------------------------------------------------------


def _is_number(v: Any) -> bool:
    return isinstance(v, int | float) and not isinstance(v, bool)


def _lookup(args: Any, path: str) -> Any:
    cur = args
    for part in path.split("."):
        if isinstance(cur, dict) and part in cur:
            cur = cur[part]
        elif isinstance(cur, list) and part.isdigit() and int(part) < len(cur):
            cur = cur[int(part)]
        else:
            return _MISSING
    return cur


def _as_text(v: Any) -> str:
    if isinstance(v, str):
        if len(v) > MAX_VALUE_CHARS:
            raise _Unchecked
        return v
    if isinstance(v, bool):
        return "true" if v else "false"
    if _is_number(v):
        return str(v)
    raise _Unchecked


def _as_number(v: Any) -> float:
    if _is_number(v):
        n = float(v)
    elif isinstance(v, str) and len(v) <= 64:
        try:
            n = float(v.strip())
        except ValueError:
            raise _Unchecked from None
    else:
        raise _Unchecked
    if not math.isfinite(n):
        raise _Unchecked
    return n


def _norm(v: Any) -> Any:
    return v.strip().casefold() if isinstance(v, str) else v


def _same(a: Any, b: Any) -> bool:
    if _is_number(a) and _is_number(b):
        return float(a) == float(b)
    if isinstance(a, str) and isinstance(b, str):
        return _norm(a) == _norm(b)
    return type(a) is type(b) and a == b


def _check_value(cond: Condition, value: Any) -> bool:
    """Evaluate every operator of one condition.  Raises _Unchecked."""
    for op, expected in cond.ops.items():
        if op == "exists":
            ok = expected  # value is present here
        elif op == "regex":
            ok = bool(cond.regexes["regex"].search(_as_text(value)))
        elif op == "not_regex":
            ok = not cond.regexes["not_regex"].search(_as_text(value))
        elif op == "equals":
            if isinstance(value, dict | list):
                raise _Unchecked
            ok = _same(value, expected)
        elif op == "in":
            if isinstance(value, dict | list):
                raise _Unchecked
            ok = any(_same(value, e) for e in expected)
        else:
            n = _as_number(value)
            ok = {"gt": n > expected, "gte": n >= expected, "lt": n < expected, "lte": n <= expected}[op]
        if not ok:
            return False
    return True


def _leaves(value: Any, depth: int = 0):
    """Every key and scalar inside the arguments (for the '*' path)."""
    if depth > 32:
        raise _Unchecked
    if isinstance(value, dict):
        for k, v in value.items():
            yield k
            yield from _leaves(v, depth + 1)
    elif isinstance(value, list):
        for v in value:
            yield from _leaves(v, depth + 1)
    elif value is not None:
        yield value


def _condition_matches(rule: ArgRule, cond: Condition, args: Any) -> bool:
    hold = rule.action == "hold"
    if cond.path == "*":
        try:
            return any(cond.regexes["regex"].search(_as_text(leaf)) for leaf in _leaves(args))
        except _Unchecked:
            return True  # hold-only path: can't check → hold
    value = _lookup(args, cond.path)
    if value is _MISSING:
        return cond.ops.get("exists") is False and len(cond.ops) == 1
    if cond.ops.get("exists") is False:
        return False
    try:
        return _check_value(cond, value)
    except _Unchecked:
        return hold  # hold rules fail closed, allow rules don't match


def rule_matches(rule: ArgRule, tool_name: str, args: Any) -> bool:
    return rule.applies_to(tool_name) and all(_condition_matches(rule, c, args) for c in rule.conditions)
