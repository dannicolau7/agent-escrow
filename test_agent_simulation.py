"""
Agent simulation tests for the escrow proxy.

Runs entirely in-process via httpx AsyncClient + ASGITransport — no server needed.
Each test gets a fresh SQLite database in a tmp directory.

Run:
    cd /Users/.../agent-escrow
    pip install -r proxy/requirements.txt
    pytest test_agent_simulation.py -v
"""
from __future__ import annotations

import asyncio

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from proxy import db, escrow
from proxy.main import app
from proxy.policies import load_policy

pytestmark = pytest.mark.asyncio


# ---------------------------------------------------------------------------
# Fixture: fresh in-process client per test
# ---------------------------------------------------------------------------

@pytest_asyncio.fixture
async def client(tmp_path):
    # Point the db module at a throw-away file so tests are isolated
    db.DB_PATH = tmp_path / "test_escrow.db"
    db._conn = None
    escrow._pending.clear()
    db.init_db()
    load_policy()

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac

    db._conn = None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

async def _submit(client, tool_name: str, agent_id: str = "agent-1", **kwargs):
    return await client.post(
        "/calls",
        json={"agent_id": agent_id, "tool_name": tool_name, "tool_input": kwargs},
    )


async def _approve(client, call_id: str, reviewer: str = "reviewer-1"):
    return await client.post(
        f"/calls/{call_id}/approve",
        json={"reviewer_id": reviewer},
    )


async def _reject(client, call_id: str, reviewer: str = "reviewer-1"):
    return await client.post(
        f"/calls/{call_id}/reject",
        json={"reviewer_id": reviewer},
    )


# ---------------------------------------------------------------------------
# 1. Health check
# ---------------------------------------------------------------------------

async def test_health(client):
    r = await client.get("/health")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok"
    assert body["pending_calls"] == 0


# ---------------------------------------------------------------------------
# 2. Safe (non-mutating) tool call — allowed immediately
# ---------------------------------------------------------------------------

async def test_safe_call_allowed_immediately(client):
    r = await _submit(client, "file_read", path="/etc/hosts")
    assert r.status_code == 201
    body = r.json()
    assert body["status"] == "allowed"
    assert body["is_mutating"] is False

    # DB record should reflect allowed state
    r2 = await client.get(f"/calls/{body['call_id']}")
    assert r2.json()["status"] == "allowed"


# ---------------------------------------------------------------------------
# 3. Mutating call — held pending approval
# ---------------------------------------------------------------------------

async def test_mutating_call_held_as_pending(client):
    r = await _submit(client, "file_write", path="/tmp/x.txt", content="hello")
    assert r.status_code == 201
    body = r.json()
    assert body["status"] == "pending"
    assert body["is_mutating"] is True
    assert escrow.pending_count() == 1


# ---------------------------------------------------------------------------
# 4. GET /calls/{call_id} — 404 for unknown id
# ---------------------------------------------------------------------------

async def test_get_unknown_call_returns_404(client):
    r = await client.get("/calls/does-not-exist")
    assert r.status_code == 404


# ---------------------------------------------------------------------------
# 5. List calls — unfiltered and filtered by status
# ---------------------------------------------------------------------------

async def test_list_calls(client):
    await _submit(client, "file_read")
    await _submit(client, "file_write")

    r = await client.get("/calls")
    assert r.status_code == 200
    assert len(r.json()) == 2

    r_pend = await client.get("/calls", params={"status": "pending"})
    assert len(r_pend.json()) == 1

    r_allow = await client.get("/calls", params={"status": "allowed"})
    assert len(r_allow.json()) == 1


# ---------------------------------------------------------------------------
# 6. Reviewer approves a pending call
# ---------------------------------------------------------------------------

async def test_approve_pending_call(client):
    call_id = (await _submit(client, "file_delete", path="/tmp/old")).json()["call_id"]

    r = await _approve(client, call_id)
    assert r.status_code == 200
    assert r.json()["status"] == "approved"

    record = (await client.get(f"/calls/{call_id}")).json()
    assert record["status"] == "approved"
    assert record["resolved_by"] == "reviewer-1"
    assert record["resolved_at"] is not None


# ---------------------------------------------------------------------------
# 7. Reviewer rejects a pending call
# ---------------------------------------------------------------------------

async def test_reject_pending_call(client):
    call_id = (await _submit(client, "send_email", to="ceo@corp.com")).json()["call_id"]

    r = await _reject(client, call_id)
    assert r.status_code == 200
    assert r.json()["status"] == "rejected"

    record = (await client.get(f"/calls/{call_id}")).json()
    assert record["status"] == "rejected"


# ---------------------------------------------------------------------------
# 8. Double-approve / double-reject → 409
# ---------------------------------------------------------------------------

async def test_double_approve_returns_409(client):
    call_id = (await _submit(client, "file_write")).json()["call_id"]
    await _approve(client, call_id)
    r = await _approve(client, call_id)
    assert r.status_code == 409


async def test_reject_already_allowed_call_returns_409(client):
    call_id = (await _submit(client, "file_read")).json()["call_id"]  # allowed
    r = await _reject(client, call_id)
    assert r.status_code == 409


# ---------------------------------------------------------------------------
# 9. Wait endpoint — call already resolved returns immediately
# ---------------------------------------------------------------------------

async def test_wait_on_already_resolved_call(client):
    call_id = (await _submit(client, "file_write")).json()["call_id"]
    await _approve(client, call_id)

    r = await client.get(f"/calls/{call_id}/wait")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "approved"
    assert body["timed_out"] is False


# ---------------------------------------------------------------------------
# 10. Wait endpoint — times out when no reviewer acts
# ---------------------------------------------------------------------------

async def test_wait_times_out(client):
    call_id = (await _submit(client, "deploy", env="prod")).json()["call_id"]

    r = await client.get(f"/calls/{call_id}/wait", params={"timeout": 1.0})
    assert r.status_code == 200
    body = r.json()
    assert body["timed_out"] is True
    assert body["status"] == "pending"


# ---------------------------------------------------------------------------
# 11. Concurrent: agent waits while reviewer approves asynchronously
# ---------------------------------------------------------------------------

async def test_concurrent_wait_and_approve(client):
    call_id = (await _submit(client, "git_push", branch="main")).json()["call_id"]

    async def reviewer():
        await asyncio.sleep(0.1)
        return await _approve(client, call_id)

    wait_task = asyncio.create_task(
        client.get(f"/calls/{call_id}/wait", params={"timeout": 10.0})
    )
    approve_resp = await reviewer()
    wait_resp = await wait_task

    assert approve_resp.status_code == 200
    assert wait_resp.status_code == 200
    assert wait_resp.json()["status"] == "approved"
    assert wait_resp.json()["timed_out"] is False


# ---------------------------------------------------------------------------
# 12. Concurrent: agent waits while reviewer rejects asynchronously
# ---------------------------------------------------------------------------

async def test_concurrent_wait_and_reject(client):
    call_id = (await _submit(client, "run_command", cmd="rm -rf /")).json()["call_id"]

    async def reviewer():
        await asyncio.sleep(0.1)
        return await _reject(client, call_id, reviewer="safety-bot")

    wait_task = asyncio.create_task(
        client.get(f"/calls/{call_id}/wait", params={"timeout": 10.0})
    )
    reject_resp = await reviewer()
    wait_resp = await wait_task

    assert reject_resp.status_code == 200
    assert wait_resp.json()["status"] == "rejected"
    assert wait_resp.json()["timed_out"] is False


# ---------------------------------------------------------------------------
# 13. Unlisted tool — held by default policy
# ---------------------------------------------------------------------------

async def test_unlisted_tool_held_by_default(client):
    r = await _submit(client, "some_novel_tool")
    body = r.json()
    assert body["status"] == "pending"
    assert body["is_mutating"] is True
    assert "default policy" in body["message"].lower()


# ---------------------------------------------------------------------------
# 14. SQLite WAL mode is active
# ---------------------------------------------------------------------------

async def test_sqlite_wal_mode(client):
    # Ensure a connection exists
    await client.get("/health")
    conn = db.get_conn()
    row = conn.execute("PRAGMA journal_mode").fetchone()
    assert row[0] == "wal"


# ---------------------------------------------------------------------------
# 15. Health reflects live pending count
# ---------------------------------------------------------------------------

async def test_health_pending_count(client):
    await _submit(client, "file_write")
    await _submit(client, "db_delete")

    r = await client.get("/health")
    assert r.json()["pending_calls"] == 2


# ---------------------------------------------------------------------------
# 16. Approve non-existent call → 404
# ---------------------------------------------------------------------------

async def test_approve_nonexistent_call(client):
    r = await _approve(client, "ghost-id")
    assert r.status_code == 404


# ---------------------------------------------------------------------------
# 17. Wait on non-existent call → 404
# ---------------------------------------------------------------------------

async def test_wait_nonexistent_call(client):
    r = await client.get("/calls/ghost-id/wait")
    assert r.status_code == 404


# ---------------------------------------------------------------------------
# 18. Full agent lifecycle simulation
# ---------------------------------------------------------------------------

async def test_full_agent_lifecycle(client):
    """
    Simulate an agent that:
      1. reads a file (auto-allowed)
      2. writes a file (held) → reviewer approves → agent unblocked
      3. deletes a file (held) → reviewer rejects → agent sees rejection
    """
    # Step 1 — safe read
    r = await _submit(client, "file_read", agent_id="agent-sim", path="/data/report.csv")
    assert r.json()["status"] == "allowed"

    # Step 2 — mutating write, concurrent approval
    write_id = (await _submit(client, "file_write", agent_id="agent-sim",
                               path="/data/output.csv", content="...")).json()["call_id"]

    async def approve_write():
        await asyncio.sleep(0.05)
        await _approve(client, write_id, reviewer="human-review")

    wait_task = asyncio.create_task(
        client.get(f"/calls/{write_id}/wait", params={"timeout": 10.0})
    )
    await approve_write()
    wait_body = (await wait_task).json()
    assert wait_body["status"] == "approved"

    # Step 3 — mutating delete, concurrent rejection
    del_id = (await _submit(client, "file_delete", agent_id="agent-sim",
                              path="/data/old.csv")).json()["call_id"]

    async def reject_delete():
        await asyncio.sleep(0.05)
        await _reject(client, del_id, reviewer="human-review")

    wait_task2 = asyncio.create_task(
        client.get(f"/calls/{del_id}/wait", params={"timeout": 10.0})
    )
    await reject_delete()
    wait_body2 = (await wait_task2).json()
    assert wait_body2["status"] == "rejected"
