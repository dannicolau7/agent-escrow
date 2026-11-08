from __future__ import annotations

import os

from fastapi import Request
from fastapi.responses import JSONResponse


# Paths that never require authentication (or handle it themselves).
# /api/events is excluded because EventSource cannot set headers —
# the endpoint validates the api_key query parameter instead.
_PUBLIC_PREFIXES = ("/ui", "/health", "/docs", "/openapi.json", "/redoc", "/api/events")


async def api_key_middleware(request: Request, call_next):
    """HTTP middleware that enforces ESCROW_API_KEY when the env var is set.

    Dev mode (key not set): all requests pass through unchanged.
    Production (key set): every non-public request must supply the key via
      Authorization: Bearer <key>  or  X-Api-Key: <key>
    """
    key = os.environ.get("ESCROW_API_KEY")
    if not key:
        return await call_next(request)

    path = request.url.path
    if any(path.startswith(p) for p in _PUBLIC_PREFIXES) or path == "/":
        return await call_next(request)

    auth_header = request.headers.get("authorization", "")
    api_key_header = request.headers.get("x-api-key", "")

    if auth_header.lower().startswith("bearer "):
        token = auth_header[7:].strip()
    elif api_key_header:
        token = api_key_header.strip()
    else:
        return JSONResponse(
            status_code=401,
            content={"detail": "Missing API key — supply Authorization: Bearer <key> or X-Api-Key: <key>"},
        )

    if token != key:
        return JSONResponse(status_code=403, content={"detail": "Invalid API key"})

    return await call_next(request)
