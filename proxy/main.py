from __future__ import annotations

import asyncio
import contextlib
import json
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, RedirectResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles

from . import db, logs, settings
from .auth import api_key_middleware, require_agent
from .config import classify_tool_call, load_policy
from .events import broadcast
from .forwarder import forward_full
from .logs import log
from .proxy_engine import RESULT_TOOL, RESULT_TOOL_DEFINITION, expire_async_holds, get_result, process_mcp_call
from .routes_ui import router as ui_router

# MCP methods that are passed straight through without escrow.  These are the
# handshake, discovery and read-only methods of the MCP spec.  Anything else
# except "tools/call" (escrowed) and "notifications/*" is refused, so a new or
# made-up method can't be used to slip past the escrow.
PASSTHROUGH_METHODS = {
    "initialize",
    "ping",
    "tools/list",
    "resources/list",
    "resources/templates/list",
    "resources/read",
    "prompts/list",
    "prompts/get",
    "completion/complete",
}
# Passthrough methods worth an audit entry (they return data to the agent).
_AUDITED_PASSTHROUGH = {"resources/read", "prompts/get"}


@asynccontextmanager
async def lifespan(app: FastAPI):
    logs.setup()
    settings.validate_startup()
    db.init_db()
    load_policy()
    stale = db.reject_stale_pending("Server restarted; action could not be held.")
    for action_id in stale:
        broadcast("action_resolved", {"action_id": action_id, "status": "REJECTED"})
    db.insert_audit_log("server.started", detail={"stale_purged": len(stale), "mode": settings.mode()})
    log.info(
        "server.started",
        extra={"mode": settings.mode(), "stale_purged": len(stale), "hold_mode": settings.hold_mode()},
    )
    sweeper = asyncio.create_task(_sweep_expired_holds())
    yield
    sweeper.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await sweeper


async def _sweep_expired_holds(interval: float = 15.0) -> None:
    """Async-mode actions have no waiting request to time them out."""
    while True:
        await asyncio.sleep(interval)
        try:
            expire_async_holds()
        except Exception:
            log.exception("sweeper.failed")


app = FastAPI(
    title="Agent Escrow Proxy",
    description="Intercepts mutating tool calls and holds them for human review.",
    version="1.1.0",
    lifespan=lifespan,
)

# Order matters: Starlette runs the LAST-added middleware first.  Auth is
# added first and CORS second so CORS wraps everything, answers preflight
# requests, and adds its headers to 401/403 responses too.
app.middleware("http")(api_key_middleware)
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins(),
    allow_credentials=False,  # auth uses headers, never cookies
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["Authorization", "X-Api-Key", "Content-Type", "Mcp-Session-Id", "Mcp-Protocol-Version"],
    expose_headers=["Mcp-Session-Id"],
)
app.include_router(ui_router)

_WEB_OUT = Path(__file__).parent.parent / "web" / "out"
_WEB_DIR = _WEB_OUT if _WEB_OUT.exists() else Path(__file__).parent.parent / "web"
app.mount("/ui", StaticFiles(directory=str(_WEB_DIR), html=True), name="ui")


@app.get("/", include_in_schema=False)
async def root():
    return RedirectResponse(url="/ui/")


@app.get("/health", tags=["meta"])
async def health():
    return {"status": "ok"}


def _rpc_error(rpc_id, code: int, message: str) -> dict:
    return {"jsonrpc": "2.0", "id": rpc_id, "error": {"code": code, "message": message}}


@app.post("/mcp", dependencies=[Depends(require_agent)])
async def handle_mcp(request: Request):
    try:
        payload = await request.json()
    except Exception:
        return _rpc_error(None, -32700, "Parse error")

    if not isinstance(payload, dict):
        return _rpc_error(None, -32600, "Invalid Request: expected an object")

    rpc_id = payload.get("id")
    method = payload.get("method")
    if not isinstance(method, str):
        return _rpc_error(rpc_id, -32600, "Invalid Request: method must be a string")

    submitter = getattr(request.state, "key_label", None)
    client_ip = request.client.host if request.client else None

    if method != "tools/call":
        if not (method in PASSTHROUGH_METHODS or method.startswith("notifications/")):
            db.insert_audit_log("rpc.refused", actor=submitter, ip_address=client_ip, detail={"method": method})
            log.warning("rpc.refused", extra={"method": method, "actor": submitter})
            return _rpc_error(rpc_id, -32601, f"Method not allowed through escrow: {method}")

        if method in _AUDITED_PASSTHROUGH:
            db.insert_audit_log(
                "rpc.forwarded",
                actor=submitter,
                ip_address=client_ip,
                detail={"method": method, "params": payload.get("params")},
            )
        result = await forward_full(payload, dict(request.headers))
        if result.body is None:
            return Response(status_code=202, headers=result.headers)
        body = result.body
        if method == "tools/list" and settings.hold_mode() == "async":
            body = _with_result_tool(body)
        return JSONResponse(body, headers=result.headers)

    params = payload.get("params")
    if not isinstance(params, dict):
        return _rpc_error(rpc_id, -32602, "Invalid params: expected an object")

    if not isinstance(params.get("name"), str) or not params["name"]:
        return _rpc_error(rpc_id, -32602, "Invalid params: name must be a non-empty string")

    args = params.get("arguments")
    if args is not None and not isinstance(args, dict):
        return _rpc_error(rpc_id, -32602, "Invalid params: arguments must be an object")

    if params["name"] == RESULT_TOOL:
        return get_result(rpc_id, (args or {}).get("action_id"), submitter, getattr(request.state, "role", ""))

    call = process_mcp_call(
        payload,
        forward_headers=dict(request.headers),
        submitted_by=submitter,
        ip_address=client_ip,
    )

    # Wait mode + a client that accepts SSE + a call that will be held:
    # stream heartbeats so the client knows we're alive while a human decides.
    if (
        settings.hold_mode() == "wait"
        and "text/event-stream" in request.headers.get("accept", "")
        and classify_tool_call(params["name"], args)["action"] == "HOLD"
    ):
        meta = params.get("_meta")
        token = meta.get("progressToken") if isinstance(meta, dict) else None
        return StreamingResponse(
            _stream_until_decided(call, token),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    return await call


def _with_result_tool(body: dict) -> dict:
    """Advertise escrow_get_result alongside the upstream's tools (async mode)."""
    result = body.get("result")
    if not isinstance(result, dict) or not isinstance(result.get("tools"), list):
        return body
    tools = [t for t in result["tools"] if not (isinstance(t, dict) and t.get("name") == RESULT_TOOL)]
    return {**body, "result": {**result, "tools": [*tools, RESULT_TOOL_DEFINITION]}}


async def _stream_until_decided(call, progress_token):
    """SSE body for a held call: heartbeats, then the final JSON-RPC response.

    With a progressToken (MCP `_meta.progressToken`) the heartbeats are
    `notifications/progress` messages, which MCP clients can use to reset
    their request timeout.  Without one they are SSE comments that only keep
    proxies and load balancers from closing the connection.  If the client
    disconnects, the held call is cancelled and its action rejected.
    """
    task = asyncio.ensure_future(call)
    beats = 0
    try:
        while True:
            done, _ = await asyncio.wait({task}, timeout=settings.heartbeat_seconds())
            if done:
                break
            beats += 1
            if progress_token is not None:
                note = {
                    "jsonrpc": "2.0",
                    "method": "notifications/progress",
                    "params": {
                        "progressToken": progress_token,
                        "progress": beats,
                        "message": "Waiting for human review",
                    },
                }
                yield f"event: message\ndata: {json.dumps(note)}\n\n"
            else:
                yield ": waiting for human review\n\n"
        yield f"event: message\ndata: {json.dumps(task.result())}\n\n"
    finally:
        if not task.done():
            task.cancel()
