"""Prometheus text exposition format for the /metrics endpoint."""
from __future__ import annotations

from typing import Any, Dict, List, Tuple

from . import db


def _fetch(conn, sql: str, params: tuple = ()) -> List[Any]:
    return conn.execute(sql, params).fetchall()


def collect() -> str:
    conn = db.get_conn()

    pending: int = conn.execute(
        "SELECT COUNT(*) FROM pending_actions WHERE status = 'PENDING'"
    ).fetchone()[0]

    action_rows = _fetch(
        conn,
        "SELECT status, risk_level, category, COUNT(*) AS n "
        "FROM pending_actions GROUP BY status, risk_level, category",
    )

    latency_rows = _fetch(
        conn,
        """
        SELECT
            (julianday(resolved_at) - julianday(created_at)) * 86400.0 AS secs
        FROM pending_actions
        WHERE resolved_at IS NOT NULL AND created_at IS NOT NULL
        """,
    )
    lat_seconds = [r[0] for r in latency_rows if r[0] is not None and r[0] >= 0]
    lat_sum = sum(lat_seconds)
    lat_count = len(lat_seconds)

    audit_rows = _fetch(
        conn,
        "SELECT event_type, COUNT(*) AS n FROM audit_log GROUP BY event_type",
    )

    lines: List[str] = []

    def metric(name: str, help_text: str, mtype: str, samples: List[Tuple[str, Any]]) -> None:
        lines.append(f"# HELP {name} {help_text}")
        lines.append(f"# TYPE {name} {mtype}")
        for labels, value in samples:
            if labels:
                lines.append(f"{name}{{{labels}}} {value}")
            else:
                lines.append(f"{name} {value}")

    metric(
        "escrow_pending_count",
        "Number of tool calls currently awaiting human review.",
        "gauge",
        [("", pending)],
    )

    action_samples = [
        (
            f'status="{r["status"]}",risk_level="{r["risk_level"]}",category="{r["category"]}"',
            r["n"],
        )
        for r in action_rows
    ]
    metric(
        "escrow_actions_total",
        "Total tool calls processed, labelled by outcome and risk.",
        "counter",
        action_samples,
    )

    metric(
        "escrow_approval_latency_seconds_sum",
        "Total seconds spent waiting for human review across all resolved actions.",
        "gauge",
        [("", f"{lat_sum:.3f}")],
    )
    metric(
        "escrow_approval_latency_seconds_count",
        "Number of resolved actions used to compute the latency sum.",
        "gauge",
        [("", lat_count)],
    )

    audit_samples = [
        (f'event_type="{r["event_type"]}"', r["n"])
        for r in audit_rows
    ]
    metric(
        "escrow_audit_events_total",
        "Total audit log entries, labelled by event type.",
        "counter",
        audit_samples,
    )

    return "\n".join(lines) + "\n"
