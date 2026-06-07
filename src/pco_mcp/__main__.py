"""Entry point: run the PCO MCP server over stdio.

    python -m pco_mcp      # or, via the installed script: pco-mcp
"""

from __future__ import annotations

from .server import mcp


def main() -> None:
    # FastMCP.run() defaults to stdio transport, which is what local MCP
    # clients (Claude Code / Claude Desktop) spawn and talk to.
    mcp.run()


if __name__ == "__main__":
    main()
