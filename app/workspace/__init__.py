"""Workspace access — one front door for every caller.

Everything Asta knows about a codebase comes through here. Callers name a
workspace; which provider answers, and whether that workspace has a rich index
or only keyword search, is not their concern.

Back-compat: `WORKSPACES` behaves like the old module-level dict (name -> Path)
but reads the live registry, so a workspace added at runtime is visible
immediately and no caller had to change to get that.
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path

from . import registry
from .registry import Workspace, add, all_workspaces, get, infer, provider_for, remove, update

MAX_FILE_CHARS = 30_000


class _RootsView(Mapping):
    """Live name -> Path view of the registry."""

    def _data(self) -> dict[str, Path]:
        return {n: w.path for n, w in all_workspaces().items() if w.enabled}

    def __getitem__(self, key): return self._data()[key]
    def __iter__(self): return iter(self._data())
    def __len__(self): return len(self._data())
    def __repr__(self): return repr(self._data())


#: legacy name kept deliberately — see module docstring
WORKSPACES = _RootsView()


def names() -> list[str]:
    return sorted(WORKSPACES)


def available_workspaces() -> dict[str, dict]:
    """Registry + live provider status, for the UI and /api/status."""
    out = {}
    for name, ws in all_workspaces().items():
        if not ws.enabled:
            continue
        entry = {"root": ws.root, "exists": ws.exists(),
                 "provider": ws.provider, "jira_projects": ws.jira_projects}
        if ws.exists():
            try:
                entry.update(provider_for(name).status())
            except (ValueError, OSError) as exc:
                entry["error"] = str(exc)
        out[name] = entry
    return out


def _root(workspace: str) -> Path:
    ws = get(workspace)
    if ws is None or not ws.exists():
        known = ", ".join(names()) or "(none registered)"
        raise ValueError(f"Unknown workspace '{workspace}'. Registered: {known}")
    return ws.path


# --- context -----------------------------------------------------------------

async def resolve_context(workspace: str, task: str) -> str:
    """Which files/lines answer this task. Call before reading anything."""
    return await provider_for(workspace).resolve(task)


def conventions(workspace: str) -> str:
    """Build commands, lessons and pinned facts for this workspace.

    UNTRUSTED: this is file content from the user's repos. Wrap it before it
    reaches a model.
    """
    return provider_for(workspace).conventions()


def boot_command(workspace: str, hint: str = "") -> str | None:
    return provider_for(workspace).boot_command(hint)


async def drift(workspace: str) -> tuple[bool, str]:
    return await provider_for(workspace).drift()


async def provision(workspace: str, repos: list[str] | None = None) -> str:
    ws = get(workspace)
    return await provider_for(workspace).provision(repos or (ws.repos if ws else None))


async def enrich(workspace: str) -> str:
    return await provider_for(workspace).enrich()


def graph_pages(workspace: str) -> list[dict]:
    try:
        return provider_for(workspace).graph_pages(workspace)
    except (ValueError, OSError):
        return []


# --- files -------------------------------------------------------------------

def list_services(workspace: str) -> list[str]:
    return provider_for(workspace).services()


def read_workspace_file(workspace: str, rel_path: str,
                        start_line: int = 1, end_line: int = 0) -> str:
    """Read (a slice of) a file inside a workspace. Paths outside are refused."""
    root = _root(workspace).resolve()
    path = (root / rel_path).resolve()
    if not str(path).startswith(str(root)):
        raise ValueError("Path escapes the workspace root.")
    if not path.is_file():
        raise ValueError(f"Not a file: {rel_path}")
    lines = path.read_text(errors="replace").splitlines()
    if end_line <= 0:
        end_line = len(lines)
    start_line = max(1, start_line)
    chunk = lines[start_line - 1:end_line]
    text = "\n".join(f"{start_line + i}: {l}" for i, l in enumerate(chunk))
    if len(text) > MAX_FILE_CHARS:
        text = text[:MAX_FILE_CHARS] + f"\n… truncated; ask for a narrower line range of {rel_path}"
    return text


# --- setup -------------------------------------------------------------------

def detect(root: str | Path) -> dict:
    """What Asta would do with this directory, without registering it."""
    p = Path(root).expanduser()
    if not p.is_dir():
        return {"ok": False, "error": f"Not a directory: {p}"}
    pid = registry.detect_provider(p)
    cls = registry._BY_ID[pid]
    repos = sorted(
        d.name for d in p.iterdir()
        if d.is_dir() and not d.name.startswith(".")
        and d.name not in ("npmcache", "node_modules")
    )
    return {"ok": True, "root": str(p), "provider": pid,
            "provider_label": cls.label, "repos": repos,
            "indexed": pid == "indexed"}
