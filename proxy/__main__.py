"""Run with: python -m proxy  (from the agent-escrow root directory)

Local development entry point: defaults to ESCROW_MODE=dev and listens on
localhost only.  Set ESCROW_RELOAD=1 for auto-reload — but note that a reload
restarts the server and rejects every action still waiting for review.
For production use the Docker image (uvicorn, ESCROW_MODE=production).
"""
import os

import uvicorn

os.environ.setdefault("ESCROW_MODE", "dev")

uvicorn.run(
    "proxy.main:app",
    host=os.environ.get("ESCROW_HOST", "127.0.0.1"),
    port=int(os.environ.get("ESCROW_PORT", "4000")),
    reload=os.environ.get("ESCROW_RELOAD") == "1",
)
