# Deploying AgentEscrow

## 1. Docker Compose (self-hosted)

```bash
cp .env.example .env          # fill in keys, MCP_UPSTREAM_URL, GRAFANA_PASSWORD
printf '%s' "sk_..." > escrow_scrape_key.txt   # the viewer key you gave Prometheus in ESCROW_KEYS
docker compose up -d --build
```

- The proxy is published on `127.0.0.1:4000` only. Prometheus (`:9090`) and Grafana (`:3000`) are also bound to localhost; reach them through an SSH tunnel or put them behind your own auth.
- The proxy **refuses to start** in production mode if `ESCROW_KEYS`/`ESCROW_API_KEY` or `MCP_UPSTREAM_URL` is missing. Check `docker compose logs proxy`.
- Data lives in the `escrow-data` volume (`/data/escrow.db`). Back it up with `docker compose cp proxy:/data/escrow.db ./backup.db` (stop the proxy first, or use `sqlite3 .backup`).

### HTTPS

Never expose port 4000 directly to the internet: API keys travel in headers. Either:

- **Built-in Caddy profile** (automatic Let's Encrypt): set `ESCROW_DOMAIN=escrow.example.com` in `.env`, point DNS at the host, open ports 80/443, then
  ```bash
  docker compose --profile tls up -d
  ```
- **Your own load balancer / ingress** terminating TLS and forwarding to port 4000. Set `ESCROW_BIND=0.0.0.0` only if the port is not reachable from the internet.

Set `DASHBOARD_URL` to the public `https://…/ui/` address so notification links work.

## 2. Fly.io

```bash
fly launch --no-deploy                     # uses the Dockerfile
fly volumes create escrow_data --size 1
fly secrets set ESCROW_KEYS='…' MCP_UPSTREAM_URL='…' MCP_UPSTREAM_TOKEN='…' ESCROW_AUDIT_KEY='…'
```

In `fly.toml` mount the volume at `/data`, set `internal_port = 4000`, and **keep exactly one machine** (`fly scale count 1`), because of the single-instance limit below. Fly terminates TLS for you.

## 3. Connecting an agent

Point the agent's MCP client at `https://<host>/mcp` (Streamable HTTP) with header `Authorization: Bearer <agent key>`. Raise the client's request timeout above `ESCROW_HOLD_TIMEOUT` (300 s by default), or held calls will time out on the client side before a human can review them.

The upstream must speak MCP over Streamable HTTP. Servers that only support stdio need an HTTP bridge in front of them (for example `mcp-proxy` / `supergateway`).

## Operational limits — read before going to production

- **Single instance only.** Held calls wait inside the proxy process, and on startup the proxy rejects every action still marked pending. Run one container with one worker. Scaling out needs a shared store and cross-instance signalling (Postgres `LISTEN/NOTIFY` or Redis), which is not built yet.
- **Restarts reject pending actions.** Agents receive an error and must retry. Deploy during quiet periods.
- **Rotating keys** means editing `ESCROW_KEYS` and restarting (which rejects pending actions).
- **Audit retention**: rows are never deleted automatically. Export with `/api/audit/export` and archive; deleting rows will show up as a broken chain in `/api/audit/verify`.
