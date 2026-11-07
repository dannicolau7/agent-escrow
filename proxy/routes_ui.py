from __future__ import annotations

import json
from typing import Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from . import db
from .db import get_pending_actions, get_resolved_history
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

    success = resolve_waiter(req.action_id, req.decision, req.rejection_reason)
    if not success:
        raise HTTPException(status_code=404, detail="Action not found or already resolved")

    return {"status": "ok", "action_id": req.action_id, "resolved_as": req.decision}


@router.get("/api/history")
def list_history(limit: int = 20):
    db.ensure_db()
    return get_resolved_history(limit)
