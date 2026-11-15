from __future__ import annotations

import asyncio
import json
import uuid
from typing import Any

from . import db, settings
from .config import classify_tool_call
from .events import broadcast
from .forwarder import forward_full
from .logs import log
from .notifications import notify_action_added, notify_action_resolved, schedule

EXECUTION_WAITERS: dict[str, dict[str, Any]] = {}


def _rpc_error(rpc_id: Any, message: str, code: int = -32000) -> dict:
    return {"jsonrpc": "2.0", "id": rpc_id, "error": {"code": code, "message": message}}


async def _execute(
    action_id: str | None,
    payload: dict,
    forward_headers: dict | None,
    tool_name: str,
    actor: str | None,
    ip_address: str | None,
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


RESULT_TOOL = "escrow_get_result"

RESULT_TOOL_DEFINITION = {
    "name": RESULT_TOOL,
    "description": (
        "Get the outcome of a tool call that AgentEscrow held for human review. "
        "Call it with the action_id from the 'held for review' message. "
        "status is PENDING (ask again in ~10 seconds), REJECTED, or APPROVED with the tool's real result."
    ),
    "inputSchema": {
        "type": "object",
        "properties": {"action_id": {"type": "string", "description": "The act_… id of the held call"}},
        "required": ["action_id"],
    },
}


def _held_result(rpc_id: Any, action_id: str, tool_name: str, risk_level: str) -> dict:
    """Immediate answer to a held call in async mode.  Not an error: the call
    was accepted for review but has NOT run yet."""
    text = (
        f"HELD FOR HUMAN REVIEW: {tool_name} ({risk_level}) has NOT run yet. "
        f"action_id={action_id}. Call the `{RESULT_TOOL}` tool with "
        f'{{"action_id": "{action_id}"}} every ~10 seconds until it reports APPROVED or REJECTED. '
        "Do not retry the original call."
    )
    return {
        "jsonrpc": "2.0",
        "id": rpc_id,
        "result": {
            "content": [{"type": "text", "text": text}],
            "structuredContent": {"status": "PENDING", "action_id": action_id, "tool_name": tool_name},
            "isError": False,
        },
    }


def _session_headers(forward_headers: dict | None) -> dict:
    lower = {k.lower(): v for k, v in (forward_headers or {}).items()}
    return {k: lower[k] for k in ("mcp-session-id", "mcp-protocol-version") if k in lower}


async def process_mcp_call(
    payload: dict,
    timeout: float | None = None,  # noqa: ASYNC109 (hold timeout, not an I/O timeout)
    forward_headers: dict | None = None,
    submitted_by: str | None = None,
    ip_address: str | None = None,
    hold_mode: str | None = None,
) -> dict:
    db.ensure_db()
    if timeout is None:
        timeout = settings.hold_timeout()
    if hold_mode is None:
        hold_mode = settings.hold_mode()
    rpc_id = payload.get("id", str(uuid.uuid4()))
    params = payload.get("params") or {}
    tool_name = params.get("name") or "unknown_tool"
    tool_args = params.get("arguments") or {}
    reason = tool_args.get("reason") if isinstance(tool_args.get("reason"), str) else None
    reason = reason or "No justification provided by agent."

    classification = classify_tool_call(tool_name, tool_args)
    matched_rule = {"rule": classification["rule"]} if "rule" in classification else {}

    if classification["action"] == "AUTO_APPROVE":
        db.insert_audit_log(
            "action.auto_approved",
            actor=submitted_by,
            ip_address=ip_address,
            detail={"tool_name": tool_name, "arguments": tool_args, **matched_rule},
        )
        return await _execute(None, payload, forward_headers, tool_name, submitted_by, ip_address)

    action_id = f"act_{uuid.uuid4().hex[:12]}"
    session = _session_headers(forward_headers)

    # Wait mode: register the waiter BEFORE the action becomes visible
    # anywhere, so a reviewer can never resolve it before we're listening.
    event = asyncio.Event()
    if hold_mode == "wait":
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
            hold_mode=hold_mode,
            rpc_payload=payload,
            session_id=session.get("mcp-session-id"),
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
                "hold_mode": hold_mode,
                **matched_rule,
            },
        )
        log.info(
            "action.held",
            extra={
                "action_id": action_id,
                "tool_name": tool_name,
                "risk_level": classification["risk_level"],
                "hold_mode": hold_mode,
            },
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

        if hold_mode == "async":
            return _held_result(rpc_id, action_id, tool_name, classification["risk_level"])

        try:
            await asyncio.wait_for(event.wait(), timeout=timeout)
        except TimeoutError:
            if _expire(action_id, tool_name, timeout):
                return _rpc_error(rpc_id, "Escrow Timeout: Action was not approved by human in time.")
            # A reviewer resolved it at the last moment; fall through and honour that.
    except asyncio.CancelledError:
        # Agent disconnected / server shutting down: don't leave it approvable.
        if db.update_action_status(action_id, "REJECTED", "Agent disconnected before review.", resolved_by="system"):
            db.insert_audit_log("action.cancelled", action_id=action_id, detail={"tool_name": tool_name})
            broadcast("action_resolved", {"action_id": action_id, "status": "REJECTED"})
            log.info("action.cancelled", extra={"action_id": action_id, "tool_name": tool_name})
        raise
    finally:
        EXECUTION_WAITERS.pop(action_id, None)

    # The database is the source of truth for the decision.
    row = db.get_action(action_id) or {}
    if row.get("status") == "APPROVED":
        return await _execute(action_id, payload, forward_headers, tool_name, row.get("resolved_by"), ip_address)

    rejection_reason = row.get("rejection_reason")
    return _rpc_error(rpc_id, f"Action rejected by supervisor: {rejection_reason or 'No reason provided.'}")


def _expire(action_id: str, tool_name: str, timeout: float) -> bool:
    """Reject a still-pending action as timed out.  Returns True if it was."""
    if not db.update_action_status(
        action_id, "REJECTED", f"Hold timed out after {timeout:g} seconds.", resolved_by="system"
    ):
        return False
    db.insert_audit_log("action.timeout", action_id=action_id, detail={"tool_name": tool_name})
    broadcast("action_resolved", {"action_id": action_id, "status": "REJECTED"})
    schedule(notify_action_resolved(action_id=action_id, tool_name=tool_name, status="TIMEOUT", resolved_by="system"))
    log.info("action.timeout", extra={"action_id": action_id, "tool_name": tool_name})
    return True


def expire_async_holds() -> int:
    """Time out async-mode actions nobody reviewed.  Called periodically."""
    timeout = settings.hold_timeout()
    expired = 0
    for row in db.list_expired_async(timeout):
        if _expire(row["id"], row["tool_name"], timeout):
            expired += 1
    return expired


async def execute_approved_async(action_id: str, actor: str | None, ip_address: str | None) -> None:
    """Run an approved async-mode action upstream (there is no waiting request)."""
    row = db.get_action(action_id)
    if not row or row.get("status") != "APPROVED" or row.get("hold_mode") != "async":
        return
    try:
        payload = json.loads(row["rpc_payload"])
    except (TypeError, json.JSONDecodeError):
        db.record_execution(action_id, "FAILED", {"error": "stored request could not be read"})
        return
    headers = {"mcp-session-id": row["session_id"]} if row.get("session_id") else {}
    await _execute(action_id, payload, headers, row["tool_name"], actor, ip_address)


def get_result(rpc_id: Any, action_id: Any, caller: str | None, caller_role: str) -> dict:
    """Answer the escrow_get_result virtual tool."""

    def reply(data: dict, text: str, is_error: bool = False) -> dict:
        return {
            "jsonrpc": "2.0",
            "id": rpc_id,
            "result": {"content": [{"type": "text", "text": text}], "structuredContent": data, "isError": is_error},
        }

    if not isinstance(action_id, str) or not action_id:
        return _rpc_error(rpc_id, "Invalid params: action_id must be a string", code=-32602)
    row = db.get_action(action_id)
    # An agent may only read its own actions (admins may read any).
    if not row or (caller_role != "admin" and row.get("submitted_by") != caller):
        return _rpc_error(rpc_id, f"Unknown action_id: {action_id}", code=-32602)

    status = row["status"]
    if status == "PENDING":
        return reply(
            {"status": "PENDING", "action_id": action_id}, "Still waiting for human review. Ask again in ~10 seconds."
        )
    if status == "REJECTED":
        why = row.get("rejection_reason") or "No reason provided."
        return reply(
            {"status": "REJECTED", "action_id": action_id, "reason": why},
            f"REJECTED by reviewer: {why}. The action did not run.",
            is_error=True,
        )
    # APPROVED
    if not row.get("execution_status"):
        return reply(
            {"status": "APPROVED", "action_id": action_id, "execution": "RUNNING"},
            "Approved; running now. Ask again in a few seconds.",
        )
    try:
        upstream = json.loads(row.get("execution_result") or "{}")
    except json.JSONDecodeError:
        upstream = {"result": {"content": [{"type": "text", "text": row.get("execution_result") or ""}]}}
    if row["execution_status"] == "SUCCEEDED" and isinstance(upstream.get("result"), dict):
        result: dict[str, Any] = dict(upstream["result"])
        meta: dict[str, Any] = dict(result.get("structuredContent") or {})
        meta.update(escrow_status="APPROVED", action_id=action_id, approved_by=row.get("resolved_by"))
        result["structuredContent"] = meta
        return {"jsonrpc": "2.0", "id": rpc_id, "result": result}
    err = upstream.get("error") or (upstream.get("result") or {}).get("content")
    return reply(
        {"status": "APPROVED", "action_id": action_id, "execution": "FAILED", "error": err},
        f"Approved, but the tool failed when it ran: {json.dumps(err)[:500]}",
        is_error=True,
    )


def resolve_waiter(action_id: str, decision: str, rejection_reason: str | None = None) -> bool:
    entry = EXECUTION_WAITERS.get(action_id)
    if entry is None:
        return False
    entry["decision"] = decision
    entry["rejection_reason"] = rejection_reason
    entry["event"].set()
    return True
