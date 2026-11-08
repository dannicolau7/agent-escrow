from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles

from . import db, logs, settings
from .auth import api_key_middleware, require_agent
from .config import load_policy
from .events import broadcast
from .forwarder import forward_full
from .logs import log
from .proxy_engine import process_mcp_call
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
    log.info("server.started", extra={"mode": settings.mode(), "stale_purged": len(stale)})
    yield


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
        return JSONResponse(result.body, headers=result.headers)

    params = payload.get("params")
    if not isinstance(params, dict):
        return _rpc_error(rpc_id, -32602, "Invalid params: expected an object")

    if not isinstance(params.get("name"), str) or not params["name"]:
        return _rpc_error(rpc_id, -32602, "Invalid params: name must be a non-empty string")

    args = params.get("arguments")
    if args is not None and not isinstance(args, dict):
        return _rpc_error(rpc_id, -32602, "Invalid params: arguments must be an object")

    return await process_mcp_call(
        payload,
        forward_headers=dict(request.headers),
        submitted_by=submitter,
        ip_address=client_ip,
    )
