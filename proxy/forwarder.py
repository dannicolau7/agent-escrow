from __future__ import annotations

import os
from typing import Any

import httpx

# Injected by tests to avoid real network I/O.
_test_transport: Any = None


async def forward(payload: dict, headers: dict | None = None) -> dict:
    """Forward a JSON-RPC payload to MCP_UPSTREAM_URL, or mock it in dev mode."""
    upstream_url = os.environ.get("MCP_UPSTREAM_URL")
    rpc_id = payload.get("id")

    if not upstream_url:
        tool_name = payload.get("params", {}).get("name", "unknown")
        return {
            "jsonrpc": "2.0",
            "id": rpc_id,
            "result": {"content": [{"type": "text", "text": f"[mock upstream] executed {tool_name}"}]},
        }

    forward_headers: dict = {}
    if headers:
        for key in ("authorization", "x-api-key"):
            if key in headers:
                forward_headers[key] = headers[key]

    client_kwargs: dict = {}
    if _test_transport is not None:
        client_kwargs["transport"] = _test_transport

    try:
        async with httpx.AsyncClient(**client_kwargs) as client:
            resp = await client.post(
                upstream_url, json=payload, headers=forward_headers, timeout=30.0
            )
            resp.raise_for_status()
            return resp.json()
    except httpx.TimeoutException:
        return {
            "jsonrpc": "2.0",
            "id": rpc_id,
            "error": {"code": -32001, "message": "Upstream MCP server timed out."},
        }
    except httpx.HTTPStatusError as exc:
        return {
            "jsonrpc": "2.0",
            "id": rpc_id,
            "error": {"code": -32002, "message": f"Upstream HTTP error: {exc.response.status_code}"},
        }
    except httpx.RequestError as exc:
        return {
            "jsonrpc": "2.0",
            "id": rpc_id,
            "error": {"code": -32003, "message": f"Could not reach upstream MCP server: {exc}"},
        }
