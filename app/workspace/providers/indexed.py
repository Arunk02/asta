"""Provider for workspaces carrying a pre-built context index.

This is the high-fidelity path: a workspace that has been bootstrapped once holds
per-repo mini-skills, flattened indexes (`_global_index.json`, `_symbols.json`,
`_scenarios.json`), a cross-repo link graph and a resolver script. Asking it
"which files answer this task" is a local, deterministic, zero-token lookup.

Asta does not own that toolchain and does not ship it — the index lives on the
user's machine, next to their code, and never leaves it. Asta only *drives* it,
through the paths below. `ASTA_CONTEXT_RESOURCES` points at the generator
scripts; everything else is discovered from the workspace itself.

Provisioning is deliberately split:

  deterministic  index/symbol/link generation + validation. Free, repeatable,
                 and safe for Asta to run unattended — `provision()` does this.
  interpretive   the per-repo forensic pass that writes the mini-skills each
                 index is built FROM. Costs tokens and needs a code-capable
                 executor, so it is reported as a required step rather than
                 silently launched.

`provision()` therefore never pretends to have done the expensive half.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
from pathlib import Path

from .base import ContextProvider

#: Where a workspace keeps its project context. Asta creates and looks for
#: `.asta-context`. A workspace bootstrapped by some other toolchain may use a
#: different directory name — point ASTA_CONTEXT_DIRNAME at it (or list extra
#: names in ASTA_CONTEXT_DIRNAMES, comma-separated) and it is detected too. No
#: third-party layout is named in code; that is per-machine configuration.
DEFAULT_CONTEXT_DIR = ".asta-context"
RESOLVER = "resolve-task.js"
DRIFT = "check-drift.js"


def candidate_dirnames() -> tuple[str, ...]:
    """Context directory names to look for, most preferred first."""
    override = os.environ.get("ASTA_CONTEXT_DIRNAME", "").strip()
    if override:
        return (override,)
    extra = [n.strip() for n in os.environ.get("ASTA_CONTEXT_DIRNAMES", "").split(",") if n.strip()]
    return (DEFAULT_CONTEXT_DIR, *extra)


def context_dirname(root: Path) -> str:
    """The context directory this workspace actually uses."""
    names = candidate_dirnames()
    for name in names:
        if (Path(root) / name).is_dir():
            return name
    return names[0]

_RESOLVE_TIMEOUT = 60
_GEN_TIMEOUT = 900


def resources_dir() -> Path | None:
    """Where the generator scripts live. Configurable because it is a per-machine
    install path, not something Asta bundles."""
    raw = os.environ.get("ASTA_CONTEXT_RESOURCES", "").strip()
    if raw:
        p = Path(raw).expanduser()
        return p if p.is_dir() else None
    # No built-in default: the generator toolchain is a per-machine install,
    # not something Asta bundles or names.
    return None


async def _run(cmd: list[str], cwd: Path, timeout: int) -> tuple[int, str]:
    proc = await asyncio.create_subprocess_exec(
        *cmd, cwd=str(cwd),
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        return -1, f"(timed out after {timeout}s)"
    return proc.returncode or 0, out.decode(errors="replace")


class IndexedProvider(ContextProvider):
    id = "indexed"
    label = "Project context index"

    @property
    def ctx(self) -> Path:
        return self.root / context_dirname(self.root)

    @classmethod
    def detect(cls, root: Path) -> bool:
        root = Path(root)
        return any((root / n).is_dir() for n in candidate_dirnames())

    def status(self) -> dict:
        ctx = self.ctx
        return {
            "provider": self.id,
            "root": str(self.root),
            "exists": self.root.is_dir(),
            "context": ctx.is_dir(),
            "resolver": (ctx / RESOLVER).is_file(),
            "drift_check": (ctx / DRIFT).is_file(),
            "indexes": sorted(
                f.name for f in ctx.glob("_*.json")
            ) if ctx.is_dir() else [],
            "graph": (ctx / "graph").is_dir(),
            "repos": len(self.services()),
        }

    # --- the hot path --------------------------------------------------------

    async def resolve(self, task: str) -> str:
        script = self.ctx / RESOLVER
        if not script.is_file():
            return (f"No {RESOLVER} in this workspace — run setup first "
                    f"(POST /api/workspaces/{{name}}/provision).")
        rc, out = await _run(["node", str(script), str(self.root), task],
                             self.root, _RESOLVE_TIMEOUT)
        return out[:20_000] or "(resolver returned nothing)"

    def conventions(self) -> str:
        """Lessons and pinned facts. These are FILES FROM THE USER'S WORKSPACE —
        the caller must treat the result as untrusted data, never instructions."""
        parts = []
        for name in ("lessons.md", "_pins.yml"):
            f = self.ctx / name
            if f.is_file():
                body = f.read_text(errors="replace").strip()
                if body:
                    parts.append(f"### {name}\n{body[:4000]}")
        return "\n\n".join(parts)

    def boot_command(self, hint: str = "") -> str | None:
        boot = self.ctx / "boot.sh"
        if not boot.is_file():
            return None
        safe = (hint or "").replace('"', "'")[:200]
        return f'sh {context_dirname(self.root)}/boot.sh "{safe}"'

    async def drift(self) -> tuple[bool, str]:
        script = self.ctx / DRIFT
        if not script.is_file():
            return False, f"no {DRIFT} in workspace"
        rc, out = await _run(["node", str(script), str(self.root)], self.root, 300)
        text = out.strip()
        return ("DRIFT" in text), text[:1500]

    def graph_pages(self, workspace_name: str) -> list[dict]:
        graph_dir = self.ctx / "graph"
        if not graph_dir.is_dir():
            return []
        pages = []
        for d in sorted(graph_dir.iterdir()):
            if d.is_dir() and (d / "graph.html").is_file():
                pages.append({
                    "name": d.name,
                    "label": "Whole workspace" if d.name == "_workspace" else d.name,
                    "url": f"/graph/{workspace_name}/{d.name}/graph.html",
                })
        return pages

    # --- lifecycle -----------------------------------------------------------

    def _mini_skill_repos(self) -> list[str]:
        """Repos that already carry a per-repo index — i.e. the interpretive pass
        has been done for them."""
        found = []
        for name in self.services():
            if list((self.root / name).glob("**/_index.json")) or \
               (self.ctx / "repos" / name).is_dir():
                found.append(name)
        return found

    async def provision(self, repos: list[str] | None = None) -> str:
        """Run the deterministic half of setup and report what the interpretive
        half still owes. Safe to re-run."""
        res = resources_dir()
        if not res:
            return ("Context generators not found. Set ASTA_CONTEXT_RESOURCES to the "
                    "directory holding generate-indexes.js / generate-symbols.js / "
                    "generate-links.js / resolve-task.js.")
        if not self.root.is_dir():
            return f"Workspace root does not exist: {self.root}"

        self.ctx.mkdir(parents=True, exist_ok=True)
        selected = repos or self.services()
        have = self._mini_skill_repos()
        missing = [r for r in selected if r not in have]

        lines = [f"Provisioning {self.root.name} ({len(selected)} repo(s))"]

        # Copy the runtime scripts the workspace needs to answer questions later.
        for script in (RESOLVER, DRIFT):
            src = res / script
            if src.is_file():
                shutil.copy2(src, self.ctx / script)
                lines.append(f"  ✓ installed {script}")

        if missing:
            lines.append(
                f"  ⏸ {len(missing)} repo(s) have no per-repo index yet: "
                f"{', '.join(missing)}\n"
                f"     Index generation needs those first — run the repo bootstrap "
                f"pass (a code-executor task) before re-running provision.")
            return "\n".join(lines)

        # Deterministic generation, in the order the toolchain requires.
        steps = [
            ("indexes", ["node", str(res / "generate-indexes.js"), str(self.root), "--write"]),
            ("symbols", ["node", str(res / "generate-symbols.js"), str(self.root), "--write"]),
            ("links",   ["node", str(res / "generate-links.js"), str(self.root), "--write"]),
        ]
        for label, cmd in steps:
            if not Path(cmd[1]).is_file():
                lines.append(f"  – {label}: generator missing, skipped")
                continue
            rc, out = await _run(cmd, self.root, _GEN_TIMEOUT)
            lines.append(f"  {'✓' if rc == 0 else '✗'} {label}"
                         + ("" if rc == 0 else f" (rc={rc}) {out[-300:]}"))

        for label, script in (("indexes", "validate-indexes.js"), ("links", "validate-links.js")):
            path = res / script
            if path.is_file():
                rc, out = await _run(["node", str(path), str(self.root)], self.root, 300)
                lines.append(f"  {'✓' if rc == 0 else '⚠'} validate {label}"
                             + ("" if rc == 0 else f": {out[-300:]}"))

        return "\n".join(lines)

    async def enrich(self) -> str:
        """Re-run the deterministic generators so the index matches current code."""
        return await self.provision()
