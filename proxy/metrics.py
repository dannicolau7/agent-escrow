"""Prometheus text exposition format for the /metrics endpoint.

Values are computed from the database with aggregate queries, so they stay
correct across restarts.  Review latency is exported as a real histogram.
"""

from __future__ import annotations

from typing import Any

from . import db

# Review-latency histogram buckets, in seconds.
_BUCKETS = (5, 15, 30, 60, 120, 300, 600, 1800, 3600)


def _esc(value: Any) -> str:
    """Escape a Prometheus label value."""
    return str(value).replace("\\", "\\\\").replace("\n", "\\n").replace('"', '\\"')


def collect() -> str:
    pending: int = db._query("SELECT COUNT(*) FROM pending_actions WHERE status = 'PENDING'")[0][0]

    action_rows = db._query(
        "SELECT status, risk_level, category, COUNT(*) AS n "
        "FROM pending_actions GROUP BY status, risk_level, category"
    )

    exec_rows = db._query(
        "SELECT execution_status, COUNT(*) AS n FROM pending_actions "
        "WHERE execution_status IS NOT NULL GROUP BY execution_status"
    )

    # Histogram over human-reviewed actions only (system timeouts excluded).
    secs_expr = "(julianday(resolved_at) - julianday(created_at)) * 86400.0"
    bucket_cols = ", ".join(f"SUM(CASE WHEN {secs_expr} <= {b} THEN 1 ELSE 0 END)" for b in _BUCKETS)
    where = "WHERE resolved_at IS NOT NULL AND created_at IS NOT NULL AND COALESCE(resolved_by, '') != 'system'"
    # Built only from the constants above; no user input reaches this SQL.
    sql = f"SELECT COUNT(*), COALESCE(SUM({secs_expr}), 0), {bucket_cols} FROM pending_actions {where}"  # noqa: S608
    hist = db._query(sql)[0]
    lat_count, lat_sum = hist[0], float(hist[1] or 0.0)
    bucket_counts = [int(v or 0) for v in hist[2:]]

    audit_rows = db._query("SELECT event_type, COUNT(*) AS n FROM audit_log GROUP BY event_type")

    lines: list[str] = []

    def metric(name: str, help_text: str, mtype: str, samples: list[tuple[str, Any]]) -> None:
        lines.append(f"# HELP {name} {help_text}")
        lines.append(f"# TYPE {name} {mtype}")
        for labels, value in samples:
            lines.append(f"{name}{{{labels}}} {value}" if labels else f"{name} {value}")

    metric(
        "escrow_pending_count",
        "Number of tool calls currently awaiting human review.",
        "gauge",
        [("", pending)],
    )

    metric(
        "escrow_actions_total",
        "Held tool calls, labelled by outcome and risk.",
        "counter",
        [
            (
                f'status="{_esc(r["status"])}",risk_level="{_esc(r["risk_level"])}",category="{_esc(r["category"])}"',
                r["n"],
            )
            for r in action_rows
        ],
    )

    metric(
        "escrow_executions_total",
        "Approved tool calls sent upstream, labelled by result.",
        "counter",
        [(f'result="{_esc(r["execution_status"])}"', r["n"]) for r in exec_rows],
    )

    # Histogram (cumulative buckets, as Prometheus expects).
    lines.append("# HELP escrow_review_latency_seconds Time from hold to human decision.")
    lines.append("# TYPE escrow_review_latency_seconds histogram")
    for b, n in zip(_BUCKETS, bucket_counts, strict=False):
        lines.append(f'escrow_review_latency_seconds_bucket{{le="{b}"}} {n}')
    lines.append(f'escrow_review_latency_seconds_bucket{{le="+Inf"}} {lat_count}')
    lines.append(f"escrow_review_latency_seconds_sum {lat_sum:.3f}")
    lines.append(f"escrow_review_latency_seconds_count {lat_count}")

    # Kept for existing Grafana panels.
    metric(
        "escrow_approval_latency_seconds_sum",
        "Deprecated: use escrow_review_latency_seconds_sum.",
        "gauge",
        [("", f"{lat_sum:.3f}")],
    )
    metric(
        "escrow_approval_latency_seconds_count",
        "Deprecated: use escrow_review_latency_seconds_count.",
        "gauge",
        [("", lat_count)],
    )

    metric(
        "escrow_audit_events_total",
        "Total audit log entries, labelled by event type.",
        "counter",
        [(f'event_type="{_esc(r["event_type"])}"', r["n"]) for r in audit_rows],
    )

    return "\n".join(lines) + "\n"
