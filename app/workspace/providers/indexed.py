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

ROOT = Path(__file__).resolve().parents[3]

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
    # Asta bundles the generators with its workspace-context skill, so a fresh
    # install can provision with no extra setup. ASTA_CONTEXT_RESOURCES still
    # overrides, for a machine that keeps them elsewhere.
    bundled = ROOT / "skills" / "workspace-context" / "resources"
    return bundled if bundled.is_dir() else None


async def _run(cmd: list[str], cwd: Path, timeout: int, ctx_dir: str = "") -> tuple[int, str]:
    env = {**os.environ, "ASTA_CONTEXT_DIR": ctx_dir} if ctx_dir else None
    proc = await asyncio.create_subprocess_exec(
        *cmd, cwd=str(cwd), env=env,
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
                             self.root, _RESOLVE_TIMEOUT, self.ctx.name)
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

    async def _sha_drift(self) -> list[str]:
        """Repos whose recorded SHA no longer matches HEAD.

        A floor under the script-based check. The script maps drift precisely
        through each mini-skill's `sources:` front-matter, which is richer — but
        a context written in a simpler shape has no mini-skills, so the script
        finds nothing to mark stale and reports CLEAN while the SHAs plainly
        disagree. A silent false negative is the worst outcome here: the whole
        point is knowing when to stop trusting the context.
        """
        stale = []
        for repo in self.services():
            idx = self.ctx / "repos" / repo / "_index.json"
            repo_dir = self.root / repo
            if not idx.is_file() or not (repo_dir / ".git").exists():
                continue
            try:
                recorded = json.loads(idx.read_text()).get("verified_against", "")
            except (OSError, json.JSONDecodeError):
                continue
            if not recorded:
                continue
            rc, head = await _run(["git", "rev-parse", "HEAD"], repo_dir, 30)
            head = head.strip()
            if rc == 0 and head and not head.startswith(recorded[:8]) \
               and not recorded.startswith(head[:8]):
                stale.append(f"{repo}: {recorded[:8]}..{head[:8]}")
        return stale

    async def drift(self) -> tuple[bool, str]:
        script = self.ctx / DRIFT
        text = ""
        if script.is_file():
            rc, out = await _run(["node", str(script), str(self.root)], self.root, 300,
                                 self.ctx.name)
            text = out.strip()
            if "DRIFT" in text:
                return True, text[:1500]

        sha_stale = await self._sha_drift()
        if sha_stale:
            detail = "DRIFT (recorded SHA no longer matches HEAD):\n  " + "\n  ".join(sha_stale)
            return True, (detail + (f"\n\n{text[:600]}" if text else ""))[:1500]

        if not script.is_file() and not self.services():
            return False, f"no {DRIFT} in workspace"
        return False, text[:1500] or "context up-to-date"

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

    def _repo_meta(self, repo: str) -> dict:
        f = self.ctx / "repos" / repo / "_index.json"
        try:
            return json.loads(f.read_text())
        except (OSError, json.JSONDecodeError):
            return {}

    def _write_manifests(self, repos: list[str]) -> str:
        """Write workspace.yml, _repo_router.json and _global_links.json.

        Derived entirely from the per-repo indexes, so it is deterministic and
        costs nothing. The router is a routing *stub*: each repo's declared
        domains become the phrases that select it. That is honest for a fresh
        workspace — richer cross-repo routing is a later enrichment, and a stub
        beats a resolver that cannot start.
        """
        written = []

        wsf = self.ctx / "workspace.yml"
        if not wsf.exists():
            body = [f"workspace: {self.root.name}", "version: 3",
                    f"mode: {'single' if len(repos) == 1 else 'workspace'}",
                    "tier: full", "repos:"]
            for r in repos:
                meta = self._repo_meta(r)
                domains = ", ".join(meta.get("domains", [])[:8])
                body += [f"  - key: {r}",
                         f"    root: \"{'.' if len(repos) == 1 else r}\"",
                         f"    domains: [{domains}]",
                         "    depends_on: []"]
            wsf.write_text("\n".join(body) + "\n")
            written.append("workspace.yml")

        links = self.ctx / "_global_links.json"
        if not links.exists():
            # No verified cross-repo contracts yet; an empty list is the correct
            # starting value and the resolver short-circuits on it.
            links.write_text("[]\n")
            written.append("_global_links.json")

        router = self.ctx / "_repo_router.json"
        if not router.exists():
            buckets, summaries = {}, {}
            for r in repos:
                meta = self._repo_meta(r)
                summaries[r] = meta.get("summary", "")
                for phrase in meta.get("domains", []):
                    buckets.setdefault(str(phrase).lower(), []).append(r)
                buckets.setdefault(r.lower(), []).append(r)
            router.write_text(json.dumps({
                "schema_version": 2,
                "request_buckets": buckets,
                "flows": [],
                "per_repo_summary": summaries,
                "disambiguation_rules": [],   # array: the resolver iterates it
                "glossary": [],
                "default_repo": repos[0] if repos else "",
            }, indent=2) + "\n")
            written.append("_repo_router.json")

        return ("  ✓ manifests: " + ", ".join(written)) if written else \
               "  – manifests already present"

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

        # The resolver refuses to start without these three. The generators do
        # not produce them, so a build that skipped this step produced indexes
        # that looked complete and a resolver that returned
        # {"error":"missing_file"} on every query. Written before the
        # generators run, and never overwritten if the user has customised them.
        lines.append(self._write_manifests(selected))

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
            rc, out = await _run(cmd, self.root, _GEN_TIMEOUT, self.ctx.name)
            lines.append(f"  {'✓' if rc == 0 else '✗'} {label}"
                         + ("" if rc == 0 else f" (rc={rc}) {out[-300:]}"))

        for label, script in (("indexes", "validate-indexes.js"), ("links", "validate-links.js")):
            path = res / script
            if path.is_file():
                rc, out = await _run(["node", str(path), str(self.root)], self.root, 300, self.ctx.name)
                lines.append(f"  {'✓' if rc == 0 else '⚠'} validate {label}"
                             + ("" if rc == 0 else f": {out[-300:]}"))

        return "\n".join(lines)

    async def enrich(self) -> str:
        """Re-run the deterministic generators so the index matches current code."""
        return await self.provision()
