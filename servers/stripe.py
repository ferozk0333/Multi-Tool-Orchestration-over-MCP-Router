"""stripe MCP server: its catalogue slice, all tools synthetic."""

from __future__ import annotations

from mcp.server.lowlevel import Server

from servers._base import build_server, run_stdio


def build() -> Server:
    # This function builds the stripe server.
    return build_server("stripe")


if __name__ == "__main__":
    run_stdio(build())
