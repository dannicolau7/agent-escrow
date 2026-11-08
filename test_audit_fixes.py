"""Regression tests for the issues found in the security audit."""
from __future__ import annotations

import asyncio

import httpx
import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

import proxy.forwarder as forwarder_module
import proxy.notifications as notif_module
from proxy import db, proxy_engine, settings
from proxy.config import classify_tool_call
from proxy.main import app
from proxy.proxy_engine import EXECUTION_WAITERS, process_mcp_call



@pytest_asyncio.fixture
async def client(tmp_path, monkeypatch):
    db.DB_PATH = tmp_path / "audit_fixes.db"
    db._conn = None
    EXECUTION_WAITERS.clear()
    db.init_db()
    forwarder_module._test_transport = None
    notif_module._test_transport = None
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        yield ac
    EXECUTION_WAITERS.clear()
    forwarder_module._test_transport = None
    notif_module._test_transport = None
    db._conn = None


def _call(name, args=None, rpc_id=1):
    return {"jsonrpc": "2.0", "id": rpc_id, "method": "tools/call", "params": {"name": name, "arguments": args or {}}}


async def _wait_for_pending(client, n=1, headers=None):
    for _ in range(100):
        rows = (await client.get("/api/pending", headers=headers or {})).json()
        if len(rows) >= n:
            return rows
        await asyncio.sleep(0.01)
    raise AssertionError("action never appeared in the queue")


# --- 1. Approval during slow notifications is not lost --------------------------

async def test_approval_during_slow_notification_executes(client, monkeypatch):
    async def slow_notify(**kw):
        await asyncio.sleep(1.0)
    monkeypatch.setattr(proxy_engine, "notify_action_added", slow_notify)

    task = asyncio.create_task(process_mcp_call(_call("delete_user", {"id": 1}, rpc_id=7), timeout=3.0))
    rows = await _wait_for_pending(client)
    r = await client.post("/api/resolve", json={"action_id": rows[0]["id"], "decision": "APPROVED"})
    assert r.status_code == 200

    result = await asyncio.wait_for(task, timeout=2.0)   # must not wait for the notification
    assert "result" in result, result
    row = db.get_action(rows[0]["id"])
    assert row["status"] == "APPROVED"
    assert row["execution_status"] == "SUCCEEDED"


# --- 2. Agent's escrow key is not forwarded (see also test_mcp_forwarding) -------

async def test_no_client_credentials_forwarded_without_upstream_token(client, monkeypatch):
    seen = {}
    def handler(req):
        seen.update(req.headers)
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "result": {}})
    monkeypatch.setenv("MCP_UPSTREAM_URL", "http://up/mcp")
    forwarder_module._test_transport = httpx.MockTransport(handler)
    await client.post("/mcp", json=_call("get_user"), headers={"Authorization": "Bearer agent-secret"})
    assert "authorization" not in seen


# --- 3. Unknown methods are refused and audited ----------------------------------

async def test_unknown_method_refused(client, monkeypatch):
    called = []
    monkeypatch.setenv("MCP_UPSTREAM_URL", "http://up/mcp")
    forwarder_module._test_transport = httpx.MockTransport(lambda r: called.append(1) or httpx.Response(200, json={}))
    r = await client.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "resources/write_anything", "params": {}})
    assert r.json()["error"]["code"] == -32601
    assert not called
    assert db.get_audit_log(event_type="rpc.refused")


async def test_notification_gets_202(client):
    r = await client.post("/mcp", json={"jsonrpc": "2.0", "method": "notifications/initialized"})
    assert r.status_code == 202


# --- 4. Production mode never fails open -----------------------------------------

async def test_production_without_keys_refuses_requests(client, monkeypatch):
    monkeypatch.setenv("ESCROW_MODE", "production")
    r = await client.get("/api/pending")
    assert r.status_code == 503


def test_production_startup_requires_keys_and_upstream(monkeypatch):
    monkeypatch.setenv("ESCROW_MODE", "production")
    with pytest.raises(settings.ConfigError):
        settings.validate_startup()
    monkeypatch.setenv("ESCROW_KEYS", "a:agent:k")
    monkeypatch.setenv("MCP_UPSTREAM_URL", "http://up/mcp")
    settings.validate_startup()


# --- 5. No silent mock in production ---------------------------------------------

async def test_no_mock_result_in_production(monkeypatch):
    monkeypatch.setenv("ESCROW_MODE", "production")
    monkeypatch.delenv("MCP_UPSTREAM_URL", raising=False)
    body = await forwarder_module.forward(_call("get_user"))
    assert "error" in body and "mock" not in str(body)


# --- 6. Classifier ----------------------------------------------------------------

@pytest.mark.parametrize("name", [
    "get_and_remove_user", "read_and_erase_disk", "list_and_kill_pods",
    "getAndDeleteUser", "get_user_delete", "search_then_wipe",
])
def test_classifier_bypasses_are_held(name):
    assert classify_tool_call(name)["action"] == "HOLD", name


@pytest.mark.parametrize("name", [
    "get_credit_score", "get_payment_status", "list_dropdown_options",
    "read_email", "get_post", "getUser", "describe-table",
])
def test_classifier_reads_not_held(name):
    assert classify_tool_call(name)["action"] == "AUTO_APPROVE", name


def test_classifier_risk_levels():
    assert classify_tool_call("charge_card")["risk_level"] == "HIGH"
    assert classify_tool_call("git_push")["category"] == "GENERIC_WRITE"
    assert classify_tool_call("purge_cache")["risk_level"] == "CRITICAL"
    assert classify_tool_call("frobnicate")["action"] == "HOLD"


# --- Forwarding: SSE, sessions, failures ------------------------------------------

async def test_sse_upstream_response_parsed_and_session_returned(client, monkeypatch):
    sse = (
        'event: message\ndata: {"jsonrpc":"2.0","method":"notifications/progress","params":{}}\n\n'
        'event: message\ndata: {"jsonrpc":"2.0","id":1,"result":{"tools":[]}}\n\n'
    )
    monkeypatch.setenv("MCP_UPSTREAM_URL", "http://up/mcp")
    forwarder_module._test_transport = httpx.MockTransport(
        lambda r: httpx.Response(200, text=sse, headers={"content-type": "text/event-stream", "mcp-session-id": "s-9"})
    )
    r = await client.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}})
    assert r.json() == {"jsonrpc": "2.0", "id": 1, "result": {"tools": []}}
    assert r.headers.get("mcp-session-id") == "s-9"


async def test_invalid_upstream_body_is_rpc_error(client, monkeypatch):
    monkeypatch.setenv("MCP_UPSTREAM_URL", "http://up/mcp")
    forwarder_module._test_transport = httpx.MockTransport(
        lambda r: httpx.Response(200, text="<html>oops</html>", headers={"content-type": "text/html"})
    )
    r = await client.post("/mcp", json=_call("get_user"))
    assert r.status_code == 200 and r.json()["error"]["code"] == -32004


async def test_failed_execution_is_recorded(client, monkeypatch):
    monkeypatch.setenv("MCP_UPSTREAM_URL", "http://up/mcp")
    forwarder_module._test_transport = httpx.MockTransport(lambda r: httpx.Response(500))
    task = asyncio.create_task(client.post("/mcp", json=_call("delete_user")))
    rows = await _wait_for_pending(client)
    await client.post("/api/resolve", json={"action_id": rows[0]["id"], "decision": "APPROVED"})
    body = (await task).json()
    assert body["error"]["code"] == -32002
    row = db.get_action(rows[0]["id"])
    assert row["execution_status"] == "FAILED"
    assert db.get_audit_log(event_type="action.execution_failed")


async def test_auto_approved_calls_are_audited(client):
    await client.post("/mcp", json=_call("get_user"))
    assert db.get_audit_log(event_type="action.auto_approved")
    assert db.get_audit_log(event_type="action.executed")


# --- Auth details -----------------------------------------------------------------

async def test_query_key_only_on_sse(client, monkeypatch):
    monkeypatch.setenv("ESCROW_KEYS", "v:viewer:k2")
    assert (await client.get("/api/pending?api_key=k2")).status_code == 401
    assert (await client.get("/api/pending", headers={"X-Api-Key": "k2"})).status_code == 200


async def test_cors_preflight_not_blocked_by_auth(client, monkeypatch):
    monkeypatch.setenv("ESCROW_KEYS", "v:viewer:k2")
    r = await client.options(
        "/api/pending",
        headers={"Origin": "https://evil.example", "Access-Control-Request-Method": "GET"},
    )
    assert r.status_code != 401


def test_cors_production_default_is_same_origin(monkeypatch):
    monkeypatch.setenv("ESCROW_MODE", "production")
    monkeypatch.delenv("ESCROW_CORS_ORIGINS", raising=False)
    assert settings.cors_origins() == []
    monkeypatch.setenv("ESCROW_CORS_ORIGINS", "https://a.example, https://b.example")
    assert settings.cors_origins() == ["https://a.example", "https://b.example"]


async def test_viewer_cannot_export_audit(client, monkeypatch):
    monkeypatch.setenv("ESCROW_KEYS", "v:viewer:kv|a:admin:ka")
    assert (await client.get("/api/audit/export", headers={"X-Api-Key": "kv"})).status_code == 403
    r = await client.get("/api/audit/export", headers={"X-Api-Key": "ka"})
    assert r.status_code == 200 and r.text.startswith("id,created_at,event_type")


# --- Audit chain ------------------------------------------------------------------

async def test_audit_chain_detects_tampering(client):
    for i in range(3):
        db.insert_audit_log("test.event", detail={"i": i})
    assert (await client.get("/api/audit/verify")).json()["ok"] is True
    conn = db.get_conn()
    conn.execute("UPDATE audit_log SET actor='mallory' WHERE id = (SELECT MIN(id) FROM audit_log)")
    conn.commit()
    res = (await client.get("/api/audit/verify")).json()
    assert res["ok"] is False and res["first_bad_id"] is not None


# --- Notifications ----------------------------------------------------------------

async def test_slack_escapes_mentions_and_pagerduty_resolves(client, monkeypatch):
    monkeypatch.setenv("SLACK_WEBHOOK_URL", "http://slack/hook")
    monkeypatch.setenv("PAGERDUTY_ROUTING_KEY", "pd")
    sent = []
    notif_module._test_transport = httpx.MockTransport(
        lambda r: sent.append((str(r.url), r.content.decode())) or httpx.Response(200)
    )
    task = asyncio.create_task(client.post("/mcp", json=_call("delete_user", {"reason": "<!channel> click <http://x|here>"})))
    rows = await _wait_for_pending(client)
    await client.post("/api/resolve", json={"action_id": rows[0]["id"], "decision": "REJECTED"})
    await task
    await asyncio.sleep(0.05)
    slack = [b for u, b in sent if "slack" in u]
    assert slack and "<!channel>" not in slack[0] and "&lt;!channel&gt;" in slack[0]
    assert any('"event_action": "resolve"' in b or '"event_action":"resolve"' in b for u, b in sent if "pagerduty" in u)


# --- Metrics ----------------------------------------------------------------------

async def test_metrics_histogram(client):
    text = (await client.get("/metrics")).text
    assert "# TYPE escrow_review_latency_seconds histogram" in text
    assert 'escrow_review_latency_seconds_bucket{le="+Inf"}' in text
