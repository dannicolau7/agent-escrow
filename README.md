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
make install     # .venv, dev deps, web deps, git hooks
make dev         # proxy (dev mode) + fake MCP server on http://127.0.0.1:4000
make demo        # in a second terminal: an agent sends safe and risky calls
```

Approve or reject the held calls at http://127.0.0.1:4000/ui/ (`J` / `K`). `make help` lists every command; `make check` runs what CI runs. Coding agents must follow [AGENTS.md](AGENTS.md).

Without make: `pip install -r requirements-dev.txt && python -m proxy`.

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
make test        # unit tests
make e2e         # end-to-end: real proxy + fake MCP server processes
make lint        # ruff + pyright
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
| `ESCROW_HOLD_MODE` | `wait` (default): keep the agent's request open until a human decides. `async`: answer at once with "held as act_…"; the agent calls the `escrow_get_result` tool for the outcome. See [Client timeouts](#client-timeouts) |
| `ESCROW_HEARTBEAT_SECONDS` | Wait mode: interval of progress heartbeats on streamed responses (default 15) |
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

### Argument rules

Tool names aren't enough: `run_sql("SELECT 1")` and `run_sql("DROP TABLE users")` share a name. `argument_rules` in `proxy/policies.yaml` look inside the call's `arguments`:

```yaml
argument_rules:
  - name: large-refund
    action: hold              # hold | allow
    risk_level: HIGH
    category: FINANCIAL
    tools: [refund_payment]   # exact names, and/or tool_pattern: "regex"
    when:                     # every condition must match
      amount: {gt: 50}
```

- **Conditions** are keyed by argument path (`query`, `options.force`, `items.0.id`) or `"*"` (any key or value anywhere; hold rules only). Operators: `regex`, `not_regex`, `equals`, `in`, `gt`, `gte`, `lt`, `lte`, `exists`. String comparisons ignore case and surrounding spaces.
- **`hold` rules** hold the call and can only *raise* its risk. They beat `always_allow` and `allow` rules. If the argument is present but can't be checked (sent as `"75"`, `[75]`, `"nan"`, a huge string…), the rule still holds, so an agent can't dodge it with odd types.
- **`allow` rules** auto-approve only when every argument is present with the expected type, and never override `always_hold`. They must name their tools.
- A missing argument matches neither, except `exists: false`.
- The rule that decided is shown in the audit log (`detail.rule`).
- Regexes run on text the agent controls (up to 100,000 characters per value). Avoid patterns that backtrack badly, such as `[^;]*;?\s*\Z` or an unbounded `.*` between two words; use bounded repeats like `{0,500}`. `test_argument_rules.py` times the shipped rules against hostile input.
- A mistake in the file (unknown key or operator, bad regex, an allow rule without tools) **stops the proxy from starting**, rather than silently skipping the rule.

Shipped rules hold destructive SQL (`DROP`, `DELETE`, … → CRITICAL; `INSERT`, `UPDATE`, … → HIGH) on SQL/query/database tools, and destructive shell commands (`rm -rf`, `curl … | sh`, `mkfs`, `shutdown`, …) on shell/command tools. Commented examples cover auto-approving read-only SQL and refunds under $50. A regex can't prove SQL is harmless, so pair a SELECT allow rule with a read-only database user.

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

## Client timeouts

Many MCP clients give up on a request after about 60 seconds; a human review can take minutes. Two ways to handle it:

- **`wait` mode (default).** If the client accepts `text/event-stream` (Streamable HTTP clients do), a held call is answered with an SSE stream. While it waits, the proxy sends a `notifications/progress` message every `ESCROW_HEARTBEAT_SECONDS` if the request carried `_meta.progressToken` (or an SSE comment otherwise), then the final result. Clients that reset their timeout on progress (e.g. the MCP TypeScript SDK's `resetTimeoutOnProgress`) stay connected. If the client disconnects, the action is withdrawn (REJECTED, "Agent disconnected") so it can't run later with nobody waiting.
- **`async` mode** (`ESCROW_HOLD_MODE=async`). Works with any client. A held call returns immediately (`isError: false`) with `structuredContent: {"status": "PENDING", "action_id": …}` and text telling the model it has **not** run yet. `tools/list` gains an `escrow_get_result` tool; the agent calls it with the `action_id` to get `PENDING`, `REJECTED`, or the tool's real result once approved. Approved calls run in the background, reusing the original MCP session. An agent can only read results of its own calls. Unreviewed actions expire after `ESCROW_HOLD_TIMEOUT`.

Use `async` when your client's request timeout is shorter than your reviewers' typical response time.

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
│   ├── arg_rules.py      # Argument rules (hold/allow on arguments)
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
