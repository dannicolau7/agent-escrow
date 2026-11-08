from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Optional

from fastapi import HTTPException, Request
from fastapi.responses import JSONResponse

_PUBLIC_PREFIXES = ("/ui", "/health", "/docs", "/openapi.json", "/redoc")
_VALID_ROLES = ("agent", "viewer", "approver", "admin")


@dataclass
class _KeyInfo:
    label: str
    role: str


def _load_keys() -> dict[str, _KeyInfo]:
    """Parse ESCROW_KEYS (pipe-separated label:role:key triples) and the
    legacy ESCROW_API_KEY (treated as admin) into a lookup table."""
    keys: dict[str, _KeyInfo] = {}

    for entry in os.environ.get("ESCROW_KEYS", "").split("|"):
        entry = entry.strip()
        if not entry:
            continue
        parts = entry.split(":", 2)
        if len(parts) == 3:
            label, role, key = (p.strip() for p in parts)
            if role in _VALID_ROLES and key:
                keys[key] = _KeyInfo(label=label, role=role)

    legacy = os.environ.get("ESCROW_API_KEY", "").strip()
    if legacy and legacy not in keys:
        keys[legacy] = _KeyInfo(label="legacy-admin", role="admin")

    return keys


def _extract_token(request: Request) -> Optional[str]:
    """Read the API key from headers or (for EventSource) from ?api_key=."""
    auth = request.headers.get("authorization", "")
    if auth.lower().startswith("bearer "):
        return auth[7:].strip() or None
    hdr = request.headers.get("x-api-key", "").strip()
    if hdr:
        return hdr
    qp = request.query_params.get("api_key", "").strip()
    return qp or None


async def api_key_middleware(request: Request, call_next):
    """Authenticate every request and attach (role, key_label) to request.state.

    Precedence:
      always-public paths → role="public"
      no keys configured  → role="admin"  (dev mode)
      valid token         → role from key table
      missing token       → 401
      unknown token       → 403
    """
    path = request.url.path

    if path == "/" or any(path.startswith(p) for p in _PUBLIC_PREFIXES):
        request.state.role = "public"
        request.state.key_label = ""
        return await call_next(request)

    keys = _load_keys()
    if not keys:
        request.state.role = "admin"
        request.state.key_label = "dev"
        return await call_next(request)

    token = _extract_token(request)
    if not token:
        return JSONResponse(
            status_code=401,
            content={"detail": "Missing API key — use Authorization: Bearer <key> or X-Api-Key: <key>"},
        )

    info = keys.get(token)
    if not info:
        return JSONResponse(status_code=403, content={"detail": "Invalid API key"})

    request.state.role = info.role
    request.state.key_label = info.label
    return await call_next(request)


# ---------------------------------------------------------------------------
# FastAPI dependencies for route-level role enforcement
# ---------------------------------------------------------------------------

def _role(request: Request) -> str:
    return getattr(request.state, "role", "admin")


def require_viewer(request: Request) -> None:
    """viewer, approver, or admin."""
    if _role(request) not in ("viewer", "approver", "admin"):
        raise HTTPException(status_code=403, detail="viewer role or higher required")


def require_approver(request: Request) -> None:
    """approver or admin."""
    if _role(request) not in ("approver", "admin"):
        raise HTTPException(status_code=403, detail="approver role or higher required")


def require_agent(request: Request) -> None:
    """agent or admin.  Human reviewer roles are intentionally excluded so a
    compromised reviewer key cannot trigger tool calls."""
    if _role(request) not in ("agent", "admin"):
        raise HTTPException(status_code=403, detail="agent or admin role required")
