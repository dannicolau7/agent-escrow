# AgentEscrow

A local-first safety proxy that intercepts autonomous AI agent tool calls and holds destructive mutations for human review before they reach downstream systems.

## How it works

```
Agent (Claude / LLM)
  │
  │  POST /mcp  JSON-RPC 2.0
  ▼
AgentEscrow Proxy  (port 4000)
  │
  ├── Safe reads (get_*, list_*, search_*)  ──► auto-approved, returned instantly
  │
  └── Mutations (delete_*, send_*, refund_*, git_push, …)
        │
        ├── held in SQLite (WAL mode)
        ├── agent HTTP connection kept open (asyncio.Event)
        └── reviewer dashboard (/ui) ──► Approve / Reject
              │
              ▼
        decision signalled back to waiting agent
```

## Quick start

### 1. Python proxy

```bash
pip install -r proxy/requirements.txt
python -m proxy          # starts on http://localhost:4000
```

Open **http://localhost:4000** — redirects to the reviewer dashboard.

### 2. Next.js UI (optional, richer keyboard-driven dashboard)

```bash
cd web
npm install
npm run build            # generates web/out/  (served by FastAPI)
```

For live development against the running proxy:

```bash
NEXT_PUBLIC_API_URL=http://localhost:4000 npm run dev
# open http://localhost:3000
```

### 3. Run tests

```bash
pytest test_agent_simulation.py test_mcp_flow.py -v
```

## Reviewer dashboard

| Key | Action |
|-----|--------|
| `J` | Approve the focused action |
| `K` | Reject the focused action (prompts for reason) |
| `↑` / `↓` | Navigate between pending cards |
| `Enter` | Confirm rejection |
| `Esc` | Cancel rejection |

## Risk tiers

| Level | Trigger | Category | Behaviour |
|-------|---------|----------|-----------|
| LOW | `get_*`, `list_*`, `read_*`, `search_*` | READ | Auto-approved |
| MEDIUM | `.*send.*`, `.*update.*`, `.*push.*` | COMMUNICATION / GENERIC_WRITE | Held |
| HIGH | `.*refund.*`, `.*charge.*`, `.*transfer.*` | FINANCIAL | Held |
| CRITICAL | `.*delete.*`, `.*drop.*`, `.*destroy.*` | DATABASE | Held |

Customize tiers in `proxy/policies.yaml`.

## API reference

| Method | Path | Description |
|--------|------|-------------|
| `POST` | `/mcp` | JSON-RPC 2.0 entry point for agents |
| `GET` | `/api/pending` | List pending actions |
| `POST` | `/api/resolve` | Approve or reject an action |
| `GET` | `/api/history` | Audit ledger (last 20 resolved) |
| `GET` | `/health` | Liveness check |
| `GET` | `/docs` | Swagger UI |

## Project layout

```
agent-escrow/
├── proxy/
│   ├── main.py          # FastAPI app, CORS, static file serving
│   ├── proxy_engine.py  # Async escrow logic (asyncio.Event per call)
│   ├── routes_ui.py     # Reviewer REST API
│   ├── db.py            # SQLite schema + WAL config
│   ├── config.py        # Risk classification engine
│   ├── policies.py      # Policy loader
│   └── policies.yaml    # Declarative risk tiers
├── web/
│   ├── src/app/page.tsx          # Keyboard-driven dashboard
│   └── src/components/           # ActionCard, HistoryTable
├── test_agent_simulation.py      # 19 REST escrow tests
└── test_mcp_flow.py              # 12 MCP path tests
```
