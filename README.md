# AgentEscrow

A safety proxy that sits between an AI agent and its MCP server. Read-only tool calls go straight through; anything that changes state is held until a human approves or rejects it. Approved calls are then sent to the real MCP server and the agent gets the real result.

## How it works

```
Agent (Claude / any MCP client)
  │  POST /mcp  JSON-RPC 2.0   (agent key)
  ▼
AgentEscrow proxy  (port 4000)
  │
  ├── initialize, tools/list, resources/read, …  ──► forwarded, never held
  ├── any other non-tools/call method            ──► refused (-32601)
  │
  ├── tools/call, read-only name (get_user …)    ──► forwarded upstream, audited
  │
  └── tools/call, everything else                ──► HELD
        ├── stored in SQLite, audited, shown on the dashboard (SSE push)
        ├── Slack / webhook / PagerDuty notified in the background
        └── reviewer approves or rejects (approver key)
              ├── APPROVED → forwarded to MCP_UPSTREAM_URL, real result returned,
              │              outcome (ran / failed) recorded
              └── REJECTED or timed out → JSON-RPC error returned to the agent
```

## Quick start (local development)

```bash
pip install -r requirements-dev.txt
python -m proxy          # dev mode on http://127.0.0.1:4000
```

`python -m proxy` runs in **dev mode**: with no API keys everyone is admin, and with no `MCP_UPSTREAM_URL` approved calls return a `[mock upstream]` result. Production mode (the default everywhere else, including Docker) refuses to start without keys and an upstream.

Open http://localhost:4000 for the reviewer dashboard. Leave the key blank in dev mode.

### Dashboard (Next.js)

```bash
cd web && npm install && npm run build   # writes web/out/, served at /ui by the proxy
```

For live development against a running proxy:

```bash
cd web && NEXT_PUBLIC_API_URL=http://localhost:4000 npm run dev   # http://localhost:3000
```

### Tests

```bash
pytest
```

## Production

See **[DEPLOY.md](DEPLOY.md)** for Docker Compose, HTTPS, Fly.io and the operational limits (single instance).

## Configuration

| Variable | Purpose |
|---|---|
| `ESCROW_MODE` | `production` (default) or `dev` |
| `ESCROW_KEYS` | `label:role:key` triples separated by `\|`. Roles: `agent`, `viewer`, `approver`, `admin` |
| `ESCROW_API_KEY` | Legacy single key, treated as `admin` |
| `MCP_UPSTREAM_URL` | Streamable HTTP endpoint of the real MCP server |
| `MCP_UPSTREAM_TOKEN` | Sent upstream as `Authorization: Bearer …` (the agent's escrow key is never forwarded) |
| `MCP_UPSTREAM_TIMEOUT` | Seconds to wait for the upstream (default 30) |
| `ESCROW_HOLD_TIMEOUT` | Seconds an action waits for review before auto-rejection (default 300) |
| `ESCROW_CORS_ORIGINS` | Comma-separated browser origins allowed to call the API (default: same origin; `localhost:3000` in dev) |
| `ESCROW_AUDIT_KEY` | Secret for signing the audit hash chain (recommended) |
| `ESCROW_LOG_LEVEL` | JSON log level (default `INFO`) |
| `DASHBOARD_URL`, `SLACK_WEBHOOK_URL`, `NOTIFICATION_WEBHOOK_URL`, `PAGERDUTY_ROUTING_KEY` | Notifications |

### Roles

| Role | `/mcp` | View queue, history, audit, metrics | Approve / reject | Export & verify audit |
|---|---|---|---|---|
| agent | ✓ | | | |
| viewer | | ✓ | | |
| approver | | ✓ | ✓ | |
| admin | ✓ | ✓ | ✓ | ✓ |

Send keys as `Authorization: Bearer <key>` or `X-Api-Key: <key>`. `?api_key=` is accepted only on `/api/events` (EventSource can't send headers).

## Risk classification

Tool names are split into words (`get_user`, `get-user` and `getUser` all become `get user`). Rules in `proxy/policies.yaml`, highest precedence first:

1. `always_hold` / `always_allow` — exact tool names.
2. **Read verb first** (`get`, `list`, `search`, `read`, `describe`) and no chaining word (`and`, `then`, `or`, …) → auto-approved, unless a destructive word appears (`get_user_delete` is held).
3. Mutation word lists, first match wins:

| Risk | Example words | Category |
|---|---|---|
| CRITICAL | delete, drop, destroy, purge, remove, erase, wipe, kill | DATABASE |
| HIGH | refund, charge, transfer, pay, withdraw, purchase | FINANCIAL |
| MEDIUM | send, email, notify, publish, post | COMMUNICATION |
| MEDIUM | update, write, push, create, deploy, run, merge | GENERIC_WRITE |

4. Anything else is **held** (fail-safe).

## API reference

| Method | Path | Role | Description |
|---|---|---|---|
| `POST` | `/mcp` | agent | JSON-RPC 2.0 entry point for agents |
| `GET` | `/api/pending` | viewer | Actions waiting for review |
| `POST` | `/api/resolve` | approver | Approve or reject: `{"action_id", "decision": "APPROVED"\|"REJECTED", "rejection_reason"}` |
| `GET` | `/api/history?limit=20` | viewer | Resolved actions, with execution outcome |
| `GET` | `/api/events` | viewer | Server-Sent Events: `action_added`, `action_resolved` |
| `GET` | `/api/audit` | viewer | Audit log (filter by `action_id`, `event_type`) |
| `GET` | `/api/audit/export?format=csv\|jsonl&since=&until=` | admin | Download the audit log with hashes |
| `GET` | `/api/audit/verify` | admin | Recompute the audit hash chain |
| `GET` | `/metrics` | viewer | Prometheus metrics |
| `GET` | `/health` | public | Liveness check |
| `GET` | `/docs` | public | Swagger UI |

## Audit log

Every hold, decision, timeout, auto-approved call, forwarded read, refused method and execution outcome is written to `audit_log`. Rows are hash-chained (HMAC-SHA256 with `ESCROW_AUDIT_KEY`), so editing or deleting a row is detected by `/api/audit/verify`. Without `ESCROW_AUDIT_KEY` the chain still detects edits, but someone with database access could rebuild it.

## Reviewer dashboard

| Key | Action |
|-----|--------|
| `J` | Approve the focused action |
| `K` | Reject the focused action (prompts for reason) |
| `↑` / `↓` | Navigate between pending cards |
| `Enter` | Confirm rejection |
| `Esc` | Cancel rejection |

## Project layout

```
agent-escrow/
├── proxy/
│   ├── main.py           # FastAPI app, /mcp routing, method allowlist
│   ├── proxy_engine.py   # Hold / wait / execute logic
│   ├── forwarder.py      # Streamable HTTP client for the upstream MCP server
│   ├── routes_ui.py      # Reviewer API, audit export, metrics
│   ├── auth.py           # API keys and roles
│   ├── config.py         # Risk classifier
│   ├── policies.yaml     # Classification rules
│   ├── db.py             # SQLite storage and hash-chained audit log
│   ├── notifications.py  # Slack, webhook, PagerDuty
│   ├── events.py         # SSE fan-out
│   ├── metrics.py        # Prometheus exposition
│   ├── logs.py           # JSON logging
│   └── settings.py       # Environment settings and startup checks
├── web/                  # Next.js dashboard (web/index.html is a no-build fallback)
├── grafana/, prometheus.yml
├── Dockerfile, docker-compose.yml, .env.example, DEPLOY.md
└── test_*.py
```
