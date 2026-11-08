from __future__ import annotations

import csv
import io
import json

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import PlainTextResponse, Response, StreamingResponse
from pydantic import BaseModel

from . import db
from . import metrics as _metrics
from .auth import require_admin, require_approver, require_viewer
from .db import get_audit_log, get_pending_actions, get_resolved_history
from .events import broadcast, subscribe
from .logs import log
from .notifications import notify_action_resolved, schedule
from .proxy_engine import resolve_waiter

router = APIRouter()


class ResolveRequest(BaseModel):
    action_id: str
    decision: str
    rejection_reason: str | None = None


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
    rejection_reason = req.rejection_reason if req.decision == "REJECTED" else None

    # The conditional UPDATE (WHERE status='PENDING') is the single point of
    # truth: exactly one reviewer wins, and the waiting agent reads the
    # decision from the database.
    updated = db.update_action_status(req.action_id, req.decision, rejection_reason, resolved_by=resolved_by)
    if not updated:
        raise HTTPException(status_code=404, detail="Action not found or already resolved")

    resolve_waiter(req.action_id, req.decision, rejection_reason)
    broadcast("action_resolved", {"action_id": req.action_id, "status": req.decision})

    event_type = "action.approved" if req.decision == "APPROVED" else "action.rejected"
    db.insert_audit_log(
        event_type,
        action_id=req.action_id,
        actor=resolved_by,
        ip_address=request.client.host if request.client else None,
        detail={"rejection_reason": rejection_reason} if rejection_reason else None,
    )
    log.info(event_type, extra={"action_id": req.action_id, "actor": resolved_by})

    row = db.get_action(req.action_id) or {}
    schedule(
        notify_action_resolved(
            action_id=req.action_id,
            tool_name=row.get("tool_name", ""),
            status=req.decision,
            resolved_by=resolved_by,
        )
    )

    return {"status": "ok", "action_id": req.action_id, "resolved_as": req.decision, "resolved_by": resolved_by}


@router.get("/api/events", dependencies=[Depends(require_viewer)])
async def sse_events():
    """Server-Sent Events stream.  Auth handled by the middleware (including
    the ?api_key= query param, accepted only on this path)."""

    async def _stream():
        async for chunk in subscribe():
            yield chunk

    return StreamingResponse(
        _stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.get("/api/history", dependencies=[Depends(require_viewer)])
def list_history(limit: int = Query(20, ge=1, le=1000)):
    db.ensure_db()
    return get_resolved_history(limit)


@router.get("/api/audit", dependencies=[Depends(require_viewer)])
def list_audit(
    limit: int = Query(50, ge=1, le=1000),
    action_id: str | None = None,
    event_type: str | None = None,
):
    db.ensure_db()
    return get_audit_log(limit=limit, action_id=action_id, event_type=event_type)


@router.get("/api/audit/export", dependencies=[Depends(require_admin)])
def export_audit(
    format: str = Query("csv", pattern="^(csv|jsonl)$"),
    since: str | None = None,
    until: str | None = None,
    limit: int = Query(100_000, ge=1, le=100_000),
):
    """Download the audit log (oldest first) as CSV or JSON Lines, including
    each row's hash so the export can be checked against the chain."""
    db.ensure_db()
    rows = list(reversed(get_audit_log(limit=limit, since=since, until=until)))
    if format == "jsonl":
        body = "".join(json.dumps(r, default=str) + "\n" for r in rows)
        return Response(
            body,
            media_type="application/x-ndjson",
            headers={"Content-Disposition": 'attachment; filename="escrow-audit.jsonl"'},
        )
    buf = io.StringIO()
    cols = ["id", "created_at", "event_type", "action_id", "actor", "ip_address", "detail", "prev_hash", "hash"]
    writer = csv.DictWriter(buf, fieldnames=cols, extrasaction="ignore")
    writer.writeheader()
    for r in rows:
        r = dict(r)
        if isinstance(r.get("detail"), dict | list):
            r["detail"] = json.dumps(r["detail"], sort_keys=True)
        writer.writerow(r)
    return Response(
        buf.getvalue(),
        media_type="text/csv",
        headers={"Content-Disposition": 'attachment; filename="escrow-audit.csv"'},
    )


@router.get("/api/audit/verify", dependencies=[Depends(require_admin)])
def verify_audit():
    """Recompute the audit hash chain and report the first tampered row."""
    db.ensure_db()
    return db.verify_audit_chain()


@router.get("/metrics", dependencies=[Depends(require_viewer)])
def prometheus_metrics():
    db.ensure_db()
    return PlainTextResponse(
        _metrics.collect(),
        media_type="text/plain; version=0.0.4; charset=utf-8",
    )
