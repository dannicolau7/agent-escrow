# AgentEscrow — Technical Specification

## 1. System overview

AgentEscrow is an asynchronous JSON-RPC reverse proxy implementing the Model Context Protocol (MCP). It operates as a local HTTP server on port 4000 and intercepts all tool calls from autonomous agents before they reach downstream APIs. Calls classified as mutating are held in an asynchronous escrow queue until a human operator approves or rejects them via the reviewer dashboard.

## 2. MCP endpoint

**`POST /mcp`** — accepts standard JSON-RPC 2.0 payloads:

```json
{
  "jsonrpc": "2.0",
  "id": "agent-call-1049",
  "method": "tools/call",
  "params": {
    "name": "delete_record",
    "arguments": {
      "table": "users",
      "id": 42,
      "reason": "Remove inactive account per GDPR request"
    }
  }
}
```

### Fast path (safe reads)

Tools matching `get_*`, `list_*`, `search_*`, `read_*`, `describe_*` are auto-approved and return immediately:

```json
{
  "jsonrpc": "2.0",
  "id": "agent-call-1049",
  "result": {
    "content": [{ "type": "text", "text": "[Auto-Executed Safe Read] Output from get_user" }]
  }
}
```

### Escrow path (mutations)

All other tool calls are written to SQLite and held:

1. Record inserted with `status = PENDING`
2. `asyncio.Event` created and stored in `EXECUTION_WAITERS[action_id]`
3. The coroutine suspends at `await asyncio.wait_for(event.wait(), timeout=300.0)`
4. When a reviewer calls `POST /api/resolve`, the event is set and the coroutine resumes
5. Response returned to agent with approved/rejected outcome

**Timeout:** If unresolved after 300 s, returns JSON-RPC error `-32000`:
```json
{ "error": { "code": -32000, "message": "Escrow Timeout: Action was not approved by human in time." } }
```

## 3. Risk classification

Defined in `proxy/config.py` and `proxy/policies.yaml`:

| Risk | Pattern | Category | Action |
|------|---------|----------|--------|
| LOW | Prefix: `get_`, `list_`, `search_`, `read_`, `describe_` | READ | AUTO_APPROVE |
| MEDIUM | `.*send.*`, `.*update.*`, `.*write.*`, `.*push.*` | COMMUNICATION / GENERIC_WRITE | HOLD |
| HIGH | `.*refund.*`, `.*charge.*`, `.*transfer.*` | FINANCIAL | HOLD |
| CRITICAL | `.*delete.*`, `.*drop.*`, `.*destroy.*` | DATABASE | HOLD |

Default for unlisted tools: **HOLD** (fail-safe).

## 4. Database schema

**File:** `proxy/escrow.db` (SQLite, WAL mode, `synchronous=NORMAL`)

```sql
CREATE TABLE pending_actions (
    id               TEXT PRIMARY KEY,   -- "act_9f83a2c01b4e"
    rpc_id           TEXT NOT NULL,
    tool_name        TEXT NOT NULL,
    category         TEXT NOT NULL,
    risk_level       TEXT NOT NULL,
    reason           TEXT,
    raw_arguments    TEXT NOT NULL,      -- JSON text
    status           TEXT NOT NULL,      -- PENDING | APPROVED | REJECTED
    rejection_reason TEXT,
    created_at       TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    resolved_at      TEXT
);
CREATE INDEX idx_pending_actions_status ON pending_actions(status);

CREATE TABLE calls (
    call_id       TEXT PRIMARY KEY,
    agent_id      TEXT NOT NULL,
    tool_name     TEXT NOT NULL,
    tool_input    TEXT NOT NULL,
    status        TEXT NOT NULL DEFAULT 'pending',
    is_mutating   INTEGER NOT NULL DEFAULT 0,
    policy_reason TEXT,
    created_at    TEXT NOT NULL,
    resolved_at   TEXT,
    resolved_by   TEXT
);
CREATE INDEX idx_calls_status ON calls(status);
```

## 5. Reviewer API

### `GET /api/pending`

Returns array of `pending_actions` rows with `status = PENDING`, ordered by `created_at ASC`. `raw_arguments` is deserialized from JSON text to object.

### `POST /api/resolve`

```json
{ "action_id": "act_9f83a2c01b4e", "decision": "APPROVED", "rejection_reason": null }
```

- `decision` must be `APPROVED` or `REJECTED` (400 otherwise)
- Returns 404 if action not found in `EXECUTION_WAITERS` (already resolved or timed out)
- Sets the `asyncio.Event`, which unblocks the waiting `/mcp` coroutine
- DB update (`status`, `resolved_at`, `rejection_reason`) happens inside the unblocked coroutine

### `GET /api/history?limit=20`

Returns up to `limit` resolved actions (`status != PENDING`), ordered by `resolved_at DESC`.

## 6. Concurrency model

```
Request thread (async)          Reviewer thread (async)
─────────────────────           ───────────────────────
POST /mcp
  classify tool
  insert_action(PENDING)
  event = asyncio.Event()
  EXECUTION_WAITERS[id] = event
  await event.wait()   ◄────── POST /api/resolve
       │                          entry["decision"] = "APPROVED"
       │                          event.set()
       ▼
  update_action(APPROVED)
  return JSON-RPC result
```

Both `/mcp` and `/api/resolve` are `async def` handlers running on the same asyncio event loop. `event.set()` is safe because it is called from within the event loop, not from a thread.

## 7. Static file serving

FastAPI serves the reviewer UI via `StaticFiles`:

- If `web/out/` exists (Next.js build output): served from there
- Fallback: `web/index.html` (plain HTML/JS, no build required)
- Mounted at `/ui`; root `/` redirects to `/ui/`

## 8. Non-functional properties

| Property | Value |
|----------|-------|
| Fast-path latency overhead | < 5 ms (no I/O) |
| Persistence | SQLite WAL — no blocking on concurrent reads |
| External dependencies | None (runs entirely on localhost) |
| Escrow timeout | 300 s (configurable via `process_mcp_call(timeout=…)`) |
| Test coverage | 31 tests (19 REST + 12 MCP), all in-process via `ASGITransport` |
