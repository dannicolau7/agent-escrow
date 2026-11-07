"""Run with: python -m proxy  (from the agent-escrow root directory)"""
import uvicorn

uvicorn.run("proxy.main:app", host="0.0.0.0", port=4000, reload=True)
