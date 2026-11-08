"""Test-wide defaults: run the proxy in dev mode with no keys or upstream."""

import os

os.environ["ESCROW_MODE"] = "dev"
for var in (
    "ESCROW_KEYS",
    "ESCROW_API_KEY",
    "MCP_UPSTREAM_URL",
    "MCP_UPSTREAM_TOKEN",
    "SLACK_WEBHOOK_URL",
    "NOTIFICATION_WEBHOOK_URL",
    "PAGERDUTY_ROUTING_KEY",
    "ESCROW_CORS_ORIGINS",
    "ESCROW_AUDIT_KEY",
):
    os.environ.pop(var, None)
