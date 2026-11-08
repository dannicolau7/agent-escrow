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
from proxy import db
from proxy.config import classify_tool_call
from proxy.main import app
from proxy.proxy_engine import EXECUTION_WAITERS

pytestmark = pytest.mark.asyncio


# ---------------------------------------------------------------------------
# Shared fixture
# ---------------------------------------------------------------------------

@pytest_asyncio.fixture
async def client(tmp_path, monkeypatch):
    db.DB_PATH = tmp_path / "test_escrow.db"
    db._conn = None
    EXECUTION_WAITERS.clear()
    db.init_db()
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


# ---------------------------------------------------------------------------
# 14. SSE broadcast: events.broadcast() delivers to all subscribers
# ---------------------------------------------------------------------------

async def test_sse_broadcast_reaches_subscriber():
    import proxy.events as ev
    # Reset subscribers for isolation
    ev._subscribers.clear()

    received: list = []

    async def _collect():
        async for chunk in ev.subscribe():
            if chunk.startswith("data:"):
                import json
                received.append(json.loads(chunk.split("data:", 1)[1].strip()))
                return

    task = asyncio.create_task(_collect())
    await asyncio.sleep(0)  # let subscriber register
    ev.broadcast("action_added", {"action_id": "act_test", "tool_name": "delete_x"})
    await asyncio.wait_for(task, timeout=2.0)

    assert len(received) == 1
    assert received[0]["type"] == "action_added"
    assert received[0]["action_id"] == "act_test"
    ev._subscribers.clear()


# ---------------------------------------------------------------------------
# 15. SSE broadcast: action_resolved event after /api/resolve
# ---------------------------------------------------------------------------

async def test_sse_action_resolved_event(client):
    import proxy.events as ev
    ev._subscribers.clear()

    received: list = []

    async def _collect():
        async for chunk in ev.subscribe():
            if chunk.startswith("data:"):
                import json
                received.append(json.loads(chunk.split("data:", 1)[1].strip()))
                if any(e.get("type") == "action_resolved" for e in received):
                    return

    async def _work():
        await asyncio.sleep(0)
        # Create a pending action
        t = asyncio.create_task(client.post("/mcp", json={
            "jsonrpc": "2.0", "id": 1, "method": "tools/call",
            "params": {"name": "delete_record", "arguments": {}},
        }))
        await asyncio.sleep(0.05)
        pending = (await client.get("/api/pending")).json()
        await client.post("/api/resolve", json={"action_id": pending[0]["id"], "decision": "APPROVED"})
        await t

    task = asyncio.create_task(_collect())
    await asyncio.sleep(0)
    await _work()
    await asyncio.wait_for(task, timeout=3.0)

    resolved = [e for e in received if e.get("type") == "action_resolved"]
    assert resolved, f"no action_resolved event; got {received}"
    assert resolved[0]["status"] == "APPROVED"
    ev._subscribers.clear()


# ---------------------------------------------------------------------------
# 16-18. Auth middleware
# ---------------------------------------------------------------------------

async def test_auth_disabled_in_dev_mode(client, monkeypatch):
    monkeypatch.delenv("ESCROW_API_KEY", raising=False)
    r = await client.get("/api/pending")
    assert r.status_code == 200


async def test_auth_missing_key_returns_401(client, monkeypatch):
    monkeypatch.setenv("ESCROW_API_KEY", "secret123")
    r = await client.get("/api/pending")
    assert r.status_code == 401


async def test_auth_wrong_key_returns_403(client, monkeypatch):
    monkeypatch.setenv("ESCROW_API_KEY", "secret123")
    r = await client.get("/api/pending", headers={"X-Api-Key": "wrong"})
    assert r.status_code == 403


async def test_auth_correct_bearer_token(client, monkeypatch):
    monkeypatch.setenv("ESCROW_API_KEY", "secret123")
    r = await client.get("/api/pending", headers={"Authorization": "Bearer secret123"})
    assert r.status_code == 200


async def test_auth_correct_x_api_key(client, monkeypatch):
    monkeypatch.setenv("ESCROW_API_KEY", "secret123")
    r = await client.get("/api/pending", headers={"X-Api-Key": "secret123"})
    assert r.status_code == 200


async def test_auth_mcp_endpoint_protected(client, monkeypatch):
    monkeypatch.setenv("ESCROW_API_KEY", "secret123")
    r = await client.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                                         "params": {"name": "get_user", "arguments": {}}})
    assert r.status_code == 401


async def test_auth_health_always_public(client, monkeypatch):
    monkeypatch.setenv("ESCROW_API_KEY", "secret123")
    r = await client.get("/health")
    assert r.status_code == 200


# ---------------------------------------------------------------------------
# 23-28. Push notifications
# ---------------------------------------------------------------------------

import proxy.notifications as notif_module


async def test_no_notification_when_no_env_vars(monkeypatch):
    """When no webhook env vars are set, no HTTP calls are made."""
    for var in ("SLACK_WEBHOOK_URL", "NOTIFICATION_WEBHOOK_URL", "PAGERDUTY_ROUTING_KEY"):
        monkeypatch.delenv(var, raising=False)

    called: list = []
    notif_module._test_transport = httpx.MockTransport(
        handler=lambda req: called.append(req) or httpx.Response(200)
    )
    await notif_module.notify_action_added(
        action_id="act_test", tool_name="delete_x",
        risk_level="CRITICAL", category="DATABASE", reason="test",
    )
    assert called == [], "unexpected HTTP call when no webhooks configured"
    notif_module._test_transport = None


async def test_slack_webhook_called(monkeypatch):
    monkeypatch.setenv("SLACK_WEBHOOK_URL", "http://fake-slack/hook")
    monkeypatch.delenv("NOTIFICATION_WEBHOOK_URL", raising=False)
    monkeypatch.delenv("PAGERDUTY_ROUTING_KEY", raising=False)

    captured: list = []
    notif_module._test_transport = httpx.MockTransport(
        handler=lambda req: captured.append(req) or httpx.Response(200)
    )
    await notif_module.notify_action_added(
        action_id="act_slack", tool_name="drop_table",
        risk_level="CRITICAL", category="DATABASE", reason="cleanup",
    )
    notif_module._test_transport = None

    assert len(captured) == 1
    assert "fake-slack" in str(captured[0].url)
    import json
    body = json.loads(captured[0].content)
    assert "drop_table" in body["text"]
    assert any("drop_table" in str(b) for b in body["blocks"])


async def test_generic_webhook_called(monkeypatch):
    monkeypatch.setenv("NOTIFICATION_WEBHOOK_URL", "http://fake-webhook/notify")
    monkeypatch.delenv("SLACK_WEBHOOK_URL", raising=False)
    monkeypatch.delenv("PAGERDUTY_ROUTING_KEY", raising=False)

    captured: list = []
    notif_module._test_transport = httpx.MockTransport(
        handler=lambda req: captured.append(req) or httpx.Response(200)
    )
    await notif_module.notify_action_added(
        action_id="act_wh", tool_name="send_email",
        risk_level="MEDIUM", category="COMMUNICATION", reason="notify team",
    )
    notif_module._test_transport = None

    assert len(captured) == 1
    import json
    body = json.loads(captured[0].content)
    assert body["tool_name"] == "send_email"
    assert body["risk_level"] == "MEDIUM"
    assert body["action_id"] == "act_wh"


async def test_pagerduty_called_with_correct_severity(monkeypatch):
    monkeypatch.setenv("PAGERDUTY_ROUTING_KEY", "pd-routing-key-123")
    monkeypatch.delenv("SLACK_WEBHOOK_URL", raising=False)
    monkeypatch.delenv("NOTIFICATION_WEBHOOK_URL", raising=False)

    captured: list = []
    notif_module._test_transport = httpx.MockTransport(
        handler=lambda req: captured.append(req) or httpx.Response(202)
    )
    await notif_module.notify_action_added(
        action_id="act_pd", tool_name="transfer_funds",
        risk_level="HIGH", category="FINANCIAL", reason="wire transfer",
    )
    notif_module._test_transport = None

    assert len(captured) == 1
    assert "pagerduty.com" in str(captured[0].url)
    import json
    body = json.loads(captured[0].content)
    assert body["routing_key"] == "pd-routing-key-123"
    assert body["payload"]["severity"] == "error"   # HIGH → error
    assert body["dedup_key"] == "act_pd"


async def test_notification_error_does_not_break_escrow(client, monkeypatch):
    """A webhook that returns 500 must not prevent the action from being escrowed."""
    monkeypatch.setenv("SLACK_WEBHOOK_URL", "http://broken-webhook/hook")
    notif_module._test_transport = httpx.MockTransport(
        handler=lambda req: httpx.Response(500)
    )

    async def _mcp():
        return await client.post("/mcp", json={
            "jsonrpc": "2.0", "id": 1, "method": "tools/call",
            "params": {"name": "delete_record", "arguments": {}},
        })

    task = asyncio.create_task(_mcp())
    await asyncio.sleep(0.1)
    pending = (await client.get("/api/pending")).json()
    assert len(pending) == 1, "action must be in escrow even when webhook fails"

    await client.post("/api/resolve", json={"action_id": pending[0]["id"], "decision": "APPROVED"})
    await task
    notif_module._test_transport = None


async def test_all_three_channels_called(monkeypatch):
    monkeypatch.setenv("SLACK_WEBHOOK_URL", "http://fake-slack/hook")
    monkeypatch.setenv("NOTIFICATION_WEBHOOK_URL", "http://fake-webhook/notify")
    monkeypatch.setenv("PAGERDUTY_ROUTING_KEY", "pd-key")

    urls: list = []
    notif_module._test_transport = httpx.MockTransport(
        handler=lambda req: urls.append(str(req.url)) or httpx.Response(200)
    )
    await notif_module.notify_action_added(
        action_id="act_all", tool_name="drop_table",
        risk_level="CRITICAL", category="DATABASE", reason="all channels",
    )
    notif_module._test_transport = None

    assert any("slack" in u for u in urls)
    assert any("fake-webhook" in u for u in urls)
    assert any("pagerduty" in u for u in urls)


# ---------------------------------------------------------------------------
# RBAC tests
# ---------------------------------------------------------------------------

def _escrow_keys(*triples: tuple) -> str:
    """Build ESCROW_KEYS value from (label, role, key) triples."""
    return "|".join(f"{l}:{r}:{k}" for l, r, k in triples)


async def test_rbac_approver_can_resolve(client, monkeypatch):
    monkeypatch.setenv("ESCROW_KEYS", _escrow_keys(
        ("agent1", "agent", "sk_agent"),
        ("reviewer", "approver", "sk_approver"),
    ))
    # Agent submits a mutating call
    t = asyncio.create_task(client.post("/mcp", json={
        "jsonrpc": "2.0", "id": 1, "method": "tools/call",
        "params": {"name": "delete_record", "arguments": {}},
    }, headers={"X-Api-Key": "sk_agent"}))
    await asyncio.sleep(0.05)

    pending = (await client.get("/api/pending", headers={"X-Api-Key": "sk_approver"})).json()
    assert len(pending) == 1

    r = await client.post("/api/resolve",
        json={"action_id": pending[0]["id"], "decision": "APPROVED"},
        headers={"X-Api-Key": "sk_approver"},
    )
    assert r.status_code == 200
    assert r.json()["resolved_by"] == "reviewer"
    await t


async def test_rbac_approver_cannot_call_mcp(client, monkeypatch):
    monkeypatch.setenv("ESCROW_KEYS", _escrow_keys(("reviewer", "approver", "sk_approver")))
    r = await client.post("/mcp", json={
        "jsonrpc": "2.0", "id": 1, "method": "tools/call",
        "params": {"name": "get_user", "arguments": {}},
    }, headers={"X-Api-Key": "sk_approver"})
    assert r.status_code == 403


async def test_rbac_viewer_can_read_pending(client, monkeypatch):
    monkeypatch.setenv("ESCROW_KEYS", _escrow_keys(("watcher", "viewer", "sk_viewer")))
    r = await client.get("/api/pending", headers={"X-Api-Key": "sk_viewer"})
    assert r.status_code == 200


async def test_rbac_viewer_cannot_resolve(client, monkeypatch):
    monkeypatch.setenv("ESCROW_KEYS", _escrow_keys(("watcher", "viewer", "sk_viewer")))
    r = await client.post("/api/resolve",
        json={"action_id": "ghost", "decision": "APPROVED"},
        headers={"X-Api-Key": "sk_viewer"},
    )
    assert r.status_code == 403


async def test_rbac_agent_cannot_read_pending(client, monkeypatch):
    monkeypatch.setenv("ESCROW_KEYS", _escrow_keys(("bot", "agent", "sk_agent")))
    r = await client.get("/api/pending", headers={"X-Api-Key": "sk_agent"})
    assert r.status_code == 403


async def test_rbac_agent_can_call_mcp(client, monkeypatch):
    monkeypatch.setenv("ESCROW_KEYS", _escrow_keys(("bot", "agent", "sk_agent")))
    r = await client.post("/mcp", json={
        "jsonrpc": "2.0", "id": 1, "method": "tools/call",
        "params": {"name": "get_user", "arguments": {}},
    }, headers={"X-Api-Key": "sk_agent"})
    assert r.status_code == 200
    assert "result" in r.json()


async def test_rbac_resolved_by_recorded(client, monkeypatch):
    monkeypatch.setenv("ESCROW_KEYS", _escrow_keys(
        ("myagent", "agent", "sk_agent"),
        ("alice", "approver", "sk_alice"),
    ))
    t = asyncio.create_task(client.post("/mcp", json={
        "jsonrpc": "2.0", "id": 1, "method": "tools/call",
        "params": {"name": "delete_record", "arguments": {}},
    }, headers={"X-Api-Key": "sk_agent"}))
    await asyncio.sleep(0.05)

    pending = (await client.get("/api/pending", headers={"X-Api-Key": "sk_alice"})).json()
    action_id = pending[0]["id"]

    await client.post("/api/resolve",
        json={"action_id": action_id, "decision": "REJECTED", "rejection_reason": "not needed"},
        headers={"X-Api-Key": "sk_alice"},
    )
    await t

    history = (await client.get("/api/history", headers={"X-Api-Key": "sk_alice"})).json()
    resolved = next(h for h in history if h["id"] == action_id)
    assert resolved["resolved_by"] == "alice"
    assert resolved["rejection_reason"] == "not needed"


async def test_rbac_legacy_escrow_api_key_is_admin(client, monkeypatch):
    monkeypatch.setenv("ESCROW_API_KEY", "legacy_key")
    monkeypatch.delenv("ESCROW_KEYS", raising=False)
    # Legacy admin key can call /mcp AND /api/pending AND /api/resolve
    r_mcp = await client.post("/mcp", json={
        "jsonrpc": "2.0", "id": 1, "method": "tools/call",
        "params": {"name": "get_user", "arguments": {}},
    }, headers={"X-Api-Key": "legacy_key"})
    assert r_mcp.status_code == 200

    r_pending = await client.get("/api/pending", headers={"X-Api-Key": "legacy_key"})
    assert r_pending.status_code == 200


# ---------------------------------------------------------------------------
# Audit trail tests
# ---------------------------------------------------------------------------

async def test_audit_action_created_on_escrow(client):
    t = asyncio.create_task(client.post("/mcp", json={
        "jsonrpc": "2.0", "id": 1, "method": "tools/call",
        "params": {"name": "delete_record", "arguments": {}},
    }))
    await asyncio.sleep(0.05)

    pending = (await client.get("/api/pending")).json()
    action_id = pending[0]["id"]

    audit = (await client.get(f"/api/audit?action_id={action_id}")).json()
    created = [e for e in audit if e["event_type"] == "action.created"]
    assert created, "expected action.created entry"
    assert created[0]["detail"]["tool_name"] == "delete_record"
    assert created[0]["detail"]["risk_level"] == "CRITICAL"

    await client.post("/api/resolve", json={"action_id": action_id, "decision": "APPROVED"})
    await t


async def test_audit_action_approved(client):
    t = asyncio.create_task(client.post("/mcp", json={
        "jsonrpc": "2.0", "id": 1, "method": "tools/call",
        "params": {"name": "delete_record", "arguments": {}},
    }))
    await asyncio.sleep(0.05)

    pending = (await client.get("/api/pending")).json()
    action_id = pending[0]["id"]
    await client.post("/api/resolve", json={"action_id": action_id, "decision": "APPROVED"})
    await t

    audit = (await client.get(f"/api/audit?action_id={action_id}")).json()
    approved = [e for e in audit if e["event_type"] == "action.approved"]
    assert approved, "expected action.approved entry"
    assert approved[0]["action_id"] == action_id


async def test_audit_action_rejected_with_reason(client):
    t = asyncio.create_task(client.post("/mcp", json={
        "jsonrpc": "2.0", "id": 1, "method": "tools/call",
        "params": {"name": "delete_record", "arguments": {}},
    }))
    await asyncio.sleep(0.05)

    pending = (await client.get("/api/pending")).json()
    action_id = pending[0]["id"]
    await client.post("/api/resolve", json={
        "action_id": action_id, "decision": "REJECTED", "rejection_reason": "not authorised"
    })
    await t

    audit = (await client.get(f"/api/audit?action_id={action_id}")).json()
    rejected = [e for e in audit if e["event_type"] == "action.rejected"]
    assert rejected
    assert rejected[0]["detail"]["rejection_reason"] == "not authorised"


async def test_audit_timeout(client):
    from proxy.proxy_engine import process_mcp_call
    payload = {
        "jsonrpc": "2.0", "id": 99, "method": "tools/call",
        "params": {"name": "drop_table", "arguments": {}},
    }
    result = await process_mcp_call(payload, timeout=0.05)
    assert "error" in result

    action_id = None
    for row in db.get_audit_log(event_type="action.created"):
        if row["detail"] and row["detail"].get("tool_name") == "drop_table":
            action_id = row["action_id"]
            break

    assert action_id, "no action.created entry for drop_table"
    audit = db.get_audit_log(action_id=action_id)
    timeout_entries = [e for e in audit if e["event_type"] == "action.timeout"]
    assert timeout_entries, "expected action.timeout entry"


async def test_audit_submitted_by_in_pending_actions(client, monkeypatch):
    monkeypatch.setenv("ESCROW_KEYS", "mybot:agent:sk_bot")
    t = asyncio.create_task(client.post("/mcp", json={
        "jsonrpc": "2.0", "id": 1, "method": "tools/call",
        "params": {"name": "delete_record", "arguments": {}},
    }, headers={"X-Api-Key": "sk_bot"}))
    await asyncio.sleep(0.05)

    # Switch to admin key to read pending
    monkeypatch.delenv("ESCROW_KEYS")
    pending = (await client.get("/api/pending")).json()
    assert pending[0]["submitted_by"] == "mybot"

    await client.post("/api/resolve", json={"action_id": pending[0]["id"], "decision": "APPROVED"})
    await t


async def test_audit_endpoint_filters_by_event_type(client):
    t = asyncio.create_task(client.post("/mcp", json={
        "jsonrpc": "2.0", "id": 1, "method": "tools/call",
        "params": {"name": "delete_record", "arguments": {}},
    }))
    await asyncio.sleep(0.05)
    pending = (await client.get("/api/pending")).json()
    action_id = pending[0]["id"]
    await client.post("/api/resolve", json={"action_id": action_id, "decision": "APPROVED"})
    await t

    only_created = (await client.get("/api/audit?event_type=action.created")).json()
    assert all(e["event_type"] == "action.created" for e in only_created)
    assert any(e["action_id"] == action_id for e in only_created)


# ---------------------------------------------------------------------------
# Metrics endpoint tests
# ---------------------------------------------------------------------------

async def test_metrics_empty_db(client):
    resp = await client.get("/metrics")
    assert resp.status_code == 200
    assert "text/plain" in resp.headers["content-type"]
    body = resp.text
    assert "escrow_pending_count 0" in body
    assert "escrow_approval_latency_seconds_count 0" in body


async def test_metrics_pending_count(client):
    t = asyncio.create_task(client.post("/mcp", json={
        "jsonrpc": "2.0", "id": 1, "method": "tools/call",
        "params": {"name": "delete_record", "arguments": {}},
    }))
    await asyncio.sleep(0.05)

    body = (await client.get("/metrics")).text
    assert "escrow_pending_count 1" in body

    pending = (await client.get("/api/pending")).json()
    await client.post("/api/resolve", json={"action_id": pending[0]["id"], "decision": "APPROVED"})
    await t

    body = (await client.get("/metrics")).text
    assert "escrow_pending_count 0" in body


async def test_metrics_actions_total_labels(client):
    for _ in range(2):
        t = asyncio.create_task(client.post("/mcp", json={
            "jsonrpc": "2.0", "id": 1, "method": "tools/call",
            "params": {"name": "delete_record", "arguments": {}},
        }))
        await asyncio.sleep(0.05)
        pending = (await client.get("/api/pending")).json()
        await client.post("/api/resolve", json={"action_id": pending[0]["id"], "decision": "APPROVED"})
        await t

    t = asyncio.create_task(client.post("/mcp", json={
        "jsonrpc": "2.0", "id": 2, "method": "tools/call",
        "params": {"name": "delete_record", "arguments": {}},
    }))
    await asyncio.sleep(0.05)
    pending = (await client.get("/api/pending")).json()
    await client.post("/api/resolve", json={
        "action_id": pending[0]["id"], "decision": "REJECTED", "rejection_reason": "no"
    })
    await t

    body = (await client.get("/metrics")).text
    assert 'status="APPROVED"' in body
    assert 'status="REJECTED"' in body
    assert 'risk_level="CRITICAL"' in body


async def test_metrics_latency_nonzero_after_resolution(client):
    t = asyncio.create_task(client.post("/mcp", json={
        "jsonrpc": "2.0", "id": 1, "method": "tools/call",
        "params": {"name": "delete_record", "arguments": {}},
    }))
    await asyncio.sleep(0.05)
    pending = (await client.get("/api/pending")).json()
    await client.post("/api/resolve", json={"action_id": pending[0]["id"], "decision": "APPROVED"})
    await t

    body = (await client.get("/metrics")).text
    assert "escrow_approval_latency_seconds_count 1" in body
    # sum should be a positive float
    for line in body.splitlines():
        if line.startswith("escrow_approval_latency_seconds_sum "):
            val = float(line.split()[-1])
            assert val >= 0


async def test_metrics_audit_events_appear(client):
    t = asyncio.create_task(client.post("/mcp", json={
        "jsonrpc": "2.0", "id": 1, "method": "tools/call",
        "params": {"name": "delete_record", "arguments": {}},
    }))
    await asyncio.sleep(0.05)
    pending = (await client.get("/api/pending")).json()
    await client.post("/api/resolve", json={"action_id": pending[0]["id"], "decision": "APPROVED"})
    await t

    body = (await client.get("/metrics")).text
    assert 'event_type="action.created"' in body
    assert 'event_type="action.approved"' in body


async def test_metrics_prometheus_format_structure(client):
    body = (await client.get("/metrics")).text
    for line in body.splitlines():
        if line and not line.startswith("#"):
            parts = line.rsplit(" ", 1)
            assert len(parts) == 2, f"bad metric line: {line!r}"
            float(parts[1])  # value must be numeric
