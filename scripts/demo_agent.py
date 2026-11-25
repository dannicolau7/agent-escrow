"""Demo agent: talks MCP to the proxy like a real client would.

Run (with `make dev` running):  python scripts/demo_agent.py
Then approve / reject the held calls at http://127.0.0.1:4000/ui/ (J / K keys).

Env: ESCROW_URL (default http://127.0.0.1:4000), ESCROW_AGENT_KEY (blank in dev).
To sign every request (ESCROW_AGENT_SIGNATURES on the proxy): ESCROW_AGENT_SIGNING_KEY (path to
the agent's private key) and ESCROW_AGENT_KEYID (printed when the key was registered).
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import time
from pathlib import Path
from urllib.parse import urlsplit

import httpx

BASE = os.environ.get("ESCROW_URL", "http://127.0.0.1:4000").rstrip("/")
URL = BASE + "/mcp"
KEY = os.environ.get("ESCROW_AGENT_KEY", "")
HEADERS = {"authorization": f"Bearer {KEY}"} if KEY else {}
SIGNING_KEY = os.environ.get("ESCROW_AGENT_SIGNING_KEY", "")
KEYID = os.environ.get("ESCROW_AGENT_KEYID", "")

CALLS = [
    ("get_user", {"id": 42}),
    ("list_orders", {"user_id": 42}),
    ("delete_user", {"id": 42, "reason": "GDPR erasure request #1183"}),
    ("transfer_funds", {"amount": 250, "currency": "USD", "reason": "Refund duplicate charge"}),
    ("send_email", {"to": "customer@example.com", "reason": "Notify about refund"}),
]


async def rpc(client: httpx.AsyncClient, rid: int, method: str, params: dict | None = None, session: str | None = None):
    headers = dict(HEADERS)
    if session:
        headers["mcp-session-id"] = session
    body = json.dumps({"jsonrpc": "2.0", "id": rid, "method": method, "params": params or {}}).encode()
    headers["content-type"] = "application/json"
    if SIGNING_KEY and KEYID:
        sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
        from proxy.agent_identity import sign_request

        target = urlsplit(URL)
        headers.update(sign_request(Path(SIGNING_KEY).read_bytes(), KEYID, "POST", target.netloc, target.path, body))
    r = await client.post(URL, content=body, headers=headers)
    r.raise_for_status()
    return r.json(), r.headers.get("mcp-session-id")


async def call_tool(client, rid, name, args, session):
    t0 = time.monotonic()
    print(f"→ {name} {args}")
    body, _ = await rpc(client, rid, "tools/call", {"name": name, "arguments": args}, session)
    held = body.get("result", {}).get("structuredContent", {})
    if held.get("status") == "PENDING" and held.get("action_id"):
        # Async hold mode: the proxy answered at once; poll for the outcome.
        print(f"… {name} held for review as {held['action_id']} (async mode), polling")
        while True:
            await asyncio.sleep(2)
            body, _ = await rpc(
                client,
                rid,
                "tools/call",
                {"name": "escrow_get_result", "arguments": {"action_id": held["action_id"]}},
                session,
            )
            sc = body.get("result", {}).get("structuredContent", {})
            if sc.get("status") not in ("PENDING",) and sc.get("execution") != "RUNNING":
                break
    took = time.monotonic() - t0
    if "error" in body or body.get("result", {}).get("isError"):
        if "error" not in body:
            body = {"error": {"message": body["result"]["content"][0]["text"]}}
        print(f"✗ {name} ({took:.1f}s): {body['error']['message']}")
    else:
        text = body["result"]["content"][0]["text"] if body["result"].get("content") else body["result"]
        print(f"✓ {name} ({took:.1f}s): {text}")


async def main() -> int:
    async with httpx.AsyncClient(timeout=httpx.Timeout(10.0, read=None)) as client:
        try:
            init, session = await rpc(
                client,
                1,
                "initialize",
                {
                    "protocolVersion": "2025-06-18",
                    "capabilities": {},
                    "clientInfo": {"name": "demo-agent", "version": "1"},
                },
            )
        except httpx.HTTPError as exc:
            print(f"Could not reach the proxy at {URL}: {exc}\nIs `make dev` running?")
            return 1
        print(f"connected (session {session}): {init.get('result', init)}")
        tools, _ = await rpc(client, 2, "tools/list", session=session)
        print("tools:", [t["name"] for t in tools.get("result", {}).get("tools", [])])
        print(f"\nRisky calls are now held — approve or reject them at {BASE}/ui/\n")
        await asyncio.gather(*(call_tool(client, 10 + i, n, a, session) for i, (n, a) in enumerate(CALLS)))
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
