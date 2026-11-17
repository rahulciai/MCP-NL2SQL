"""FastMCP server manifest and tool registration."""

from __future__ import annotations

try:
    from fastmcp import FastMCP
except ImportError:  # pragma: no cover - compatibility with the MCP Python SDK
    from mcp.server.fastmcp import FastMCP

from core.prompt_builder import build_system_prompt
from tools.callback_tools import register_callback_tools
from tools.discovery_tools import register_discovery_tools
from tools.query_tools import register_query_tools


SERVER_NAME = "mmm-nl2sql"
SYSTEM_PROMPT = build_system_prompt()

try:
    mcp = FastMCP(SERVER_NAME, instructions=SYSTEM_PROMPT)
except TypeError:  # pragma: no cover - support older FastMCP releases
    mcp = FastMCP(SERVER_NAME)

register_discovery_tools(mcp)
register_query_tools(mcp)
register_callback_tools(mcp)
