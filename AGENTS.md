# Rules for coding agents working on AgentEscrow

AgentEscrow is a **security product**: it holds risky AI-agent tool calls until a human approves them. A bug here means an agent does something nobody approved. Read this whole file before changing code.

## Workflow

- **Never commit to `main`.** Create a branch (`feat/…`, `fix/…`), open a PR, and let CI pass. `main` only changes through merged PRs.
- `git pull` / rebase on `main` before starting, so you don't build on old code.
- Small PRs: one roadmap item or one fix per PR. Check `ROADMAP.md` for priorities.
- Before every commit run:
  ```bash
  make check        # lint + type check + all tests, including end-to-end
  ```
  If `make check` fails, fix it. Don't skip, weaken or delete a test to make it pass.
- **Every bug fix gets a regression test. Every feature gets tests**, including one for the failure or unauthorized path.
- When behaviour or config changes, update `README.md`, `DEPLOY.md` and `.env.example` in the same PR.

## Invariants — never break these

Each one is covered by tests in `test_audit_fixes.py`. If a change makes one fail, the change is wrong, not the test.

1. **Listen before showing.** In `proxy/proxy_engine.py` the waiter is registered in `EXECUTION_WAITERS` *before* the action is inserted, broadcast or notified. Nothing slow (HTTP calls, notifications) may run between holding an action and waiting on it. Notifications go through `notifications.schedule(...)` (background), never `await`ed inline.
2. **The database decides.** `db.update_action_status(...)` only changes a `PENDING` row and returns whether it did; the waiting request reads the final status from the database. Never trust in-memory state alone for a decision.
3. **Never forward caller credentials upstream.** The agent's `Authorization` / `X-Api-Key` is an *escrow* key. Upstream auth comes only from `MCP_UPSTREAM_TOKEN` (`proxy/forwarder.py`).
4. **Only allowlisted methods pass through unescrowed.** `PASSTHROUGH_METHODS` in `proxy/main.py`. Anything that can change state must go through `tools/call` escrow. Don't add a method to the allowlist unless the MCP spec defines it as read-only.
5. **Fail closed.**
   - Production mode (`ESCROW_MODE=production`) is the default. It must refuse to start without keys and an upstream, and must never return fake/mock results.
   - Unknown tools are **held**, never auto-approved.
   - A new route needs an explicit role dependency (`require_viewer` / `require_approver` / `require_admin` / `require_agent`). `request.state.role` defaults to no privileges.
6. **Classifier precedence** (`proxy/config.py`): mutation rules can never be bypassed by a read prefix. Names with chaining words (`get_and_delete_x`) are held. Test new rules against both bypass names and false positives.
7. **Everything that matters is audited** through `db.insert_audit_log` (hash-chained). Don't write to `audit_log` any other way, and never update or delete audit rows.
8. **Agent-supplied text is untrusted.** Escape it in Slack/HTML/Markdown (`notifications._slack_escape`, `esc()` in `web/index.html`). Never render it as HTML.
9. **No secrets in URLs or logs.** `?api_key=` is accepted only on `/api/events`. Don't log keys, tokens or full tool arguments at INFO level.

## Project map

| Area | File |
|---|---|
| `/mcp` routing, method allowlist, middleware order | `proxy/main.py` |
| Hold / wait / execute | `proxy/proxy_engine.py` |
| Upstream MCP client (Streamable HTTP, SSE) | `proxy/forwarder.py` |
| Keys and roles | `proxy/auth.py` |
| Risk classifier + rules | `proxy/config.py`, `proxy/policies.yaml` |
| Storage, audit chain | `proxy/db.py` |
| Reviewer API, export, metrics route | `proxy/routes_ui.py` |
| Settings, startup checks | `proxy/settings.py` |
| Dashboard | `web/src/app/page.tsx`, `web/src/components/` |

## Commands

```bash
make install    # venv + Python dev deps + web deps
make dev        # proxy (dev mode) + fake MCP server, http://127.0.0.1:4000
make demo       # in another terminal: demo agent sends safe + risky calls
make test       # unit tests
make e2e        # end-to-end: real proxy + fake upstream processes
make lint       # ruff + pyright
make check      # everything CI runs
```

## Known limits (don't "fix" by accident)

- Single instance only: held calls wait in process memory, and startup rejects stale `PENDING` rows. Don't add multiple workers.
- The middleware order in `main.py` matters: auth is added first so CORS wraps it.
- Keep `--timeout-graceful-shutdown` on the uvicorn command (Dockerfile, `proxy/__main__.py`, `test_e2e.py`). Without it a restart hangs while any action is held.
