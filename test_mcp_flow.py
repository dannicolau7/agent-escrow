"""
MCP flow tests: /mcp, /api/pending, /api/resolve, /api/history.

Covers:
 - auto-approval of safe reads (get_*, list_*, search_*, etc.)
 - mutating calls held in pending_actions
 - concurrent approve / reject via /api/resolve
 - /api/history shows resolved entries
 - /api/resolve rejects bad input (400) and unknown ids (404)
 - MCP timeout via direct call to process_mcp_call
 - unlisted tools held by default policy
"""

from __future__ import annotations

import asyncio

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from proxy import db
from proxy.main import app
from proxy.proxy_engine import EXECUTION_WAITERS, process_mcp_call

pytestmark = pytest.mark.asyncio


# ---------------------------------------------------------------------------
# Fixture
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def client(tmp_path):
    db.DB_PATH = tmp_path / "test_escrow.db"
    db._conn = None
    EXECUTION_WAITERS.clear()
    db.init_db()

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac

    EXECUTION_WAITERS.clear()
    db._conn = None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _mcp_payload(tool_name: str, args: dict | None = None, rpc_id: int = 1) -> dict:
    return {
        "jsonrpc": "2.0",
        "id": rpc_id,
        "method": "tools/call",
        "params": {"name": tool_name, "arguments": args or {}},
    }


async def _get_pending(client) -> list:
    return (await client.get("/api/pending")).json()


# ---------------------------------------------------------------------------
# 1. Safe read (get_* prefix) → auto-approved, nothing lands in pending
# ---------------------------------------------------------------------------


async def test_safe_read_auto_approved(client):
    r = await client.post("/mcp", json=_mcp_payload("get_customer_info"))
    assert r.status_code == 200
    body = r.json()
    assert "result" in body
    assert body["result"]["content"][0]["type"] == "text"
    assert await _get_pending(client) == []


# ---------------------------------------------------------------------------
# 2. list_* prefix → auto-approved
# ---------------------------------------------------------------------------


async def test_list_prefix_auto_approved(client):
    r = await client.post("/mcp", json=_mcp_payload("list_orders"))
    assert "result" in r.json()
    assert await _get_pending(client) == []


# ---------------------------------------------------------------------------
# 3. search_* prefix → auto-approved
# ---------------------------------------------------------------------------


async def test_search_prefix_auto_approved(client):
    r = await client.post("/mcp", json=_mcp_payload("search_records", {"q": "foo"}))
    assert "result" in r.json()
    assert await _get_pending(client) == []


# ---------------------------------------------------------------------------
# 4. Mutating call appears in /api/pending
# ---------------------------------------------------------------------------


async def test_mutating_call_appears_in_pending(client):
    async def _call():
        return await client.post("/mcp", json=_mcp_payload("delete_record", {"id": 42}))

    task = asyncio.create_task(_call())
    await asyncio.sleep(0.05)

    pending = await _get_pending(client)
    assert len(pending) == 1
    assert pending[0]["tool_name"] == "delete_record"
    assert pending[0]["status"] == "PENDING"
    assert pending[0]["risk_level"] == "CRITICAL"

    action_id = pending[0]["id"]
    await client.post("/api/resolve", json={"action_id": action_id, "decision": "APPROVED"})
    await task


# ---------------------------------------------------------------------------
# 5. Approve flow: MCP response carries approval text
# ---------------------------------------------------------------------------


async def test_mcp_approve_flow(client):
    async def _call():
        return await client.post("/mcp", json=_mcp_payload("update_record", {"id": 1}))

    task = asyncio.create_task(_call())
    await asyncio.sleep(0.05)

    pending = await _get_pending(client)
    action_id = pending[0]["id"]

    r_resolve = await client.post("/api/resolve", json={"action_id": action_id, "decision": "APPROVED"})
    assert r_resolve.status_code == 200
    assert r_resolve.json()["resolved_as"] == "APPROVED"

    mcp_resp = (await task).json()
    assert "result" in mcp_resp
    assert mcp_resp["result"]["content"][0]["type"] == "text"


# ---------------------------------------------------------------------------
# 6. Reject flow: MCP response is a JSON-RPC error with the reason
# ---------------------------------------------------------------------------


async def test_mcp_reject_flow(client):
    async def _call():
        return await client.post("/mcp", json=_mcp_payload("send_notification", {"to": "user@corp.com"}))

    task = asyncio.create_task(_call())
    await asyncio.sleep(0.05)

    pending = await _get_pending(client)
    action_id = pending[0]["id"]

    await client.post(
        "/api/resolve",
        json={"action_id": action_id, "decision": "REJECTED", "rejection_reason": "not authorised"},
    )

    mcp_resp = (await task).json()
    assert "error" in mcp_resp
    assert "not authorised" in mcp_resp["error"]["message"]


# ---------------------------------------------------------------------------
# 7. /api/history reflects resolved actions
# ---------------------------------------------------------------------------


async def test_history_shows_resolved_action(client):
    async def _call():
        return await client.post("/mcp", json=_mcp_payload("write_file", {"path": "/tmp/x"}))

    task = asyncio.create_task(_call())
    await asyncio.sleep(0.05)

    pending = await _get_pending(client)
    action_id = pending[0]["id"]
    await client.post("/api/resolve", json={"action_id": action_id, "decision": "APPROVED"})
    await task

    history = (await client.get("/api/history")).json()
    assert any(h["id"] == action_id for h in history)
    resolved = next(h for h in history if h["id"] == action_id)
    assert resolved["status"] == "APPROVED"
    assert resolved["resolved_at"] is not None


# ---------------------------------------------------------------------------
# 8. /api/resolve with invalid decision → 400
# ---------------------------------------------------------------------------


async def test_resolve_invalid_decision(client):
    async def _call():
        return await client.post("/mcp", json=_mcp_payload("delete_record", {}))

    task = asyncio.create_task(_call())
    await asyncio.sleep(0.05)

    pending = await _get_pending(client)
    action_id = pending[0]["id"]

    r = await client.post("/api/resolve", json={"action_id": action_id, "decision": "MAYBE"})
    assert r.status_code == 400

    # clean up so the task doesn't hang
    await client.post("/api/resolve", json={"action_id": action_id, "decision": "APPROVED"})
    await task


# ---------------------------------------------------------------------------
# 9. /api/resolve with unknown action_id → 404
# ---------------------------------------------------------------------------


async def test_resolve_unknown_action(client):
    r = await client.post("/api/resolve", json={"action_id": "ghost-action", "decision": "APPROVED"})
    assert r.status_code == 404


# ---------------------------------------------------------------------------
# 10. MCP timeout: process_mcp_call returns a JSON-RPC error
# ---------------------------------------------------------------------------


async def test_mcp_timeout(client):
    payload = _mcp_payload("delete_record", {"id": 99}, rpc_id=99)
    result = await process_mcp_call(payload, timeout=0.1)

    assert "error" in result
    assert result["error"]["code"] == -32000
    assert "Timeout" in result["error"]["message"]
    # waiter must be cleaned up after timeout
    assert not any("delete_record" in str(v) for v in EXECUTION_WAITERS.values())


# ---------------------------------------------------------------------------
# 11. Unlisted tool with no matching patterns → held by default
# ---------------------------------------------------------------------------


async def test_unlisted_mcp_tool_held_by_default(client):
    async def _call():
        return await client.post("/mcp", json=_mcp_payload("some_novel_mcp_tool"))

    task = asyncio.create_task(_call())
    await asyncio.sleep(0.05)

    pending = await _get_pending(client)
    assert len(pending) == 1
    assert pending[0]["tool_name"] == "some_novel_mcp_tool"

    action_id = pending[0]["id"]
    await client.post("/api/resolve", json={"action_id": action_id, "decision": "REJECTED"})
    await task


# ---------------------------------------------------------------------------
# 12. /api/pending is empty after all actions resolved
# ---------------------------------------------------------------------------


async def test_pending_empty_after_resolution(client):
    async def _submit(tool):
        return await client.post("/mcp", json=_mcp_payload(tool))

    task1 = asyncio.create_task(_submit("delete_record"))
    task2 = asyncio.create_task(_submit("send_notification"))
    await asyncio.sleep(0.05)

    pending = await _get_pending(client)
    assert len(pending) == 2

    for p in pending:
        await client.post("/api/resolve", json={"action_id": p["id"], "decision": "APPROVED"})

    await task1
    await task2

    assert await _get_pending(client) == []
