"""
Tests for proxy/forwarder.py and the /mcp endpoint's input validation.

Covers:
 - Mock mode (no MCP_UPSTREAM_URL) returns a well-formed JSON-RPC result
 - Real upstream forwarding via injectable _test_transport
 - Authorization / X-Api-Key header passthrough
 - Upstream timeout → JSON-RPC error -32001
 - Upstream HTTP error → JSON-RPC error -32002
 - Network unreachable → JSON-RPC error -32003
 - MCP protocol methods (initialize, tools/list) forwarded without escrow
 - Batch requests → -32600 (not a 500)
 - params: null → -32602 (not a 500)
 - arguments that are not an object → -32602 (not a 500)
 - Classifier bypass: get_and_delete_user must be HELD, not auto-approved
 - transfer_funds → HIGH/FINANCIAL, drop_table → CRITICAL/DATABASE
"""
from __future__ import annotations

import asyncio
import os

import httpx
import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

import proxy.forwarder as forwarder_module
from proxy import db, escrow
from proxy.config import classify_tool_call
from proxy.main import app
from proxy.policies import load_policy
from proxy.proxy_engine import EXECUTION_WAITERS

pytestmark = pytest.mark.asyncio


# ---------------------------------------------------------------------------
# Shared fixture
# ---------------------------------------------------------------------------

@pytest_asyncio.fixture
async def client(tmp_path, monkeypatch):
    db.DB_PATH = tmp_path / "test_escrow.db"
    db._conn = None
    escrow._pending.clear()
    EXECUTION_WAITERS.clear()
    db.init_db()
    load_policy()
    monkeypatch.delenv("MCP_UPSTREAM_URL", raising=False)
    forwarder_module._test_transport = None

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac

    EXECUTION_WAITERS.clear()
    db._conn = None
    forwarder_module._test_transport = None


def _tools_call(tool_name: str, args: dict | None = None, rpc_id: int = 1) -> dict:
    return {
        "jsonrpc": "2.0",
        "id": rpc_id,
        "method": "tools/call",
        "params": {"name": tool_name, "arguments": args or {}},
    }


# ---------------------------------------------------------------------------
# 1. Mock mode: no MCP_UPSTREAM_URL → returns a structured mock result
# ---------------------------------------------------------------------------

async def test_mock_mode_auto_approved(client):
    r = await client.post("/mcp", json=_tools_call("get_user"))
    assert r.status_code == 200
    body = r.json()
    assert "result" in body
    assert body["result"]["content"][0]["type"] == "text"
    assert "get_user" in body["result"]["content"][0]["text"]


# ---------------------------------------------------------------------------
# 2. Real upstream via _test_transport
# ---------------------------------------------------------------------------

async def test_real_upstream_forwarding(client, monkeypatch):
    upstream_response = {
        "jsonrpc": "2.0",
        "id": 1,
        "result": {"content": [{"type": "text", "text": "real upstream result"}]},
    }
    mock_transport = httpx.MockTransport(
        handler=lambda req: httpx.Response(200, json=upstream_response)
    )
    monkeypatch.setenv("MCP_UPSTREAM_URL", "http://fake-upstream/mcp")
    forwarder_module._test_transport = mock_transport

    r = await client.post("/mcp", json=_tools_call("get_user"))
    assert r.status_code == 200
    assert r.json()["result"]["content"][0]["text"] == "real upstream result"


# ---------------------------------------------------------------------------
# 3. Header passthrough: Authorization and X-Api-Key forwarded
# ---------------------------------------------------------------------------

async def test_header_passthrough(client, monkeypatch):
    captured: list = []

    def handler(req: httpx.Request) -> httpx.Response:
        captured.append(dict(req.headers))
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "result": {"content": []}})

    monkeypatch.setenv("MCP_UPSTREAM_URL", "http://fake-upstream/mcp")
    forwarder_module._test_transport = httpx.MockTransport(handler=handler)

    await client.post(
        "/mcp",
        json=_tools_call("get_user"),
        headers={"Authorization": "Bearer token123", "X-Api-Key": "key456"},
    )
    assert captured, "handler never called"
    assert captured[0].get("authorization") == "Bearer token123"
    assert captured[0].get("x-api-key") == "key456"


# ---------------------------------------------------------------------------
# 4. Upstream timeout → -32001
# ---------------------------------------------------------------------------

async def test_upstream_timeout(monkeypatch):
    async def raise_timeout(*_a, **_kw):
        raise httpx.TimeoutException("timed out")

    monkeypatch.setenv("MCP_UPSTREAM_URL", "http://fake-upstream/mcp")

    class TimeoutTransport(httpx.AsyncBaseTransport):
        async def handle_async_request(self, req):
            raise httpx.TimeoutException("timed out", request=req)

    forwarder_module._test_transport = TimeoutTransport()
    from proxy.forwarder import forward
    result = await forward({"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {}})
    assert result["error"]["code"] == -32001
    forwarder_module._test_transport = None


# ---------------------------------------------------------------------------
# 5. Upstream HTTP error → -32002
# ---------------------------------------------------------------------------

async def test_upstream_http_error(monkeypatch):
    monkeypatch.setenv("MCP_UPSTREAM_URL", "http://fake-upstream/mcp")
    forwarder_module._test_transport = httpx.MockTransport(
        handler=lambda req: httpx.Response(503)
    )
    from proxy.forwarder import forward
    result = await forward({"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {}})
    assert result["error"]["code"] == -32002
    assert "503" in result["error"]["message"]
    forwarder_module._test_transport = None


# ---------------------------------------------------------------------------
# 6. Network unreachable → -32003
# ---------------------------------------------------------------------------

async def test_upstream_network_error(monkeypatch):
    monkeypatch.setenv("MCP_UPSTREAM_URL", "http://fake-upstream/mcp")

    class NetworkErrorTransport(httpx.AsyncBaseTransport):
        async def handle_async_request(self, req):
            raise httpx.ConnectError("connection refused", request=req)

    forwarder_module._test_transport = NetworkErrorTransport()
    from proxy.forwarder import forward
    result = await forward({"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {}})
    assert result["error"]["code"] == -32003
    forwarder_module._test_transport = None


# ---------------------------------------------------------------------------
# 7. MCP protocol methods are forwarded without escrow
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("method", ["initialize", "tools/list", "ping", "resources/list"])
async def test_protocol_methods_not_escrowed(client, method):
    payload = {"jsonrpc": "2.0", "id": 1, "method": method, "params": {}}
    r = await client.post("/mcp", json=payload)
    # In mock mode (no upstream), non-tools/call methods return a mock result
    assert r.status_code == 200
    body = r.json()
    # Should never be held — pending queue must stay empty
    pending = (await client.get("/api/pending")).json()
    assert pending == [], f"{method} ended up in escrow"


# ---------------------------------------------------------------------------
# 8. Batch request (array) → -32600, not 500
# ---------------------------------------------------------------------------

async def test_batch_request_returns_32600(client):
    r = await client.post("/mcp", content=b'[{"jsonrpc":"2.0","id":1,"method":"tools/call"}]',
                          headers={"content-type": "application/json"})
    assert r.status_code == 200
    body = r.json()
    assert body["error"]["code"] == -32600


# ---------------------------------------------------------------------------
# 9. params: null → -32602, not 500
# ---------------------------------------------------------------------------

async def test_null_params_returns_32602(client):
    r = await client.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": None})
    assert r.status_code == 200
    body = r.json()
    assert body["error"]["code"] == -32602


# ---------------------------------------------------------------------------
# 10. arguments not an object → -32602, not 500
# ---------------------------------------------------------------------------

async def test_non_object_arguments_returns_32602(client):
    r = await client.post("/mcp", json={
        "jsonrpc": "2.0", "id": 1, "method": "tools/call",
        "params": {"name": "delete_record", "arguments": "not_an_object"},
    })
    assert r.status_code == 200
    body = r.json()
    assert body["error"]["code"] == -32602


# ---------------------------------------------------------------------------
# 11. Classifier bypass: get_and_delete_user must be HELD
# ---------------------------------------------------------------------------

def test_classifier_no_bypass_via_safe_prefix():
    result = classify_tool_call("get_and_delete_user")
    assert result["action"] == "HOLD", "Safe prefix must not override a mutation pattern"
    assert result["risk_level"] == "CRITICAL"


# ---------------------------------------------------------------------------
# 12. transfer_funds → HIGH/FINANCIAL
# ---------------------------------------------------------------------------

def test_transfer_funds_is_high_financial():
    result = classify_tool_call("transfer_funds")
    assert result["action"] == "HOLD"
    assert result["risk_level"] == "HIGH"
    assert result["category"] == "FINANCIAL"


# ---------------------------------------------------------------------------
# 13. drop_table → CRITICAL/DATABASE
# ---------------------------------------------------------------------------

def test_drop_table_is_critical():
    result = classify_tool_call("drop_table")
    assert result["action"] == "HOLD"
    assert result["risk_level"] == "CRITICAL"
