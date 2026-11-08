from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from . import db
from .auth import require_approver, require_viewer
from .db import get_audit_log, get_pending_actions, get_resolved_history
from .events import broadcast, subscribe
from .proxy_engine import resolve_waiter

router = APIRouter()


class ResolveRequest(BaseModel):
    action_id: str
    decision: str
    rejection_reason: Optional[str] = None


@router.get("/api/pending", dependencies=[Depends(require_viewer)])
def list_pending():
    db.ensure_db()
    return get_pending_actions()


@router.post("/api/resolve", dependencies=[Depends(require_approver)])
async def resolve_action(req: ResolveRequest, request: Request):
    db.ensure_db()
    if req.decision not in ["APPROVED", "REJECTED"]:
        raise HTTPException(status_code=400, detail="Decision must be APPROVED or REJECTED")

    resolved_by = getattr(request.state, "key_label", None) or "unknown"

    # Write to DB first so /api/pending never returns this action again,
    # even if the poll fires before the waiter coroutine resumes.
    updated = db.update_action_status(
        req.action_id, req.decision, req.rejection_reason, resolved_by=resolved_by
    )
    if not updated:
        raise HTTPException(status_code=404, detail="Action not found or already resolved")

    resolve_waiter(req.action_id, req.decision, req.rejection_reason)
    broadcast("action_resolved", {"action_id": req.action_id, "status": req.decision})

    event_type = "action.approved" if req.decision == "APPROVED" else "action.rejected"
    db.insert_audit_log(
        event_type,
        action_id=req.action_id,
        actor=resolved_by,
        ip_address=request.client.host if request.client else None,
        detail={"rejection_reason": req.rejection_reason} if req.rejection_reason else None,
    )

    return {"status": "ok", "action_id": req.action_id, "resolved_as": req.decision, "resolved_by": resolved_by}


@router.get("/api/events", dependencies=[Depends(require_viewer)])
async def sse_events():
    """Server-Sent Events stream.  Auth handled by the middleware (including
    the ?api_key= query param path used by EventSource)."""
    async def _stream():
        async for chunk in subscribe():
            yield chunk
    return StreamingResponse(_stream(), media_type="text/event-stream")


@router.get("/api/history", dependencies=[Depends(require_viewer)])
def list_history(limit: int = 20):
    db.ensure_db()
    return get_resolved_history(limit)


@router.get("/api/audit", dependencies=[Depends(require_viewer)])
def list_audit(
    limit: int = 50,
    action_id: Optional[str] = None,
    event_type: Optional[str] = None,
):
    db.ensure_db()
    return get_audit_log(limit=limit, action_id=action_id, event_type=event_type)
