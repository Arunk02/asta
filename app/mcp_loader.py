"""Load MCP toolsets from asta/mcp.json (Claude Desktop / Cursor mcpServers shape).

Differences from pydantic-ai's stock loader:
- stdio servers whose command is missing from PATH are skipped (with a note),
  so one absent binary never breaks chat;
- servers with a required-but-empty env value are skipped (e.g. github until
  GITHUB_PERSONAL_ACCESS_TOKEN is set in .env);
- url servers with "auth": "oauth" (e.g. the Atlassian remote MCP) get FastMCP's
  OAuth flow with tokens persisted under data/oauth/, and stay disabled until the
  one-time browser login (`python -m app.mcp_login <name>`) has been completed —
  so a server restart never pops a surprise browser window.
"""

from __future__ import annotations

import json
import os
import re
import shutil
from pathlib import Path
from typing import Any

from pydantic_ai.mcp import MCPToolset, StdioTransport
from pydantic_ai.toolsets import PrefixedToolset

ROOT = Path(__file__).resolve().parent.parent
CONFIG = ROOT / "mcp.json"
OAUTH_DIR = ROOT / "data" / "oauth"
OAUTH_CALLBACK_PORT = 8324

_ENV_RE = re.compile(r"\$\{(\w+)(?::-([^}]*))?\}")

MAX_RESULT_CHARS = 8000


def _truncate(value):
    """Cap oversized MCP tool results so one fat Loki response can't flood the context."""
    if isinstance(value, str) and len(value) > MAX_RESULT_CHARS:
        return value[:MAX_RESULT_CHARS] + f"\n… [truncated at {MAX_RESULT_CHARS} chars — narrow the query]"
    if isinstance(value, list):
        out, budget = [], MAX_RESULT_CHARS
        for item in value:
            text = getattr(item, "text", None)
            if isinstance(text, str):
                if budget <= 0:
                    break
                if len(text) > budget:
                    item.text = text[:budget] + "\n… [truncated — narrow the query]"
                budget -= len(getattr(item, "text", "") or "")
            out.append(item)
        return out
    return value


async def _capped_tool_call(ctx, call_tool, name, args):
    return _truncate(await call_tool(name, args))


def _expand(value: str) -> str:
    def sub(m: re.Match) -> str:
        return os.environ.get(m.group(1)) or (m.group(2) or "")
    return _ENV_RE.sub(sub, value)


def oauth_logged_in(name: str) -> bool:
    """True once mcp_login has stored tokens for this server (files, not just dirs)."""
    d = OAUTH_DIR / name
    return d.is_dir() and any(p.is_file() for p in d.rglob("*"))


def build_oauth_toolset(name: str, url: str) -> MCPToolset:
    """OAuth-authenticated HTTP toolset with tokens persisted across restarts."""
    from fastmcp.client import Client
    from fastmcp.client.auth.oauth import OAuth
    from fastmcp.client.transports import StreamableHttpTransport
    from key_value.aio.stores.filetree import FileTreeStore

    storage_dir = OAUTH_DIR / name
    storage_dir.mkdir(parents=True, exist_ok=True)
    # key_value's FileTreeStore uses the server URL inside key paths but never
    # mkdirs the nested directories (upstream bug) — pre-create what it will need.
    url_as_dirs = url.replace("://", ":/").strip("/")
    for coll in ("mcp-oauth-token", "mcp-oauth-client-info", "mcp-oauth-token-expiry"):
        (storage_dir / coll / url_as_dirs).mkdir(parents=True, exist_ok=True)
    oauth = OAuth(
        mcp_url=url,
        client_name="Asta",
        token_storage=FileTreeStore(data_directory=storage_dir),
        callback_port=OAUTH_CALLBACK_PORT,
    )
    # pydantic_ai forbids timeout kwargs alongside a pre-built client — set them here.
    client = Client(StreamableHttpTransport(url), auth=oauth, init_timeout=30, timeout=120)
    return MCPToolset(client, id=name)


def _wrap(toolset: MCPToolset, name: str, spec: dict) -> Any:
    """Prefix with the server name; optionally hide tool schemas until searched for.

    `"defer": true` in mcp.json keeps a fat server's tool definitions (e.g. grafana's 40)
    out of every request — the model discovers them via tool search only when needed.
    """
    if spec.get("defer"):
        toolset = toolset.defer_loading()
    return PrefixedToolset(toolset, name)


def load_toolsets() -> tuple[list[Any], list[dict]]:
    """Returns (toolsets, status) where status describes every configured server."""
    if not CONFIG.is_file():
        return [], []
    servers = json.loads(CONFIG.read_text()).get("mcpServers", {})
    toolsets: list[Any] = []
    status: list[dict] = []
    for name, spec in servers.items():
        entry = {"name": name, "enabled": False, "reason": ""}
        if "command" in spec:
            command = _expand(spec["command"])
            resolved = command if Path(command).is_file() else shutil.which(command)
            if not resolved:
                entry["reason"] = f"command not found: {command}"
                status.append(entry)
                continue
            env = {k: _expand(v) for k, v in (spec.get("env") or {}).items()}
            if any(v == "" for v in env.values()):
                empty = [k for k, v in env.items() if v == ""]
                entry["reason"] = f"missing env: {', '.join(empty)} (set it in .env)"
                status.append(entry)
                continue
            transport = StdioTransport(
                command=resolved,
                args=[_expand(a) for a in spec.get("args", [])],
                env={**os.environ, **env} if env else None,
                cwd=spec.get("cwd"),
            )
            ts = MCPToolset(transport, id=name, init_timeout=15, read_timeout=120,
                            process_tool_call=_capped_tool_call)
            toolsets.append(_wrap(ts, name, spec))
            entry["enabled"] = True
        elif "url" in spec:
            url = _expand(spec["url"])
            if spec.get("auth") == "oauth":
                if not oauth_logged_in(name):
                    entry["reason"] = f"needs login — run: .venv/bin/python -m app.mcp_login {name}"
                    status.append(entry)
                    continue
                toolsets.append(_wrap(build_oauth_toolset(name, url), name, spec))
            else:
                ts = MCPToolset(url, id=name, headers=spec.get("headers"),
                                init_timeout=15, read_timeout=120,
                                process_tool_call=_capped_tool_call)
                toolsets.append(_wrap(ts, name, spec))
            entry["enabled"] = True
        else:
            entry["reason"] = "needs 'command' or 'url'"
        status.append(entry)
    return toolsets, status
