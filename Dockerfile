# ── Stage 1: build the Next.js dashboard ────────────────────────────────────
FROM node:20-alpine AS ui-builder
WORKDIR /app/web
COPY web/package*.json ./
RUN npm ci --cache /tmp/npm-cache
COPY web/ .
RUN npm run build

# ── Stage 2: Python runtime ──────────────────────────────────────────────────
FROM python:3.12-slim

# Non-root user
RUN useradd -m -u 1001 escrow

WORKDIR /app

# Python dependencies
COPY proxy/requirements.txt ./proxy/requirements.txt
RUN pip install --no-cache-dir -r proxy/requirements.txt

# Application code
COPY proxy/ ./proxy/
# Pre-built UI
COPY --from=ui-builder /app/web/out ./web/out

# Data directory for SQLite (overridable with DB_PATH env var)
RUN mkdir -p /data && chown escrow /data
ENV DB_PATH=/data/escrow.db \
    ESCROW_MODE=production \
    PYTHONUNBUFFERED=1

USER escrow

EXPOSE 4000

# --proxy-headers so client IPs in the audit log are correct behind a TLS proxy.
# One worker only: held actions live in this process (see DEPLOY.md).
CMD ["uvicorn", "proxy.main:app", "--host", "0.0.0.0", "--port", "4000", "--workers", "1", "--proxy-headers"]
