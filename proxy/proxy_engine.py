from __future__ import annotations

import asyncio
import uuid
from typing import Any, Dict

from . import db
from .config import classify_tool_call
from .events import broadcast
from .forwarder import forward as _forward
from .notifications import notify_action_added

EXECUTION_WAITERS: Dict[str, Dict[str, Any]] = {}


async def process_mcp_call(
    payload: dict,
    timeout: float = 300.0,
    forward_headers: dict | None = None,
) -> dict:
    db.ensure_db()
    rpc_id = payload.get("id", str(uuid.uuid4()))
    params = payload.get("params", {})
    tool_name = params.get("name", "unknown_tool")
    tool_args = params.get("arguments", {})
    reason = tool_args.get("reason") or "No justification provided by agent."

    classification = classify_tool_call(tool_name)

    if classification["action"] == "AUTO_APPROVE":
        return await _forward(payload, forward_headers)

    action_id = f"act_{uuid.uuid4().hex[:12]}"
    db.insert_action(
        action_id=action_id,
        rpc_id=str(rpc_id),
        tool_name=tool_name,
        category=classification["category"],
        risk_level=classification["risk_level"],
        reason=reason,
        args=tool_args,
    )
    broadcast("action_added", {"action_id": action_id, "tool_name": tool_name})
    await notify_action_added(
        action_id=action_id,
        tool_name=tool_name,
        risk_level=classification["risk_level"],
        category=classification["category"],
        reason=reason,
    )

    event = asyncio.Event()
    EXECUTION_WAITERS[action_id] = {
        "event": event,
        "decision": None,
        "rejection_reason": None,
    }

    try:
        await asyncio.wait_for(event.wait(), timeout=timeout)
    except asyncio.TimeoutError:
        EXECUTION_WAITERS.pop(action_id, None)
        db.update_action_status(action_id, "REJECTED", "Hold timed out after 300 seconds.")
        return {
            "jsonrpc": "2.0",
            "id": rpc_id,
            "error": {"code": -32000, "message": "Escrow Timeout: Action was not approved by human in time."},
        }

    waiter_entry = EXECUTION_WAITERS.pop(action_id, None)
    if waiter_entry is None:
        return {
            "jsonrpc": "2.0",
            "id": rpc_id,
            "error": {"code": -32000, "message": "Escrow state lost for action."},
        }

    decision = waiter_entry.get("decision")
    rejection_reason = waiter_entry.get("rejection_reason")

    if decision == "APPROVED":
        db.update_action_status(action_id, "APPROVED")
        return await _forward(payload, forward_headers)

    db.update_action_status(action_id, "REJECTED", rejection_reason)
    return {
        "jsonrpc": "2.0",
        "id": rpc_id,
        "error": {
            "code": -32000,
            "message": f"Action rejected by supervisor: {rejection_reason or 'No reason provided.'}",
        },
    }


def resolve_waiter(action_id: str, decision: str, rejection_reason: str | None = None) -> bool:
    if action_id not in EXECUTION_WAITERS:
        return False

    entry = EXECUTION_WAITERS[action_id]
    entry["decision"] = decision
    entry["rejection_reason"] = rejection_reason
    entry["event"].set()
    return True
