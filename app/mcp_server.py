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

import contextlib
import inspect
import json
import os
import sys
from pathlib import Path

from . import capabilities

ROOT = Path(__file__).resolve().parent.parent
SERVER_NAME = "asta"


def _asta_url() -> str:
    return os.environ.get(
        "ASTA_URL", f"http://127.0.0.1:{os.environ.get('ASTA_PORT', '8321')}")


def _proxy(cap):
    """An MCP tool that FORWARDS to the running server instead of running the
    function here.

    This subprocess has the capability functions importable, but not the server's
    live state: `delegate_task` would spawn its worker on THIS throwaway loop and
    `ask_user` would await a future the server never sees. Forwarding every call
    to /api/_invoke runs it in the one process that owns that state. The brain
    still makes a native tool call — the HTTP hop is localhost and invisible to it.

    The wrapper carries cap.fn's own signature so FastMCP builds the same argument
    schema the function declares; only the body changes.
    """
    import functools
    import httpx

    sig = inspect.signature(cap.fn)

    async def call(**kwargs):
        async with httpx.AsyncClient(timeout=600) as c:
            r = await c.post(
                f"{_asta_url()}/api/_invoke",
                json={"tool": cap.name, "args": kwargs,
                      "conv_id": os.environ.get("ASTA_MCP_CONV", "")},
                headers={"Authorization": "Bearer " + os.environ.get("ASTA_TOKEN", "")},
            )
            if r.status_code >= 400:
                raise RuntimeError(f"{cap.name} failed ({r.status_code}): {r.text[:300]}")
            return r.json().get("result")

    functools.update_wrapper(call, cap.fn)
    call.__signature__ = sig            # FastMCP reads this to build the schema
    return call


def _allowed_tools() -> set[str] | None:
    """Which capabilities to expose this run, from ASTA_MCP_TOOLS (comma-list), or
    None for the full set. Context-aware tool selection sets this per turn so a
    strong-but-token-heavy 50-tool schema shrinks to the ~handful the message
    actually needs. The ALWAYS floor is folded in unconditionally, so a narrow
    (or mis-ranked) selection can never drop remember / ask_user / delegate_task /
    prepare_to_send and strand the model."""
    raw = os.environ.get("ASTA_MCP_TOOLS", "").strip()
    if not raw:
        return None
    names = {t.strip() for t in raw.split(",") if t.strip()}
    return names | set(capabilities.ALWAYS)


def build_server():
    """One MCP tool per capability, each forwarding to the running Asta server so
    the function executes where its in-process state lives (see _proxy). When
    ASTA_MCP_TOOLS is set, only that subset (plus the ALWAYS floor) is exposed."""
    from mcp.server.fastmcp import FastMCP

    server = FastMCP(SERVER_NAME)
    allow = _allowed_tools()
    for cap in capabilities.registry().values():
        if allow is not None and cap.name not in allow:
            continue
        server.add_tool(_proxy(cap), name=cap.name, description=_describe(cap))
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


def config_entry(tools: list[str] | None = None, conv_id: str = "") -> dict:
    """The mcpServers entry a CLI needs to spawn this server.

    The env is carried explicitly rather than left to inheritance: the spawned
    server has to reach the RUNNING Asta to forward calls, so it needs the token
    and port even if the CLI launches it with a scrubbed environment. `tools`, when
    given, narrows the exposed set for this turn (see _allowed_tools).

    `conv_id` is the conversation this brain is answering in, and it is not
    optional in practice. `prepare_to_send` and `continue_working` read the
    conversation from a ContextVar that only the in-process chat path sets, so
    over the MCP hop — a fresh HTTP request into the server — both returned "No
    active conversation" every single time. They are in the ALWAYS floor, so the
    model was offered them on every turn and neither one worked: it could draft a
    reply to a colleague and had no way to stage it.
    """
    env = {
        "ASTA_TOKEN": os.environ.get("ASTA_TOKEN", ""),
        "ASTA_PORT": os.environ.get("ASTA_PORT", "8321"),
        "ASTA_URL": _asta_url(),
    }
    if tools:
        env["ASTA_MCP_TOOLS"] = ",".join(tools)
    if conv_id:
        env["ASTA_MCP_CONV"] = conv_id
    return {
        "mcpServers": {
            SERVER_NAME: {
                "command": str(ROOT / ".venv" / "bin" / "python"),
                "args": ["-m", "app.mcp_server"],
                "cwd": str(ROOT),
                "env": env,
            }
        }
    }


def write_config(dest: Path | None = None, tools: list[str] | None = None) -> Path:
    """Write the mcpServers config to a file a CLI can point --mcp-config at.

    Lives under data/ (gitignored) at 0600 — it carries the bearer token, so it
    must never be world-readable or committed. Callers pass a per-conversation
    `dest` so concurrent turns never clobber one shared file.
    """
    dest = dest or (ROOT / "data" / "asta-mcp.json")
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(json.dumps(config_entry(tools), indent=2))
    with contextlib.suppress(OSError):
        dest.chmod(0o600)
    return dest


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
