"""Hold modes: async (answer at once + escrow_get_result) and wait with SSE
progress heartbeats.  Both exist because many MCP clients give up on a
request after ~60 s, while a human review can take minutes."""

from __future__ import annotations

import asyncio
import json

import httpx
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

import proxy.forwarder as forwarder_module
from proxy import db, proxy_engine
from proxy.main import app
from proxy.proxy_engine import EXECUTION_WAITERS

UPSTREAM_RESULT = {"content": [{"type": "text", "text": "upstream did it"}], "structuredContent": {"rows": 1}}


@pytest_asyncio.fixture
async def client(tmp_path, monkeypatch):
    db.DB_PATH = tmp_path / "hold_modes.db"
    db._conn = None
    EXECUTION_WAITERS.clear()
    db.init_db()
    monkeypatch.setenv("MCP_UPSTREAM_URL", "http://up/mcp")
    forwarder_module._test_transport = httpx.MockTransport(
        lambda r: httpx.Response(
            200,
            json={
                "jsonrpc": "2.0",
                "id": json.loads(r.content)["id"],
                "result": {"tools": [{"name": "delete_user"}]}
                if json.loads(r.content)["method"] == "tools/list"
                else UPSTREAM_RESULT,
            },
        )
    )
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        yield ac
    forwarder_module._test_transport = None
    EXECUTION_WAITERS.clear()
    db._conn = None


def _call(name, args=None, rid=1, meta=None):
    params = {"name": name, "arguments": args or {}}
    if meta:
        params["_meta"] = meta
    return {"jsonrpc": "2.0", "id": rid, "method": "tools/call", "params": params}


async def _get_result(client, action_id, headers=None):
    r = await client.post(
        "/mcp", json=_call("escrow_get_result", {"action_id": action_id}, rid=99), headers=headers or {}
    )
    return r.json()


async def _settle():
    for _ in range(20):
        await asyncio.sleep(0.01)


# --- async mode -----------------------------------------------------------------


async def test_async_held_call_returns_immediately(client, monkeypatch):
    monkeypatch.setenv("ESCROW_HOLD_MODE", "async")
    r = await asyncio.wait_for(client.post("/mcp", json=_call("delete_user", {"id": 1})), timeout=2)
    res = r.json()["result"]
    assert res["isError"] is False
    assert res["structuredContent"]["status"] == "PENDING"
    action_id = res["structuredContent"]["action_id"]
    assert "has NOT run yet" in res["content"][0]["text"]
    assert [a["id"] for a in (await client.get("/api/pending")).json()] == [action_id]
    assert "rpc_payload" not in (await client.get("/api/pending")).json()[0]  # internal column hidden

    pending = await _get_result(client, action_id)
    assert pending["result"]["structuredContent"]["status"] == "PENDING"


async def test_async_approval_runs_upstream_and_result_is_returned(client, monkeypatch):
    monkeypatch.setenv("ESCROW_HOLD_MODE", "async")
    sent = []
    base = forwarder_module._test_transport

    def spy(req):
        sent.append(dict(req.headers))
        return base.handle_request(req)

    forwarder_module._test_transport = httpx.MockTransport(spy)
    r = await client.post("/mcp", json=_call("delete_user", {"id": 1}), headers={"Mcp-Session-Id": "sess-a"})
    action_id = r.json()["result"]["structuredContent"]["action_id"]
    assert sent == []  # nothing ran yet

    await client.post("/api/resolve", json={"action_id": action_id, "decision": "APPROVED"})
    await _settle()

    assert sent and sent[0].get("mcp-session-id") == "sess-a"  # original session reused
    out = await _get_result(client, action_id)
    assert out["result"]["content"][0]["text"] == "upstream did it"
    assert out["result"]["structuredContent"]["escrow_status"] == "APPROVED"
    assert out["result"]["structuredContent"]["rows"] == 1
    assert db.get_action(action_id)["execution_status"] == "SUCCEEDED"


async def test_async_rejection_reported(client, monkeypatch):
    monkeypatch.setenv("ESCROW_HOLD_MODE", "async")
    r = await client.post("/mcp", json=_call("transfer_funds", {"amount": 5}))
    action_id = r.json()["result"]["structuredContent"]["action_id"]
    await client.post("/api/resolve", json={"action_id": action_id, "decision": "REJECTED", "rejection_reason": "nope"})
    out = await _get_result(client, action_id)
    assert out["result"]["isError"] is True
    assert out["result"]["structuredContent"] == {"status": "REJECTED", "action_id": action_id, "reason": "nope"}


async def test_async_result_only_visible_to_submitting_agent(client, monkeypatch):
    monkeypatch.setenv("ESCROW_HOLD_MODE", "async")
    monkeypatch.setenv("ESCROW_KEYS", "bot-a:agent:ka|bot-b:agent:kb|ops:admin:kadm")
    r = await client.post("/mcp", json=_call("delete_user"), headers={"X-Api-Key": "ka"})
    action_id = r.json()["result"]["structuredContent"]["action_id"]

    other = await _get_result(client, action_id, headers={"X-Api-Key": "kb"})
    assert other["error"]["code"] == -32602  # looks like an unknown id
    assert "result" in await _get_result(client, action_id, headers={"X-Api-Key": "ka"})
    assert "result" in await _get_result(client, action_id, headers={"X-Api-Key": "kadm"})


async def test_async_tools_list_advertises_result_tool(client, monkeypatch):
    monkeypatch.setenv("ESCROW_HOLD_MODE", "async")
    r = await client.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}})
    names = [t["name"] for t in r.json()["result"]["tools"]]
    assert names == ["delete_user", "escrow_get_result"]


async def test_wait_mode_tools_list_unchanged(client):
    r = await client.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}})
    assert [t["name"] for t in r.json()["result"]["tools"]] == ["delete_user"]


async def test_async_unreviewed_actions_expire(client, monkeypatch):
    monkeypatch.setenv("ESCROW_HOLD_MODE", "async")
    r = await client.post("/mcp", json=_call("delete_user"))
    action_id = r.json()["result"]["structuredContent"]["action_id"]
    monkeypatch.setenv("ESCROW_HOLD_TIMEOUT", "0")
    await asyncio.sleep(0.05)
    assert proxy_engine.expire_async_holds() == 1
    out = await _get_result(client, action_id)
    assert out["result"]["structuredContent"]["status"] == "REJECTED"
    assert "timed out" in out["result"]["structuredContent"]["reason"]
    # A late approval is refused and nothing runs
    late = await client.post("/api/resolve", json={"action_id": action_id, "decision": "APPROVED"})
    assert late.status_code == 404


# --- wait mode with SSE heartbeats ---------------------------------------------


async def test_wait_mode_streams_progress_then_result(client, monkeypatch):
    monkeypatch.setenv("ESCROW_HEARTBEAT_SECONDS", "1")

    async def approve_soon():
        for _ in range(200):
            rows = (await client.get("/api/pending")).json()
            if rows:
                await asyncio.sleep(1.3)  # let at least one heartbeat go out
                await client.post("/api/resolve", json={"action_id": rows[0]["id"], "decision": "APPROVED"})
                return
            await asyncio.sleep(0.02)

    approver = asyncio.create_task(approve_soon())
    r = await client.post(
        "/mcp",
        json=_call("delete_user", meta={"progressToken": "tok-1"}),
        headers={"Accept": "application/json, text/event-stream"},
    )
    await approver
    assert r.headers["content-type"].startswith("text/event-stream")
    messages = [json.loads(line[5:]) for line in r.text.splitlines() if line.startswith("data:")]
    progress = [m for m in messages if m.get("method") == "notifications/progress"]
    assert progress and progress[0]["params"]["progressToken"] == "tok-1"
    assert messages[-1]["id"] == 1 and messages[-1]["result"]["content"][0]["text"] == "upstream did it"


async def test_wait_mode_without_sse_accept_is_plain_json(client):
    async def approve_soon():
        for _ in range(200):
            rows = (await client.get("/api/pending")).json()
            if rows:
                await client.post("/api/resolve", json={"action_id": rows[0]["id"], "decision": "APPROVED"})
                return
            await asyncio.sleep(0.02)

    approver = asyncio.create_task(approve_soon())
    r = await client.post("/mcp", json=_call("delete_user"))
    await approver
    assert r.headers["content-type"].startswith("application/json")
    assert r.json()["result"]["content"][0]["text"] == "upstream did it"


async def test_safe_read_with_sse_accept_is_not_streamed(client):
    r = await client.post("/mcp", json=_call("get_user"), headers={"Accept": "application/json, text/event-stream"})
    assert r.headers["content-type"].startswith("application/json")
