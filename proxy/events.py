from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncGenerator
from typing import Any

_subscribers: list[asyncio.Queue] = []


def broadcast(event_type: str, data: dict[str, Any] | None = None) -> None:
    """Emit an SSE event to every connected subscriber.  Called from async context."""
    payload = json.dumps({"type": event_type, **(data or {})})
    for q in list(_subscribers):
        try:
            q.put_nowait(payload)
        except asyncio.QueueFull:
            pass  # slow subscriber — drop the event rather than block


async def subscribe() -> AsyncGenerator[str, None]:
    """Yield SSE chunks until the client disconnects."""
    q: asyncio.Queue = asyncio.Queue(maxsize=50)
    _subscribers.append(q)
    try:
        while True:
            try:
                payload = await asyncio.wait_for(q.get(), timeout=15.0)
                yield f"data: {payload}\n\n"
            except TimeoutError:
                yield ": ping\n\n"  # keep-alive comment; prevents proxy timeouts
    finally:
        try:
            _subscribers.remove(q)
        except ValueError:
            pass
