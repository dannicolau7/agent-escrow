from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles

from . import db
from .auth import api_key_middleware
from .config import load_policy
from .forwarder import forward as _forward_to_upstream
from .proxy_engine import process_mcp_call
from .routes_ui import router as ui_router


@asynccontextmanager
async def lifespan(app: FastAPI):
    db.init_db()
    load_policy()
    # PENDING rows from a previous process have no live asyncio.Event —
    # they can never be resolved, so reject them immediately.
    db.reject_stale_pending("Server restarted; action could not be held.")
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
app.middleware("http")(api_key_middleware)
app.include_router(ui_router)

_WEB_OUT = Path(__file__).parent.parent / "web" / "out"
_WEB_DIR = _WEB_OUT if _WEB_OUT.exists() else Path(__file__).parent.parent / "web"
app.mount("/ui", StaticFiles(directory=str(_WEB_DIR), html=True), name="ui")


@app.get("/", include_in_schema=False)
async def root():
    return RedirectResponse(url="/ui/")


@app.get("/health", tags=["meta"])
async def health():
    return {"status": "ok", "pending_calls": db.count_pending()}


@app.post("/mcp")
async def handle_mcp(request: Request):
    try:
        payload = await request.json()
    except Exception:
        return {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "Parse error"}}

    if not isinstance(payload, dict):
        return {"jsonrpc": "2.0", "id": None, "error": {"code": -32600, "message": "Invalid Request: expected an object"}}

    rpc_id = payload.get("id")
    method = payload.get("method")

    # MCP protocol handshake and discovery methods are never escrowed —
    # forward them straight to the upstream (or mock if no upstream is set).
    if method != "tools/call":
        return await _forward_to_upstream(payload, dict(request.headers))

    params = payload.get("params")
    if not isinstance(params, dict):
        return {"jsonrpc": "2.0", "id": rpc_id, "error": {"code": -32602, "message": "Invalid params: expected an object"}}

    args = params.get("arguments")
    if args is not None and not isinstance(args, dict):
        return {"jsonrpc": "2.0", "id": rpc_id, "error": {"code": -32602, "message": "Invalid params: arguments must be an object"}}

    return await process_mcp_call(payload, forward_headers=dict(request.headers))
