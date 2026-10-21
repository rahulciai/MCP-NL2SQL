"""Claude Desktop entrypoint for the MMM NL2SQL MCP server."""

from core.server_manifest import mcp


if __name__ == "__main__":
    # MCP stdio owns stdout. Do not print diagnostics here: Claude Desktop
    # interprets stdout as protocol traffic.
    mcp.run()
