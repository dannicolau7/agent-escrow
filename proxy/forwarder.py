"""Forward JSON-RPC messages to the upstream MCP server (Streamable HTTP).

- Sends `Accept: application/json, text/event-stream` as the MCP spec requires
  and understands both plain JSON and SSE-framed responses.
- Passes the MCP session headers (`Mcp-Session-Id`, `Mcp-Protocol-Version`)
  through in both directions so sessions created by `initialize` keep working.
- Never forwards the caller's own credentials (their escrow key).  Upstream
  auth comes from MCP_UPSTREAM_TOKEN instead.
- Without MCP_UPSTREAM_URL it returns a mock result, but only in dev mode.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from typing import Any, Dict, Optional

import httpx

from . import settings

# Injected by tests to avoid real network I/O.
_test_transport: Any = None

# Request headers copied from the agent to the upstream (lower-case).
_PASSTHROUGH_REQUEST_HEADERS = ("mcp-session-id", "mcp-protocol-version", "last-event-id")
# Response headers copied from the upstream back to the agent.
_PASSTHROUGH_RESPONSE_HEADERS = ("mcp-session-id",)


@dataclass
class ForwardResult:
    body: Optional[dict]                      # None for 202/notification acks
    status_code: int = 200
    headers: Dict[str, str] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return self.body is not None and "error" not in self.body


def _error(rpc_id: Any, code: int, message: str) -> ForwardResult:
    return ForwardResult({"jsonrpc": "2.0", "id": rpc_id, "error": {"code": code, "message": message}})


def _parse_sse(text: str, rpc_id: Any) -> Optional[dict]:
    """Return the JSON-RPC response for `rpc_id` from an SSE body.

    The stream may carry server→client requests/notifications before the
    response; we pick the message whose id matches (or the last response).
    """
    messages: list[dict] = []
    data_lines: list[str] = []
    for line in text.splitlines() + [""]:
        if line.startswith("data:"):
            data_lines.append(line[5:].lstrip())
        elif line == "" and data_lines:
            try:
                msg = json.loads("\n".join(data_lines))
                if isinstance(msg, dict):
                    messages.append(msg)
            except json.JSONDecodeError:
                pass
            data_lines = []
    responses = [m for m in messages if "result" in m or "error" in m]
    for m in responses:
        if m.get("id") == rpc_id:
            return m
    return responses[-1] if responses else None


async def forward_full(payload: dict, headers: dict | None = None) -> ForwardResult:
    upstream_url = os.environ.get("MCP_UPSTREAM_URL")
    rpc_id = payload.get("id")
    is_notification = "id" not in payload

    if not upstream_url:
        if not settings.is_dev():
            return _error(rpc_id, -32003, "No upstream MCP server configured (MCP_UPSTREAM_URL).")
        if is_notification:
            return ForwardResult(None, status_code=202)
        params = payload.get("params") or {}
        tool_name = params.get("name", "unknown") if isinstance(params, dict) else "unknown"
        return ForwardResult(
            {
                "jsonrpc": "2.0",
                "id": rpc_id,
                "result": {"content": [{"type": "text", "text": f"[mock upstream] executed {tool_name}"}]},
            }
        )

    lower = {k.lower(): v for k, v in (headers or {}).items()}
    out_headers: dict = {
        "accept": "application/json, text/event-stream",
        "content-type": "application/json",
    }
    for key in _PASSTHROUGH_REQUEST_HEADERS:
        if key in lower:
            out_headers[key] = lower[key]
    token = os.environ.get("MCP_UPSTREAM_TOKEN")
    if token:
        out_headers["authorization"] = f"Bearer {token}"

    client_kwargs: dict = {}
    if _test_transport is not None:
        client_kwargs["transport"] = _test_transport

    try:
        timeout = float(os.environ.get("MCP_UPSTREAM_TIMEOUT", "30"))
    except ValueError:
        timeout = 30.0

    try:
        async with httpx.AsyncClient(**client_kwargs) as client:
            resp = await client.post(upstream_url, json=payload, headers=out_headers, timeout=timeout)
            resp.raise_for_status()
            back = {k: resp.headers[k] for k in _PASSTHROUGH_RESPONSE_HEADERS if k in resp.headers}

            if resp.status_code == 202 or not resp.content:
                return ForwardResult(None, status_code=202, headers=back)

            ctype = resp.headers.get("content-type", "")
            if "text/event-stream" in ctype:
                body = _parse_sse(resp.text, rpc_id)
            else:
                body = resp.json()
            if not isinstance(body, dict):
                return _error(rpc_id, -32004, "Upstream MCP server returned an invalid response.")
            return ForwardResult(body, headers=back)
    except httpx.TimeoutException:
        return _error(rpc_id, -32001, "Upstream MCP server timed out.")
    except httpx.HTTPStatusError as exc:
        return _error(rpc_id, -32002, f"Upstream HTTP error: {exc.response.status_code}")
    except httpx.RequestError as exc:
        return _error(rpc_id, -32003, f"Could not reach upstream MCP server: {exc}")
    except ValueError:
        return _error(rpc_id, -32004, "Upstream MCP server returned an invalid response.")


async def forward(payload: dict, headers: dict | None = None) -> dict:
    """Body-only wrapper kept for existing callers and tests."""
    result = await forward_full(payload, headers)
    return result.body if result.body is not None else {"jsonrpc": "2.0", "id": payload.get("id"), "result": {}}
