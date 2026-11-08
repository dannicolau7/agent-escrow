"""Runtime settings read from the environment.

ESCROW_MODE
    "production" (default) — refuses to start without API keys and an
    upstream MCP server; never fakes tool results.
    "dev" — no keys means open access, and a missing MCP_UPSTREAM_URL returns
    mock results.  `python -m proxy` and the test suite set this.
"""

from __future__ import annotations

import os


def mode() -> str:
    return os.environ.get("ESCROW_MODE", "production").strip().lower() or "production"


def is_dev() -> bool:
    return mode() == "dev"


def hold_timeout() -> float:
    """Seconds an action waits for a human before it is auto-rejected."""
    try:
        return float(os.environ.get("ESCROW_HOLD_TIMEOUT", "300"))
    except ValueError:
        return 300.0


def hold_mode() -> str:
    """How a held tools/call is answered.

    "wait"  (default) keep the request open until a human decides.  Clients
            that accept SSE get a stream with progress notifications every
            ESCROW_HEARTBEAT_SECONDS, so clients that reset their timeout on
            progress stay connected.
    "async" answer at once with "held as act_…"; the agent calls the
            escrow_get_result tool to get the outcome.  Works with any client
            timeout.
    """
    mode = os.environ.get("ESCROW_HOLD_MODE", "wait").strip().lower()
    return mode if mode in ("wait", "async") else "wait"


def heartbeat_seconds() -> float:
    try:
        return max(1.0, float(os.environ.get("ESCROW_HEARTBEAT_SECONDS", "15")))
    except ValueError:
        return 15.0


def cors_origins() -> list[str]:
    """Origins allowed to call the API from a browser.

    ESCROW_CORS_ORIGINS is a comma-separated list.  Unset means the local
    Next.js dev server in dev mode, and same-origin only (the bundled
    dashboard) in production.  Never "*" by default: any website you visit
    could otherwise call a proxy running on your machine.
    """
    raw = os.environ.get("ESCROW_CORS_ORIGINS")
    if raw is None:
        return ["http://localhost:3000", "http://127.0.0.1:3000"] if is_dev() else []
    return [o.strip() for o in raw.split(",") if o.strip()]


class ConfigError(RuntimeError):
    pass


def validate_startup() -> None:
    """Fail fast on unsafe production configuration."""
    if is_dev():
        return
    from .auth import load_keys  # local import to avoid a cycle

    problems = []
    if not load_keys():
        problems.append("no API keys configured (set ESCROW_KEYS or ESCROW_API_KEY)")
    if not os.environ.get("MCP_UPSTREAM_URL"):
        problems.append("MCP_UPSTREAM_URL is not set, so approved actions could not run")
    if problems:
        raise ConfigError(
            "Refusing to start in production mode: "
            + "; ".join(problems)
            + ".  Set ESCROW_MODE=dev for local development."
        )
