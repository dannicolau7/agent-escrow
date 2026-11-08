"""End-to-end tests: a real proxy process (production mode) in front of the
fake MCP server in scripts/fake_upstream.py, both started as subprocesses.

These catch problems unit tests can't: startup config, middleware order,
real HTTP/SSE between processes, restarts.  Run with `make e2e`.
"""

from __future__ import annotations

import os
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

import httpx
import pytest

pytestmark = pytest.mark.e2e

ROOT = Path(__file__).parent
KEYS = "bot:agent:k-agent|alice:approver:k-approver|ops:admin:k-admin|graf:viewer:k-viewer"


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _wait_http(url: str, proc: subprocess.Popen, timeout: float = 20.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            out = proc.stdout.read().decode() if proc.stdout else ""
            raise RuntimeError(f"process exited early:\n{out}")
        try:
            httpx.get(url, timeout=0.5)
            return
        except httpx.HTTPError:
            time.sleep(0.1)
    raise RuntimeError(f"{url} never came up")


class Stack:
    def __init__(self, tmp: Path):
        self.tmp = tmp
        self.up_port = _free_port()
        self.port = _free_port()
        self.url = f"http://127.0.0.1:{self.port}"
        self.upstream_url = f"http://127.0.0.1:{self.up_port}"
        self.db = tmp / "e2e.db"
        self.upstream: subprocess.Popen | None = None
        self.proxy: subprocess.Popen | None = None

    def _env(self, **extra) -> dict:
        env = {k: v for k, v in os.environ.items() if not k.startswith(("ESCROW_", "MCP_", "SLACK_", "PAGERDUTY_"))}
        env.update(
            ESCROW_MODE="production",
            ESCROW_KEYS=KEYS,
            ESCROW_AUDIT_KEY="e2e-audit-secret",
            ESCROW_HOLD_TIMEOUT="20",
            MCP_UPSTREAM_URL=f"{self.upstream_url}/mcp",
            MCP_UPSTREAM_TOKEN="upstream-token",
            DB_PATH=str(self.db),
            FAKE_UPSTREAM_PORT=str(self.up_port),
        )
        env.update(extra)
        return env

    def start_upstream(self) -> None:
        self.upstream = subprocess.Popen(
            [sys.executable, "scripts/fake_upstream.py"],
            cwd=ROOT,
            env=self._env(),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
        )
        _wait_http(f"{self.upstream_url}/_last", self.upstream)

    def start_proxy(self, **extra) -> subprocess.Popen:
        self.proxy = subprocess.Popen(
            # Same flags as the Dockerfile CMD.
            [
                sys.executable,
                "-m",
                "uvicorn",
                "proxy.main:app",
                "--host",
                "127.0.0.1",
                "--port",
                str(self.port),
                "--workers",
                "1",
                "--proxy-headers",
                "--timeout-graceful-shutdown",
                "5",
            ],
            cwd=ROOT,
            env=self._env(**extra),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
        )
        _wait_http(f"{self.url}/health", self.proxy)
        return self.proxy

    def stop_proxy(self) -> None:
        if self.proxy and self.proxy.poll() is None:
            self.proxy.terminate()
            self.proxy.wait(15)

    def stop(self) -> None:
        self.stop_proxy()
        if self.upstream and self.upstream.poll() is None:
            self.upstream.terminate()
            self.upstream.wait(10)

    # --- helpers -----------------------------------------------------------
    def rpc(
        self, method: str, params: dict | None = None, rid: int = 1, key: str = "k-agent", headers=None, timeout=30.0
    ):
        h = {"authorization": f"Bearer {key}", **(headers or {})}
        return httpx.post(
            f"{self.url}/mcp",
            json={"jsonrpc": "2.0", "id": rid, "method": method, "params": params or {}},
            headers=h,
            timeout=timeout,
        )

    def api(self, method: str, path: str, key: str, **kw):
        return httpx.request(method, f"{self.url}{path}", headers={"x-api-key": key}, timeout=10.0, **kw)

    def wait_pending(self, n: int = 1) -> list:
        for _ in range(200):
            rows = self.api("GET", "/api/pending", "k-viewer").json()
            if len(rows) >= n:
                return rows
            time.sleep(0.05)
        raise AssertionError("nothing reached the pending queue")


@pytest.fixture
def stack(tmp_path):
    s = Stack(tmp_path)
    s.start_upstream()
    s.start_proxy()
    yield s
    s.stop()


def _call_in_background(stack: Stack, name: str, args: dict, rid: int = 2) -> tuple[threading.Thread, dict]:
    box: dict = {}

    def run():
        try:
            box["resp"] = stack.rpc("tools/call", {"name": name, "arguments": args}, rid=rid)
        except httpx.HTTPError as exc:  # e.g. the proxy restarting mid-hold
            box["error"] = exc

    t = threading.Thread(target=run, daemon=True)
    t.start()
    return t, box


# ---------------------------------------------------------------------------


def test_production_refuses_to_start_without_keys(tmp_path):
    s = Stack(tmp_path)
    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "proxy.main:app", "--port", str(s.port)],
        cwd=ROOT,
        env=s._env(ESCROW_KEYS="", MCP_UPSTREAM_URL=""),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    out, _ = proc.communicate(timeout=20)
    assert proc.returncode != 0
    assert b"Refusing to start in production mode" in out


def test_session_handshake_passes_through(stack):
    r = stack.rpc("initialize", {"protocolVersion": "2025-06-18", "capabilities": {}})
    assert r.status_code == 200
    assert r.json()["result"]["serverInfo"]["name"] == "fake-upstream"
    sid = r.headers.get("mcp-session-id")
    assert sid and sid.startswith("sess-")

    tools = stack.rpc("tools/list", headers={"mcp-session-id": sid}).json()
    assert {t["name"] for t in tools["result"]["tools"]} >= {"get_user", "delete_user"}
    last = httpx.get(f"{stack.upstream_url}/_last").json()
    assert last["mcp_session_id"] == sid
    assert "text/event-stream" in last["accept"]


def test_safe_read_runs_without_review(stack):
    r = stack.rpc("tools/call", {"name": "get_user", "arguments": {"id": 1}})
    assert "fake upstream ran get_user" in r.json()["result"]["content"][0]["text"]
    assert stack.api("GET", "/api/pending", "k-viewer").json() == []


def test_held_call_approved_runs_upstream_with_upstream_token(stack):
    t, box = _call_in_background(stack, "delete_user", {"id": 7, "reason": "e2e"})
    action = stack.wait_pending()[0]
    assert action["tool_name"] == "delete_user" and action["risk_level"] == "CRITICAL"

    # Nobody but an approver can decide
    for key in ("k-viewer", "k-agent"):
        r = stack.api("POST", "/api/resolve", key, json={"action_id": action["id"], "decision": "APPROVED"})
        assert r.status_code == 403

    r = stack.api("POST", "/api/resolve", "k-approver", json={"action_id": action["id"], "decision": "APPROVED"})
    assert r.status_code == 200 and r.json()["resolved_by"] == "alice"
    t.join(15)

    body = box["resp"].json()
    assert "fake upstream ran delete_user" in body["result"]["content"][0]["text"]
    last = httpx.get(f"{stack.upstream_url}/_last").json()
    assert last["authorization"] == "Bearer upstream-token"  # never the agent's key
    assert last["x_api_key"] is None

    row = stack.api("GET", "/api/history?limit=1", "k-viewer").json()[0]
    assert (row["status"], row["resolved_by"], row["execution_status"]) == ("APPROVED", "alice", "SUCCEEDED")


def test_held_call_rejected_never_reaches_upstream(stack):
    httpx.get(f"{stack.upstream_url}/_last")  # warm
    t, box = _call_in_background(stack, "transfer_funds", {"amount": 9000})
    action = stack.wait_pending()[0]
    stack.api(
        "POST",
        "/api/resolve",
        "k-approver",
        json={"action_id": action["id"], "decision": "REJECTED", "rejection_reason": "too large"},
    )
    t.join(15)
    assert "too large" in box["resp"].json()["error"]["message"]
    last = httpx.get(f"{stack.upstream_url}/_last").json()
    assert last.get("payload", {}).get("params", {}).get("name") != "transfer_funds"


def test_unknown_method_refused(stack):
    r = stack.rpc("tools/delete", {"name": "x"})
    assert r.json()["error"]["code"] == -32601


def test_auth_required_everywhere(stack):
    assert httpx.post(f"{stack.url}/mcp", json={}).status_code == 401
    assert httpx.get(f"{stack.url}/api/pending").status_code == 401
    assert httpx.get(f"{stack.url}/api/pending", headers={"x-api-key": "nope"}).status_code == 403
    assert httpx.get(f"{stack.url}/api/pending?api_key=k-viewer").status_code == 401
    assert stack.api("GET", "/api/audit/export", "k-viewer").status_code == 403


def test_audit_chain_and_metrics(stack):
    stack.rpc("tools/call", {"name": "get_user", "arguments": {}})
    verify = stack.api("GET", "/api/audit/verify", "k-admin").json()
    assert verify["ok"] is True and verify["checked"] >= 2
    metrics = stack.api("GET", "/metrics", "k-viewer").text
    assert "escrow_pending_count 0" in metrics


def test_restart_rejects_pending_and_agent_gets_error(stack):
    t, box = _call_in_background(stack, "delete_user", {"id": 9})
    action = stack.wait_pending()[0]
    stack.stop_proxy()
    t.join(15)
    resp = box.get("resp")
    # The agent must get an error, a dropped connection, or an empty error
    # response from the shutting-down server, never a success.
    if resp is not None and resp.status_code == 200 and resp.content:
        assert "result" not in resp.json()

    stack.start_proxy()
    assert stack.api("GET", "/api/pending", "k-viewer").json() == []
    row = next(r for r in stack.api("GET", "/api/history?limit=10", "k-viewer").json() if r["id"] == action["id"])
    assert row["status"] == "REJECTED"
    # A late approval of the stale action must not run anything
    r = stack.api("POST", "/api/resolve", "k-approver", json={"action_id": action["id"], "decision": "APPROVED"})
    assert r.status_code == 404


# --- Hold modes (client timeouts) ---------------------------------------------


def test_sse_client_disconnect_withdraws_action(stack):
    """A wait-mode client that gives up (timeout / Ctrl-C) must not leave an
    approvable action behind: approving it later would run a call nobody is
    waiting for."""
    with (
        httpx.Client(timeout=10) as c,
        c.stream(
            "POST",
            f"{stack.url}/mcp",
            json={
                "jsonrpc": "2.0",
                "id": 5,
                "method": "tools/call",
                "params": {"name": "delete_user", "arguments": {}},
            },
            headers={"authorization": "Bearer k-agent", "accept": "application/json, text/event-stream"},
        ) as r,
    ):
        assert r.headers["content-type"].startswith("text/event-stream")
        action = stack.wait_pending()[0]
    # connection closed here, before anyone decided
    for _ in range(100):
        row = stack.api("GET", "/api/history?limit=5", "k-viewer").json()
        if any(x["id"] == action["id"] for x in row):
            break
        time.sleep(0.05)
    row = next(x for x in stack.api("GET", "/api/history?limit=5", "k-viewer").json() if x["id"] == action["id"])
    assert row["status"] == "REJECTED" and "disconnected" in row["rejection_reason"]
    assert stack.api("GET", "/api/pending", "k-viewer").json() == []


def test_async_mode_end_to_end(tmp_path):
    s = Stack(tmp_path)
    s.start_upstream()
    s.start_proxy(ESCROW_HOLD_MODE="async")
    try:
        tools = s.rpc("tools/list").json()["result"]["tools"]
        assert "escrow_get_result" in [t["name"] for t in tools]

        t0 = time.monotonic()
        held = s.rpc("tools/call", {"name": "delete_user", "arguments": {"id": 3}}).json()
        assert time.monotonic() - t0 < 2  # answered at once, no client timeout possible
        action_id = held["result"]["structuredContent"]["action_id"]

        def result():
            return s.rpc("tools/call", {"name": "escrow_get_result", "arguments": {"action_id": action_id}}).json()

        assert result()["result"]["structuredContent"]["status"] == "PENDING"
        s.api("POST", "/api/resolve", "k-approver", json={"action_id": action_id, "decision": "APPROVED"})
        for _ in range(100):
            out = result()
            if "escrow_status" in out["result"].get("structuredContent", {}):
                break
            time.sleep(0.05)
        assert "fake upstream ran delete_user" in out["result"]["content"][0]["text"]
        last = httpx.get(f"{s.upstream_url}/_last").json()
        assert last["authorization"] == "Bearer upstream-token"
    finally:
        s.stop()
