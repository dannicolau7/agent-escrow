from __future__ import annotations

import os
from typing import Any

import httpx

# Injectable transport for tests — avoids real HTTP.
_test_transport: Any = None

_PAGERDUTY_URL = "https://events.pagerduty.com/v2/enqueue"

_RISK_EMOJI = {"CRITICAL": "🔴", "HIGH": "🟠", "MEDIUM": "🟡", "LOW": "🟢"}
_RISK_TO_PD_SEVERITY = {"CRITICAL": "critical", "HIGH": "error", "MEDIUM": "warning", "LOW": "info"}


async def notify_action_added(
    *,
    action_id: str,
    tool_name: str,
    risk_level: str,
    category: str,
    reason: str,
) -> None:
    """Send notifications to every configured channel.  Errors are swallowed so
    a slow or mis-configured webhook never blocks the escrow flow."""
    dashboard_url = os.environ.get("DASHBOARD_URL", "http://localhost:4000/ui/")
    client_kwargs: dict = {}
    if _test_transport is not None:
        client_kwargs["transport"] = _test_transport

    async with httpx.AsyncClient(**client_kwargs, timeout=5.0) as client:
        await _slack(client, action_id, tool_name, risk_level, category, reason, dashboard_url)
        await _webhook(client, action_id, tool_name, risk_level, category, reason, dashboard_url)
        await _pagerduty(client, action_id, tool_name, risk_level, category, reason)


async def _slack(client, action_id, tool_name, risk_level, category, reason, dashboard_url):
    url = os.environ.get("SLACK_WEBHOOK_URL")
    if not url:
        return
    emoji = _RISK_EMOJI.get(risk_level, "⚠️")
    payload: dict = {
        "text": f"{emoji} Action held in escrow: `{tool_name}` ({risk_level})",
        "blocks": [
            {
                "type": "section",
                "text": {"type": "mrkdwn", "text": f"{emoji} *Action held in escrow*"},
            },
            {
                "type": "section",
                "fields": [
                    {"type": "mrkdwn", "text": f"*Tool:*\n`{tool_name}`"},
                    {"type": "mrkdwn", "text": f"*Risk:*\n{risk_level}"},
                    {"type": "mrkdwn", "text": f"*Category:*\n{category}"},
                    {"type": "mrkdwn", "text": f"*Action ID:*\n`{action_id}`"},
                ],
            },
        ],
    }
    if reason and reason != "No justification provided by agent.":
        payload["blocks"].append(
            {"type": "section", "text": {"type": "mrkdwn", "text": f"*Agent reason:* _{reason}_"}}
        )
    payload["blocks"].append(
        {
            "type": "actions",
            "elements": [
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": "Review Now"},
                    "url": dashboard_url,
                    "style": "danger" if risk_level in ("CRITICAL", "HIGH") else "primary",
                }
            ],
        }
    )
    try:
        await client.post(url, json=payload)
    except Exception:
        pass


async def _webhook(client, action_id, tool_name, risk_level, category, reason, dashboard_url):
    url = os.environ.get("NOTIFICATION_WEBHOOK_URL")
    if not url:
        return
    payload = {
        "action_id": action_id,
        "tool_name": tool_name,
        "risk_level": risk_level,
        "category": category,
        "reason": reason,
        "dashboard_url": dashboard_url,
    }
    try:
        await client.post(url, json=payload)
    except Exception:
        pass


async def _pagerduty(client, action_id, tool_name, risk_level, category, reason):
    routing_key = os.environ.get("PAGERDUTY_ROUTING_KEY")
    if not routing_key:
        return
    severity = _RISK_TO_PD_SEVERITY.get(risk_level, "warning")
    payload = {
        "routing_key": routing_key,
        "event_action": "trigger",
        "dedup_key": action_id,
        "payload": {
            "summary": f"AgentEscrow: {tool_name} ({risk_level}) held for review",
            "severity": severity,
            "source": "agent-escrow",
            "custom_details": {
                "tool_name": tool_name,
                "category": category,
                "action_id": action_id,
                "reason": reason,
            },
        },
    }
    try:
        await client.post(_PAGERDUTY_URL, json=payload)
    except Exception:
        pass
