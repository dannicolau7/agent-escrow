from __future__ import annotations

import asyncio
import os
from typing import Any, Set

import httpx

from .logs import log

# Injectable transport for tests — avoids real HTTP.
_test_transport: Any = None

_PAGERDUTY_URL = "https://events.pagerduty.com/v2/enqueue"

_RISK_EMOJI = {"CRITICAL": "🔴", "HIGH": "🟠", "MEDIUM": "🟡", "LOW": "🟢"}
_RISK_TO_PD_SEVERITY = {"CRITICAL": "critical", "HIGH": "error", "MEDIUM": "warning", "LOW": "info"}

_MAX_TEXT = 500

# Background notification tasks (kept so they aren't garbage-collected).
_tasks: Set[asyncio.Task] = set()


def _slack_escape(text: Any) -> str:
    """Escape agent-controlled text for Slack mrkdwn.

    Slack treats <…> as links/mentions (e.g. <!channel>), so &, < and > must
    be escaped.  Backticks are replaced so text can't break out of a code
    span, and the text is truncated.
    """
    s = str(text or "")
    if len(s) > _MAX_TEXT:
        s = s[:_MAX_TEXT] + "…"
    s = s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    return s.replace("`", "\u02cb")


def _client() -> httpx.AsyncClient:
    kwargs: dict = {"timeout": 5.0}
    if _test_transport is not None:
        kwargs["transport"] = _test_transport
    return httpx.AsyncClient(**kwargs)


async def _post(client: httpx.AsyncClient, channel: str, url: str, payload: dict, action_id: str) -> None:
    try:
        resp = await client.post(url, json=payload)
        if resp.status_code >= 400:
            log.warning("notification.failed", extra={"channel": channel, "action_id": action_id, "status": resp.status_code})
    except Exception as exc:  # never let a webhook break the escrow flow
        log.warning("notification.failed", extra={"channel": channel, "action_id": action_id, "error": str(exc)})


def schedule(coro) -> asyncio.Task:
    """Run a notification coroutine in the background."""
    task = asyncio.get_running_loop().create_task(coro)
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)
    return task


async def notify_action_added(
    *,
    action_id: str,
    tool_name: str,
    risk_level: str,
    category: str,
    reason: str,
) -> None:
    """Send notifications to every configured channel, concurrently."""
    dashboard_url = os.environ.get("DASHBOARD_URL", "http://localhost:4000/ui/")
    async with _client() as client:
        await asyncio.gather(
            _slack(client, action_id, tool_name, risk_level, category, reason, dashboard_url),
            _webhook(client, "action.held", action_id, tool_name, risk_level, category, reason, dashboard_url),
            _pagerduty_trigger(client, action_id, tool_name, risk_level, category, reason),
        )


async def notify_action_resolved(
    *,
    action_id: str,
    tool_name: str,
    status: str,
    resolved_by: str | None,
) -> None:
    """Close the PagerDuty incident and tell the generic webhook."""
    async with _client() as client:
        await asyncio.gather(
            _pagerduty_resolve(client, action_id),
            _webhook(
                client, "action.resolved", action_id, tool_name, None, None, None, None,
                extra={"status": status, "resolved_by": resolved_by},
            ),
        )


async def _slack(client, action_id, tool_name, risk_level, category, reason, dashboard_url):
    url = os.environ.get("SLACK_WEBHOOK_URL")
    if not url:
        return
    emoji = _RISK_EMOJI.get(risk_level, "⚠️")
    safe_tool = _slack_escape(tool_name)
    payload: dict = {
        "text": f"{emoji} Action held in escrow: `{safe_tool}` ({risk_level})",
        "blocks": [
            {
                "type": "section",
                "text": {"type": "mrkdwn", "text": f"{emoji} *Action held in escrow*"},
            },
            {
                "type": "section",
                "fields": [
                    {"type": "mrkdwn", "text": f"*Tool:*\n`{safe_tool}`"},
                    {"type": "mrkdwn", "text": f"*Risk:*\n{risk_level}"},
                    {"type": "mrkdwn", "text": f"*Category:*\n{category}"},
                    {"type": "mrkdwn", "text": f"*Action ID:*\n`{action_id}`"},
                ],
            },
        ],
    }
    if reason and reason != "No justification provided by agent.":
        payload["blocks"].append(
            {"type": "section", "text": {"type": "mrkdwn", "text": f"*Agent reason (unverified):* {_slack_escape(reason)}"}}
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
    await _post(client, "slack", url, payload, action_id)


async def _webhook(client, event, action_id, tool_name, risk_level, category, reason, dashboard_url, extra=None):
    url = os.environ.get("NOTIFICATION_WEBHOOK_URL")
    if not url:
        return
    payload = {"event": event, "action_id": action_id, "tool_name": tool_name}
    if event == "action.held":
        payload.update(
            risk_level=risk_level, category=category, reason=reason, dashboard_url=dashboard_url
        )
    payload.update(extra or {})
    await _post(client, "webhook", url, payload, action_id)


async def _pagerduty_trigger(client, action_id, tool_name, risk_level, category, reason):
    routing_key = os.environ.get("PAGERDUTY_ROUTING_KEY")
    if not routing_key:
        return
    severity = _RISK_TO_PD_SEVERITY.get(risk_level, "warning")
    payload = {
        "routing_key": routing_key,
        "event_action": "trigger",
        "dedup_key": action_id,
        "payload": {
            "summary": f"AgentEscrow: {str(tool_name)[:200]} ({risk_level}) held for review",
            "severity": severity,
            "source": "agent-escrow",
            "custom_details": {
                "tool_name": tool_name,
                "category": category,
                "action_id": action_id,
                "reason": str(reason or "")[:_MAX_TEXT],
            },
        },
    }
    await _post(client, "pagerduty", _PAGERDUTY_URL, payload, action_id)


async def _pagerduty_resolve(client, action_id):
    routing_key = os.environ.get("PAGERDUTY_ROUTING_KEY")
    if not routing_key:
        return
    payload = {"routing_key": routing_key, "event_action": "resolve", "dedup_key": action_id}
    await _post(client, "pagerduty", _PAGERDUTY_URL, payload, action_id)
