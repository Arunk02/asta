"""Developer MCP servers for the code-task brains — Serena + Context7.

Serena gives a brain symbol-level navigation and editing (LSP-backed): it finds
and edits code by symbol instead of reading whole files, which is both steadier
on a large repo and materially cheaper in tokens. Context7 injects
version-correct library docs, so the brain stops hallucinating APIs for the exact
stack a repo actually uses.

These attach ONLY to a workspace-scoped task leg (a code or analysis run), never
to a chat turn, and only when ASTA_DEV_MCP is set. Off by default; purely
additive; a server whose command is not on PATH is silently skipped — a missing
binary must never break a task, the same contract mcp_loader keeps for the chat
agent.

Why not mcp.json: that file feeds the in-process chat agent. The code brains are
separate CLIs (claude/copilot `one_shot`) that receive their MCP config inline,
per run. Serena in particular must be pointed at the repo with `--project <cwd>`,
so it cannot be one static global entry — it is built per task from the run's own
working directory.

One shared policy, both brains: `config_json()` returns a string the claude
(`--mcp-config`) and copilot (`--additional-mcp-config`) paths pass through
unchanged, so the two brains never drift on which dev tools a code task gets.
"""

from __future__ import annotations

import json
import os
import shutil
from pathlib import Path

_TRUEY = ("1", "true", "yes", "on")


def enabled() -> bool:
    """Whether code-task brains get the dev MCP servers. Off by default."""
    return os.environ.get("ASTA_DEV_MCP", "").strip().lower() in _TRUEY


def _on_path(command: str) -> bool:
    """True if the command is an existing file or resolves on PATH — the same
    skip-if-missing test mcp_loader applies, so one absent binary is a no-op."""
    return bool(command) and (Path(command).is_file() or bool(shutil.which(command)))


def _serena_spec(project: str) -> dict | None:
    """Serena pointed at this workspace. Skipped when its CLI isn't installed."""
    command = os.environ.get("ASTA_SERENA_CMD", "serena").strip()
    if not _on_path(command):
        return None
    return {
        "command": command,
        "args": ["start-mcp-server", "--context", "ide-assistant",
                 "--project", project],
    }


def _context7_spec() -> dict | None:
    """Context7 docs server. Command matches the existing mcp.json entry so the
    same install serves both the chat agent and the code brains."""
    command = os.environ.get("ASTA_CONTEXT7_CMD", "context7-mcp").strip()
    if not _on_path(command):
        return None
    return {"command": command}


def servers(project: str) -> dict:
    """The {name: spec} dev servers available for this workspace.

    A server whose binary is absent is simply left out, so the result reflects
    what can actually be spawned right now — never a spec that would fail to boot.
    """
    out: dict = {}
    serena = _serena_spec(project)
    if serena:
        out["serena"] = serena
    context7 = _context7_spec()
    if context7:
        out["context7"] = context7
    return out


def config_for(project: str, base: dict | None = None) -> dict | None:
    """An mcpServers config adding the dev servers over `base`, or None when there
    is nothing to add (disabled, no workspace, or no binaries present).

    `base` lets this compose with Asta's own server config if a caller already has
    one; the dev servers are merged on top under their own names.
    """
    if not enabled() or not project:
        return None
    devs = servers(project)
    if not devs:
        return None
    merged = dict((base or {}).get("mcpServers", {}))
    merged.update(devs)
    return {"mcpServers": merged}


def config_json(project: str, base: dict | None = None) -> str:
    """Serialized config for a CLI's mcp-config flag, or "" when there's nothing
    to attach — callers add the flag only for a non-empty string, so the disabled
    path changes the command not at all."""
    cfg = config_for(project, base)
    return json.dumps(cfg) if cfg else ""
