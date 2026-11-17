"""Entrypoint for the combined FastAPI chat API and FastMCP server."""

from __future__ import annotations

import os

import uvicorn

from api.app import app
from core.server_manifest import mcp
from core.settings import AppSettings


if __name__ == "__main__":
    transport = os.getenv("MCP_TRANSPORT", "streamable-http").lower()
    if transport == "stdio":
        # MCP stdio owns stdout; do not print diagnostics in this mode.
        mcp.run()
    else:
        runtime_settings = AppSettings.from_env()
        uvicorn.run(
            app,
            host=runtime_settings.mcp_host,
            port=runtime_settings.mcp_port,
        )
