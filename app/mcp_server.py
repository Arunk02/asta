"""Asta's own capabilities, served over MCP.

This is the third and last consumer of the capability registry, and the one that
retires the awkward parts of the other two. A CLI brain that speaks MCP calls
`resolve_context` directly instead of being taught a curl line for it, which
means:

  - the regex pre-fetchers (`_teams_activity_context`, `_outlook_context`) exist
    only because the CLI could not reliably shell back into Asta; with MCP they
    are dead weight;
  - the first-turn context shrinks to rules and flow, because the tool list
    arrives as real schemas;
  - one description, from one docstring, reaches chat, CLI and MCP alike.

Run it:

    .venv/bin/python -m app.mcp_server            # stdio, for a CLI to spawn
    .venv/bin/python -m app.mcp_server --print-config

The config is printed rather than installed. Writing into ~/.copilot or a repo's
.mcp.json is a change to Arun's tools, not to Asta, and it is his to make.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import sys
from pathlib import Path

from . import capabilities

ROOT = Path(__file__).resolve().parent.parent
SERVER_NAME = "asta"


def build_server():
    """One MCP tool per capability, wired straight to the same function chat calls."""
    from mcp.server.fastmcp import FastMCP

    server = FastMCP(SERVER_NAME)
    for cap in capabilities.registry().values():
        server.add_tool(cap.fn, name=cap.name, description=_describe(cap))
    return server


def _describe(cap) -> str:
    """Description plus the capability's hard rule.

    The rule must travel with the tool: an MCP client shows the description and
    nothing else, so a rule left in Asta's prompt would simply not exist here —
    and these are the rules whose absence costs something.
    """
    text = cap.description
    if cap.note:
        text += f"\n\nRULE: {cap.note}"
    if cap.write:
        text += "\n\nThis changes something outside Asta. Confirm with Arun before calling it."
    return text


def config_entry() -> dict:
    """The mcpServers entry a CLI needs to spawn this server."""
    return {
        "mcpServers": {
            SERVER_NAME: {
                "command": str(ROOT / ".venv" / "bin" / "python"),
                "args": ["-m", "app.mcp_server"],
                "cwd": str(ROOT),
            }
        }
    }


def tool_manifest() -> list[dict]:
    """What this server exposes — used by tests and by --list."""
    return [{"name": c.name, "group": c.group, "write": c.write,
             "async": inspect.iscoroutinefunction(c.fn),
             "description": _describe(c)}
            for c in capabilities.registry().values()]


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if "--print-config" in args:
        print(json.dumps(config_entry(), indent=2))
        return 0
    if "--list" in args:
        for t in tool_manifest():
            print(f"{t['name']:24s} {t['group']:10s} {'write' if t['write'] else 'read':5s}")
        return 0
    build_server().run(transport="stdio")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
