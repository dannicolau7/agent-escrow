from __future__ import annotations

import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import List, Optional

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles

from . import db, escrow
from .models import (
    CallRecord,
    ReviewDecision,
    ToolCallRequest,
    ToolCallResponse,
    WaitResponse,
)
from .policies import classify, load_policy
from .proxy_engine import process_mcp_call
from .routes_ui import router as ui_router


def _row_to_record(row: dict) -> CallRecord:
    return CallRecord(
        call_id=row["call_id"],
        agent_id=row["agent_id"],
        tool_name=row["tool_name"],
        tool_input=row["tool_input"],
        status=row["status"],
        is_mutating=bool(row["is_mutating"]),
        policy_reason=row["policy_reason"],
        created_at=row["created_at"],
        resolved_at=row["resolved_at"],
        resolved_by=row["resolved_by"],
    )


@asynccontextmanager
async def lifespan(app: FastAPI):
    db.init_db()
    load_policy()
    yield


app = FastAPI(
    title="Agent Escrow Proxy",
    description="Intercepts mutating tool calls and holds them for human review.",
    version="1.0.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
app.include_router(ui_router)

_WEB_DIR = Path(__file__).parent.parent / "web"
app.mount("/ui", StaticFiles(directory=str(_WEB_DIR), html=True), name="ui")


# ---------------------------------------------------------------------------
# Root redirect
# ---------------------------------------------------------------------------

@app.get("/", include_in_schema=False)
async def root():
    return RedirectResponse(url="/ui/")


# ---------------------------------------------------------------------------
# Health
# ---------------------------------------------------------------------------

@app.get("/health", tags=["meta"])
async def health():
    return {"status": "ok", "pending_calls": escrow.pending_count()}


# ---------------------------------------------------------------------------
# Submit a tool call
# ---------------------------------------------------------------------------

@app.post("/calls", response_model=ToolCallResponse, status_code=201, tags=["agent"])
async def submit_call(body: ToolCallRequest):
    """
    Agent submits a tool call.  If the policy marks it mutating the call is
    held in escrow (status=pending) until a reviewer acts on it.  Otherwise
    it is immediately allowed.
    """
    call_id = str(uuid.uuid4())
    is_mut, reason = classify(body.tool_name)

    db.insert_call(
        call_id=call_id,
        agent_id=body.agent_id,
        tool_name=body.tool_name,
        tool_input=body.tool_input,
        is_mutating=is_mut,
        policy_reason=reason,
    )

    if is_mut:
        escrow.register(call_id)
        return ToolCallResponse(
            call_id=call_id,
            status="pending",
            is_mutating=True,
            message=f"Call held for review: {reason}",
        )

    return ToolCallResponse(
        call_id=call_id,
        status="allowed",
        is_mutating=False,
        message="Non-mutating call; proceeding immediately.",
    )


# ---------------------------------------------------------------------------
# List / fetch calls
# ---------------------------------------------------------------------------

@app.get("/calls", response_model=List[CallRecord], tags=["reviewer"])
async def list_calls(status: Optional[str] = Query(None, description="Filter by status")):
    return [_row_to_record(r) for r in db.list_calls(status=status)]


@app.get("/calls/{call_id}", response_model=CallRecord, tags=["agent", "reviewer"])
async def get_call(call_id: str):
    row = db.get_call(call_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Call not found")
    return _row_to_record(row)


# ---------------------------------------------------------------------------
# Long-poll wait (agent blocks here until resolved)
# ---------------------------------------------------------------------------

@app.get("/calls/{call_id}/wait", response_model=WaitResponse, tags=["agent"])
async def wait_for_call(
    call_id: str,
    timeout: float = Query(60.0, ge=1.0, le=300.0, description="Max seconds to wait"),
):
    """
    Agent long-polls here after submitting a mutating call.
    Returns when the call is approved/rejected, or when the timeout elapses.
    """
    row = db.get_call(call_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Call not found")

    if row["status"] != "pending":
        return WaitResponse(call_id=call_id, status=row["status"], timed_out=False)

    outcome = await escrow.wait(call_id, timeout=timeout)
    row = db.get_call(call_id)
    return WaitResponse(
        call_id=call_id,
        status=row["status"] if row else "unknown",
        timed_out=(outcome == "timeout"),
    )


# ---------------------------------------------------------------------------
# Review actions
# ---------------------------------------------------------------------------

@app.post("/calls/{call_id}/approve", tags=["reviewer"])
async def approve_call(call_id: str, body: ReviewDecision):
    row = db.get_call(call_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Call not found")
    if row["status"] != "pending":
        raise HTTPException(status_code=409, detail=f"Call is already '{row['status']}'")

    if not db.update_call_status(call_id, "approved", resolved_by=body.reviewer_id):
        raise HTTPException(status_code=409, detail="Concurrent resolution detected")

    escrow.resolve(call_id)
    return {"call_id": call_id, "status": "approved"}


@app.post("/calls/{call_id}/reject", tags=["reviewer"])
async def reject_call(call_id: str, body: ReviewDecision):
    row = db.get_call(call_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Call not found")
    if row["status"] != "pending":
        raise HTTPException(status_code=409, detail=f"Call is already '{row['status']}'")

    if not db.update_call_status(call_id, "rejected", resolved_by=body.reviewer_id):
        raise HTTPException(status_code=409, detail="Concurrent resolution detected")

    escrow.resolve(call_id)
    return {"call_id": call_id, "status": "rejected"}


# ---------------------------------------------------------------------------
# JSON-RPC MCP entry point from the project master plan
# ---------------------------------------------------------------------------

@app.post("/mcp")
async def handle_mcp(request: Request):
    payload = await request.json()
    response = await process_mcp_call(payload)
    return response
