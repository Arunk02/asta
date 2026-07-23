"""One-time OAuth login for remote MCP servers (e.g. atlassian).

Usage:
    .venv/bin/python -m app.mcp_login atlassian

Opens your browser for the provider's login/consent, then stores tokens under
data/oauth/<name>/ so the Asta server can use the connection headlessly from
then on. Restart Asta after a successful login.
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")

from . import mcp_loader  # noqa: E402


async def login(name: str) -> int:
    servers = json.loads(mcp_loader.CONFIG.read_text()).get("mcpServers", {})
    spec = servers.get(name)
    if not spec or "url" not in spec or spec.get("auth") != "oauth":
        print(f"'{name}' is not an oauth url server in mcp.json. OAuth servers: "
              + ", ".join(n for n, s in servers.items() if s.get("auth") == "oauth"))
        return 1
    url = mcp_loader._expand(spec["url"])
    print(f"Connecting to {url} — your browser will open for login/consent…")
    toolset = mcp_loader.build_oauth_toolset(name, url)
    async with toolset:
        tools = await toolset.list_tools()
    print(f"✅ Logged in. {len(tools)} tools available from '{name}':")
    for t in tools[:15]:
        print("  -", t.name)
    if len(tools) > 15:
        print(f"  … and {len(tools) - 15} more")
    print("\nTokens saved under data/oauth/ — restart Asta to activate.")
    return 0


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print(__doc__)
        sys.exit(1)
    sys.exit(asyncio.run(login(sys.argv[1])))
