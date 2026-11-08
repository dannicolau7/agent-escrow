# AgentEscrow — Roadmap & Tracking

_Last updated: 2026-11-08_

## Now

- [ ] **Merge [PR #1](https://github.com/dannicolau7/agent-escrow/pull/1)** (`fix/audit-findings`) once CI is green. Auto-fix is on for it.
- [ ] **Sync the VS Code agent:** have it pull `main` after the merge, before building anything else. Otherwise it builds on old code and conflicts in `proxy/`.

## Next: test with a real agent (before building more)

So far everything is verified only by tests and a fake MCP server. Put the proxy between a real client (Claude Code / Claude Desktop) and a real MCP server (e.g. GitHub, a database) and run an actual task.

- [ ] **Client timeouts.** Most MCP clients give up after ~60 s, but a human review can take up to `ESCROW_HOLD_TIMEOUT` (300 s). If the client gives up first, the agent fails even when someone approves. This is the problem most likely to break a demo. The fix may change the design (e.g. return "held, check back" instead of keeping the request open).
- [ ] **stdio servers.** Most popular MCP servers only speak stdio. Confirm an HTTP bridge (`mcp-proxy` / `supergateway`) works smoothly.
- [ ] **Risk levels.** Check that the classifier's choices feel right on real tool names; tune `proxy/policies.yaml`.

## Build order after that

1. [ ] **#7 Per-agent / per-environment policies** + **#8 conditional rules** (e.g. auto-approve refunds under $50, rate limits per agent). This is what buyers see in a demo, and without it reviewers drown in approvals.
2. [ ] **#6 Claims, assignment, escalation, SLA timers.** Needed as soon as there's more than one reviewer; pairs with the Slack notifications.
3. [ ] **#10 Agent SDK** (Python/Node `EscrowClient`). Small; makes onboarding a pilot customer a 5-minute job.
4. [ ] **#9 Postgres + cross-instance signalling** (Postgres `LISTEN/NOTIFY` or Redis). Biggest job; only needed when one instance isn't enough.

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
