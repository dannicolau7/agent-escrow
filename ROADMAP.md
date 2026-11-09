# AgentEscrow — Roadmap & Tracking

_Last updated: 2026-11-08 (night)_

## Now

- [x] **Merge [PR #1](https://github.com/dannicolau7/agent-escrow/pull/1)** — audit fixes, merged.
- [x] **Merge [PR #3](https://github.com/dannicolau7/agent-escrow/pull/3)** (dev quick fixes) and **[PR #4](https://github.com/dannicolau7/agent-escrow/pull/4)** (client timeouts), merged.
- [x] **Merge [PR #2](https://github.com/dannicolau7/agent-escrow/pull/2)** — AGENTS.md rules, `make dev`/`make demo`, end-to-end tests, lint/type CI, graceful-shutdown fix, merged.
- [ ] **Sync the VS Code agent:** have it pull `main` and run `make install` before building anything else. That's how it picks up `AGENTS.md`.
- [ ] **Protect `main` on GitHub** (repo settings): require a PR with passing CI. Only the repo owner can do this.
- [ ] **Replace the old Docker stack** still running pre-fix code on ports 4000/3000/9090: `docker compose down`, then `docker compose up -d --build` with a filled-in `.env`.

## Next: test with a real agent (before building more)

So far everything is verified only by tests and a fake MCP server. Put the proxy between a real client (Claude Code / Claude Desktop) and a real MCP server (e.g. GitHub, a database) and run an actual task.

- [x] **Client timeouts** _(fixed in PR #4: `ESCROW_HOLD_MODE=wait` sends progress heartbeats, `async` answers "held, check back" plus the `escrow_get_result` tool; a live 70 s review passed in both modes. Still to try with Claude Desktop / Claude Code)_.
- [ ] **stdio servers.** Most popular MCP servers only speak stdio. Confirm an HTTP bridge (`mcp-proxy` / `supergateway`) works smoothly in front of the upstream (the client side is item 4 below).
- [ ] **Risk levels.** Check that the classifier's choices feel right on real tool names; tune `proxy/policies.yaml`.

## Build order: gaps found on 2026-11-08 (work top to bottom, one PR each)

Agreed with the owner: build these in this order. Items #7/#8, #6 and #10 from the original list are folded in where they fit.

### Pilot blockers
1. [ ] **Rules on arguments, not just tool names** (covers #8). Today `run_sql("SELECT …")` and `run_sql("DROP TABLE …")` get the same decision. Policies need argument matchers (e.g. hold SQL that isn't a `SELECT`, hold shell commands containing `rm`, hold refunds over $50) that can raise risk or auto-approve, and must fail closed on missing or odd-typed arguments.
2. [ ] **Approve / reject from Slack.** Interactive buttons, verified with Slack's signing secret, mapped to an approver key; same conditional update and audit as `/api/resolve`. Also covers most of the phone use case (#17).
3. [ ] **Several MCP servers behind one escrow.** Route by tool-name prefix (`github__create_issue`), merge `tools/list`, per-upstream tokens.
4. [ ] **`agent-escrow connect` stdio bridge.** A small command that Claude Desktop / Code launch as a local MCP server and that forwards to the proxy. Replaces the separate stdio item above.
5. [ ] **Better review context + edit before approve.** Show the agent's stated task and a plain-English summary; let a reviewer change arguments before approving, with the original and edited versions in the audit log.

### Security differentiators
6. [ ] **Tool-definition pinning.** Record each tool's name, description and schema the first time it's seen; if it changes later, hold calls to it and alert (protects against tool poisoning / "rug pulls").
7. [ ] **Two-person approval for CRITICAL** and no approving your own agent's actions (separation of duties).
8. [ ] **Kill switch.** Pause one agent or all agents at once; calls made while paused are held or refused.
9. [ ] **Hide secrets in stored arguments.** Mask tokens, passwords and keys in the database, dashboard, Slack and exports.
10. [ ] **Keys managed in the database.** Hashed keys with an admin API/page; rotate and revoke without a restart (restarts reject pending actions today).

### Cutting approval overload
11. [ ] **Temporary approvals:** "allow `create_issue` from this agent for 1 hour", audited, with expiry. Pairs with **#7 per-agent / per-environment policies**.
12. [ ] **Policy test / replay:** run last week's calls against a new policy and show what would have changed before switching it on.

### Selling pilots
13. [ ] **Landing page, 2-minute demo video, and a GitHub MCP setup guide.**

### After that
- [ ] **#6 Claims, assignment, escalation, SLA timers** (needed once there's more than one reviewer).
- [ ] **#10 Agent SDK** (Python/Node `EscrowClient`).
- [ ] **#9 Postgres + cross-instance signalling.** Biggest job; only when one instance isn't enough.

**Wait until a customer asks:** SSO, mobile app (#17), LLM risk scoring (#15), Git policy sync (#16), integrations marketplace (#18).

## Status of the original 18 items

| # | Item | Status | Gaps |
|---|---|---|---|
| 1 | Auth | Partial | API keys + roles only; no JWT / SSO (OIDC/SAML) |
| 2 | Real MCP forwarding | Done (HTTP) | stdio upstreams need a bridge |
| 3 | SSE real-time | Done | |
| 4 | Notifications | Partial | Slack, webhook, PagerDuty done; no Teams, email, OpsGenie |
| 5 | Docker + deployment | Done | Fly.io steps documented, not tried |
| 6 | Claims / escalation / SLA | Not started | |
| 7 | Per-agent / per-env policies | Not started | |
| 8 | Conditional rules | Not started | |
| 9 | PostgreSQL | Not started | |
| 10 | Agent SDK | Not started | |
| 11 | Audit trail | Mostly done | Hash chain, verify, CSV/JSONL export done; no PDF, no automatic retention |
| 12 | RBAC | Partial | Roles done; no teams, no user management, admin ≈ approver |
| 13 | Observability | Done | Metrics, histogram, JSON logs, Grafana |
| 14 | TLS | Partial | Via optional Caddy profile or your own load balancer |
| 15 | LLM risk scoring | Not started | |
| 16 | Policy-as-code Git sync | Not started | |
| 17 | Mobile app | Not started | |
| 18 | Integrations marketplace | Not started | |

## Known limits (documented in DEPLOY.md)

- **Single instance only.** Held calls wait inside the proxy process; startup rejects anything still pending.
- **Restarts and key rotation reject pending actions.**
- **Audit rows are never deleted automatically**; deleting them shows up as a broken chain.

## Done in PR #1 (security audit fixes)

- Lost approvals during slow notifications
- Agent's escrow key leaked to the upstream
- Non-`tools/call` methods bypassed escrow and audit
- Fail-open defaults (no keys = admin; missing upstream = fake success)
- Classifier bypass (`get_and_remove_user`) and false positives (`get_credit_score`)
- Forwarding: SSE responses, session headers, notifications, execution outcome
- CORS preflight, query-string keys, dev-mode `*` CORS
- Hash-chained audit log + export/verify; Slack escaping; PagerDuty resolve
- Dashboard error handling; Docker/Compose hardening; README, DEPLOY.md, `.env.example`
