"""A small fake MCP server (Streamable HTTP) for local development and tests.

Run:  python scripts/fake_upstream.py            # http://127.0.0.1:4101/mcp
It answers initialize / tools/list / tools/call over SSE, issues a session id,
and echoes what it received so you can see exactly what the proxy forwarded.
Set FAKE_UPSTREAM_FAIL=1 to make every tools/call return isError.
"""

from __future__ import annotations

import json
import os
import uuid

import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import Response

TOOLS = [
    {"name": "get_user", "description": "Read a user", "inputSchema": {"type": "object"}},
    {"name": "list_orders", "description": "List orders", "inputSchema": {"type": "object"}},
    {"name": "delete_user", "description": "Delete a user", "inputSchema": {"type": "object"}},
    {"name": "transfer_funds", "description": "Move money", "inputSchema": {"type": "object"}},
    {"name": "send_email", "description": "Send an email", "inputSchema": {"type": "object"}},
]

app = FastAPI(title="fake MCP upstream")
# Last request seen, for tests: GET /_last
_last: dict = {}


def _sse(message: dict, session_id: str) -> Response:
    body = f"event: message\ndata: {json.dumps(message)}\n\n"
    return Response(body, media_type="text/event-stream", headers={"mcp-session-id": session_id})


@app.get("/_last")
async def last():
    return _last


@app.post("/mcp")
async def mcp(request: Request):
    payload = await request.json()
    session_id = request.headers.get("mcp-session-id") or f"sess-{uuid.uuid4().hex[:8]}"
    _last.clear()
    _last.update(
        payload=payload,
        authorization=request.headers.get("authorization"),
        x_api_key=request.headers.get("x-api-key"),
        mcp_session_id=request.headers.get("mcp-session-id"),
        accept=request.headers.get("accept"),
    )

    if "id" not in payload:  # notification
        return Response(status_code=202)

    method = payload.get("method")
    rpc_id = payload["id"]
    if method == "initialize":
        result = {
            "protocolVersion": "2025-06-18",
            "capabilities": {"tools": {}},
            "serverInfo": {"name": "fake-upstream", "version": "1.0"},
        }
    elif method == "tools/list":
        result = {"tools": TOOLS}
    elif method == "tools/call":
        name = payload.get("params", {}).get("name")
        args = payload.get("params", {}).get("arguments", {})
        failed = os.environ.get("FAKE_UPSTREAM_FAIL") == "1"
        result = {
            "content": [
                {
                    "type": "text",
                    "text": f"fake upstream {'FAILED' if failed else 'ran'} {name} with {json.dumps(args)}",
                }
            ],
            "isError": failed,
        }
    elif method == "ping":
        result = {}
    else:
        return _sse(
            {"jsonrpc": "2.0", "id": rpc_id, "error": {"code": -32601, "message": f"unknown method {method}"}},
            session_id,
        )
    return _sse({"jsonrpc": "2.0", "id": rpc_id, "result": result}, session_id)


if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=int(os.environ.get("FAKE_UPSTREAM_PORT", "4101")), log_level="warning")
