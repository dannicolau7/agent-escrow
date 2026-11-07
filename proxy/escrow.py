"""
asyncio.Event-based holding logic for mutating tool calls.

Each pending call gets an asyncio.Event stored in `_pending`.
- register(call_id)  → creates and stores the event; call is now held
- resolve(call_id)   → sets the event and removes it; waiting agents unblock
- wait(call_id)      → awaits the event with a configurable timeout
"""
from __future__ import annotations

import asyncio
from typing import Dict


_pending: Dict[str, asyncio.Event] = {}


def register(call_id: str) -> asyncio.Event:
    event = asyncio.Event()
    _pending[call_id] = event
    return event


def resolve(call_id: str) -> bool:
    """Signal resolution. Returns True if the call was pending."""
    event = _pending.pop(call_id, None)
    if event is not None:
        event.set()
        return True
    return False


async def wait(call_id: str, timeout: float = 60.0) -> str:
    """
    Await resolution of a held call.
    Returns "resolved" if the event fired, or "timeout" if the deadline passed.
    """
    event = _pending.get(call_id)
    if event is None:
        return "resolved"
    try:
        await asyncio.wait_for(event.wait(), timeout=timeout)
        return "resolved"
    except asyncio.TimeoutError:
        return "timeout"


def pending_count() -> int:
    return len(_pending)
