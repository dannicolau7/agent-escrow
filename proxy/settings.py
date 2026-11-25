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


AUTO_APPROVE_MODES = ("verbs", "listed")


def auto_approve_mode() -> str:
    """Which tool calls may run without a person (ESCROW_AUTO_APPROVE).

    "verbs"   (default) names on `always_allow`, plus names that start with a read verb
              ("get_user"). Convenient, but it trusts tool names to mean what they say.
    "listed"  only names on `always_allow`. Everything else is held, whatever it is called.

    An unrecognised value is treated as "listed": when in doubt, hold.
    """
    mode = os.environ.get("ESCROW_AUTO_APPROVE", "verbs").strip().lower()
    return mode if mode in AUTO_APPROVE_MODES else "listed"


AGENT_SIGNATURE_MODES = ("off", "warn", "required")


def agent_signature_mode() -> str:
    """Whether agents must sign their requests (ESCROW_AGENT_SIGNATURES).

    "off"       (default) signatures are ignored.
    "warn"      a missing or bad signature is recorded and reported; the call goes on.
    "required"  a missing or bad signature is refused.

    An unrecognised value is treated as "required": when in doubt, refuse. Startup refuses
    such a value outright, so this only matters if the setting changes underneath a
    running process.
    """
    mode = os.environ.get("ESCROW_AGENT_SIGNATURES", "off").strip().lower()
    return mode if mode in AGENT_SIGNATURE_MODES else "required"


def public_authority() -> str:
    """The host agents sign, when it is not the Host header this process sees.

    Behind a proxy that rewrites the Host header, set ESCROW_PUBLIC_AUTHORITY to the host
    (and port, if not the default) that agents call.
    """
    return os.environ.get("ESCROW_PUBLIC_AUTHORITY", "").strip().lower()


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
    from . import checkpoint  # local import to avoid a cycle

    # In every mode: a configured signing key that cannot be loaded is a mistake to fix now,
    # not when an auditor asks for a checkpoint.
    try:
        checkpoint.validate_startup()
    except checkpoint.CheckpointError as exc:
        raise ConfigError(f"Refusing to start: {exc}.") from exc
    raw_mode = os.environ.get("ESCROW_AUTO_APPROVE", "verbs").strip().lower()
    if raw_mode not in AUTO_APPROVE_MODES:
        raise ConfigError(
            f"Refusing to start: ESCROW_AUTO_APPROVE must be one of {', '.join(AUTO_APPROVE_MODES)}, not {raw_mode!r}."
        )
    raw_signatures = os.environ.get("ESCROW_AGENT_SIGNATURES", "off").strip().lower()
    if raw_signatures not in AGENT_SIGNATURE_MODES:
        raise ConfigError(
            "Refusing to start: ESCROW_AGENT_SIGNATURES must be one of "
            f"{', '.join(AGENT_SIGNATURE_MODES)}, not {raw_signatures!r}."
        )
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
