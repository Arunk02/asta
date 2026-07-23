"""Contmark workspace integration: surgical repo context instead of reading whole repos."""

from __future__ import annotations

import asyncio
from pathlib import Path

WORKSPACES: dict[str, Path] = {
    # "iom" removed 2026-07-19 (focus on booking for now) — re-add the line below to restore:
    # "iom": Path.home() / "IOM-workspace",
    "booking": Path.home() / "booking-workspace",
}

MAX_FILE_CHARS = 30_000


def available_workspaces() -> dict[str, dict]:
    out = {}
    for name, root in WORKSPACES.items():
        contmark = root / ".contmark"
        out[name] = {
            "root": str(root),
            "exists": root.is_dir(),
            "contmark": contmark.is_dir(),
            "graph": (contmark / "graph").is_dir(),
        }
    return out


def _root(workspace: str) -> Path:
    root = WORKSPACES.get(workspace)
    if root is None or not root.is_dir():
        raise ValueError(f"Unknown workspace '{workspace}'. Use one of: {', '.join(WORKSPACES)}")
    return root


async def resolve_context(workspace: str, task: str) -> str:
    """Ask contmark's resolve-task.js which exact files/lines answer this task."""
    root = _root(workspace)
    script = root / ".contmark" / "resolve-task.js"
    if not script.is_file():
        return f"No .contmark/resolve-task.js in workspace '{workspace}'."
    proc = await asyncio.create_subprocess_exec(
        "node", str(script), str(root), task,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
        cwd=str(root),
    )
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=60)
    except asyncio.TimeoutError:
        proc.kill()
        return "resolve-task.js timed out after 60s."
    return out.decode(errors="replace")[:20_000] or "(no output)"


def list_services(workspace: str) -> list[str]:
    root = _root(workspace)
    return sorted(
        p.name for p in root.iterdir()
        if p.is_dir() and not p.name.startswith(".") and p.name not in ("npmcache",)
    )


def read_workspace_file(workspace: str, rel_path: str, start_line: int = 1, end_line: int = 0) -> str:
    """Read (a slice of) a file inside a workspace. Paths outside the workspace are refused."""
    root = _root(workspace)
    path = (root / rel_path).resolve()
    if not str(path).startswith(str(root.resolve())):
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


def graph_pages(workspace: str) -> list[dict]:
    """Available graphfy pages for the UI's Graph tab."""
    root = WORKSPACES.get(workspace)
    if root is None:
        return []
    graph_dir = root / ".contmark" / "graph"
    if not graph_dir.is_dir():
        return []
    pages = []
    for d in sorted(graph_dir.iterdir()):
        if d.is_dir() and (d / "graph.html").is_file():
            label = "Whole workspace" if d.name == "_workspace" else d.name
            pages.append({"name": d.name, "label": label, "url": f"/graph/{workspace}/{d.name}/graph.html"})
    return pages
