"""Structured JSON logging.

Every log line is one JSON object on stdout, e.g.
{"ts": "...", "level": "INFO", "logger": "escrow", "event": "action.held", "action_id": "act_…"}
Set ESCROW_LOG_LEVEL to change verbosity (default INFO).
"""

from __future__ import annotations

import json
import logging
import os
import sys
from datetime import UTC, datetime
from typing import Any

_RESERVED = set(vars(logging.makeLogRecord({})).keys()) | {"message", "asctime"}


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        out: dict[str, Any] = {
            "ts": datetime.fromtimestamp(record.created, UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "event": record.getMessage(),
        }
        for key, value in record.__dict__.items():
            if key not in _RESERVED and not key.startswith("_"):
                out[key] = value
        if record.exc_info:
            out["exc"] = self.formatException(record.exc_info)
        return json.dumps(out, default=str)


_configured = False


def setup() -> None:
    global _configured
    if _configured:
        return
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger("escrow")
    root.handlers[:] = [handler]
    root.setLevel(os.environ.get("ESCROW_LOG_LEVEL", "INFO").upper())
    root.propagate = False
    _configured = True


log = logging.getLogger("escrow")
