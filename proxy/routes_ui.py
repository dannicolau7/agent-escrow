from __future__ import annotations

import json
from typing import Optional

from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from . import db
from .db import get_pending_actions, get_resolved_history
from .events import broadcast, subscribe
from .proxy_engine import resolve_waiter

router = APIRouter()


class ResolveRequest(BaseModel):
    action_id: str
    decision: str
    rejection_reason: Optional[str] = None


@router.get("/api/pending")
def list_pending():
    db.ensure_db()
    raw_list = get_pending_actions()
    return raw_list


@router.post("/api/resolve")
async def resolve_action(req: ResolveRequest):
    db.ensure_db()
    if req.decision not in ["APPROVED", "REJECTED"]:
        raise HTTPException(status_code=400, detail="Decision must be APPROVED or REJECTED")

    # Write to DB first so /api/pending never returns this action again,
    # even if the poll fires before the waiter coroutine resumes.
    updated = db.update_action_status(req.action_id, req.decision, req.rejection_reason)
    if not updated:
        raise HTTPException(status_code=404, detail="Action not found or already resolved")

    # Signal the live waiter if the agent is still connected (no-op for stale actions).
    resolve_waiter(req.action_id, req.decision, req.rejection_reason)
    broadcast("action_resolved", {"action_id": req.action_id, "status": req.decision})

    return {"status": "ok", "action_id": req.action_id, "resolved_as": req.decision}


@router.get("/api/events")
async def sse_events():
    """Server-Sent Events stream — replaces 3-second polling in the dashboard."""
    async def _stream():
        async for chunk in subscribe():
            yield chunk
    return StreamingResponse(_stream(), media_type="text/event-stream")


@router.get("/api/history")
def list_history(limit: int = 20):
    db.ensure_db()
    return get_resolved_history(limit)
