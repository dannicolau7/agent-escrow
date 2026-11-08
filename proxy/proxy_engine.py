from __future__ import annotations

import asyncio
import uuid
from typing import Any, Dict, Optional

from . import db, settings
from .config import classify_tool_call
from .events import broadcast
from .forwarder import forward_full
from .logs import log
from .notifications import notify_action_added, notify_action_resolved, schedule

EXECUTION_WAITERS: Dict[str, Dict[str, Any]] = {}


def _rpc_error(rpc_id: Any, message: str, code: int = -32000) -> dict:
    return {"jsonrpc": "2.0", "id": rpc_id, "error": {"code": code, "message": message}}


async def _execute(
    action_id: Optional[str],
    payload: dict,
    forward_headers: dict | None,
    tool_name: str,
    actor: Optional[str],
    ip_address: Optional[str],
) -> dict:
    """Send the call upstream and record what actually happened."""
    result = await forward_full(payload, forward_headers)
    body = result.body if result.body is not None else {"jsonrpc": "2.0", "id": payload.get("id"), "result": {}}
    # A tool can also report failure inside a successful JSON-RPC result.
    tool_failed = isinstance(body.get("result"), dict) and body["result"].get("isError") is True
    succeeded = result.ok and not tool_failed
    status = "SUCCEEDED" if succeeded else "FAILED"

    if action_id:
        db.record_execution(action_id, status, body)
    db.insert_audit_log(
        "action.executed" if succeeded else "action.execution_failed",
        action_id=action_id,
        actor=actor,
        ip_address=ip_address,
        detail={"tool_name": tool_name, "error": body.get("error")} if not succeeded else {"tool_name": tool_name},
    )
    log.info(
        "action.executed" if succeeded else "action.execution_failed",
        extra={"action_id": action_id, "tool_name": tool_name},
    )
    return body


async def process_mcp_call(
    payload: dict,
    timeout: float | None = None,
    forward_headers: dict | None = None,
    submitted_by: str | None = None,
    ip_address: str | None = None,
) -> dict:
    db.ensure_db()
    if timeout is None:
        timeout = settings.hold_timeout()
    rpc_id = payload.get("id", str(uuid.uuid4()))
    params = payload.get("params") or {}
    tool_name = params.get("name") or "unknown_tool"
    tool_args = params.get("arguments") or {}
    reason = tool_args.get("reason") if isinstance(tool_args.get("reason"), str) else None
    reason = reason or "No justification provided by agent."

    classification = classify_tool_call(tool_name)

    if classification["action"] == "AUTO_APPROVE":
        db.insert_audit_log(
            "action.auto_approved",
            actor=submitted_by,
            ip_address=ip_address,
            detail={"tool_name": tool_name, "arguments": tool_args},
        )
        return await _execute(None, payload, forward_headers, tool_name, submitted_by, ip_address)

    action_id = f"act_{uuid.uuid4().hex[:12]}"

    # Register the waiter BEFORE the action becomes visible anywhere, so a
    # reviewer can never resolve it before we're listening.
    event = asyncio.Event()
    EXECUTION_WAITERS[action_id] = {"event": event, "decision": None, "rejection_reason": None}

    try:
        db.insert_action(
            action_id=action_id,
            rpc_id=str(rpc_id),
            tool_name=tool_name,
            category=classification["category"],
            risk_level=classification["risk_level"],
            reason=reason,
            args=tool_args,
            submitted_by=submitted_by,
        )
        db.insert_audit_log(
            "action.created",
            action_id=action_id,
            actor=submitted_by,
            ip_address=ip_address,
            detail={
                "tool_name": tool_name,
                "risk_level": classification["risk_level"],
                "category": classification["category"],
            },
        )
        log.info(
            "action.held",
            extra={"action_id": action_id, "tool_name": tool_name, "risk_level": classification["risk_level"]},
        )
        broadcast("action_added", {"action_id": action_id, "tool_name": tool_name})
        # Notifications run in the background so a slow webhook never delays the hold.
        schedule(
            notify_action_added(
                action_id=action_id,
                tool_name=tool_name,
                risk_level=classification["risk_level"],
                category=classification["category"],
                reason=reason,
            )
        )

        try:
            await asyncio.wait_for(event.wait(), timeout=timeout)
        except asyncio.TimeoutError:
            if db.update_action_status(
                action_id, "REJECTED", f"Hold timed out after {timeout:g} seconds.", resolved_by="system"
            ):
                db.insert_audit_log("action.timeout", action_id=action_id, detail={"tool_name": tool_name})
                broadcast("action_resolved", {"action_id": action_id, "status": "REJECTED"})
                schedule(notify_action_resolved(action_id=action_id, tool_name=tool_name, status="TIMEOUT", resolved_by="system"))
                log.info("action.timeout", extra={"action_id": action_id, "tool_name": tool_name})
                return _rpc_error(rpc_id, "Escrow Timeout: Action was not approved by human in time.")
            # A reviewer resolved it at the last moment; fall through and honour that.
    except asyncio.CancelledError:
        # Agent disconnected / server shutting down: don't leave it approvable.
        if db.update_action_status(action_id, "REJECTED", "Agent disconnected before review.", resolved_by="system"):
            db.insert_audit_log("action.cancelled", action_id=action_id, detail={"tool_name": tool_name})
            broadcast("action_resolved", {"action_id": action_id, "status": "REJECTED"})
        raise
    finally:
        EXECUTION_WAITERS.pop(action_id, None)

    # The database is the source of truth for the decision.
    row = db.get_action(action_id) or {}
    if row.get("status") == "APPROVED":
        return await _execute(action_id, payload, forward_headers, tool_name, row.get("resolved_by"), ip_address)

    rejection_reason = row.get("rejection_reason")
    return _rpc_error(rpc_id, f"Action rejected by supervisor: {rejection_reason or 'No reason provided.'}")


def resolve_waiter(action_id: str, decision: str, rejection_reason: str | None = None) -> bool:
    entry = EXECUTION_WAITERS.get(action_id)
    if entry is None:
        return False
    entry["decision"] = decision
    entry["rejection_reason"] = rejection_reason
    entry["event"].set()
    return True
