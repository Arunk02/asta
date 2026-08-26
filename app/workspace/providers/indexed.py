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
import re
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

#: The resolver is documented as routing to "the ~350 tokens that matter";
#: the cap was 20,000 characters, about 5,000 tokens, injected before the
#: model had read the question. 6,000 characters is ~1,500 tokens — room for
#: a real answer, a quarter of the old ceiling. Override if a workspace
#: genuinely needs more.
_RESOLVE_CHARS = int(os.environ.get("ASTA_RESOLVE_CHARS", "6000"))
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


#: Changes that cannot alter what the context SAYS about a repo. Rebuilding on
#: these is pure token waste: the context describes what a service owns, its
#: entry points and its dependencies — none of which a test fixture, a lockfile
#: or a typo fix changes. Kept deliberately conservative: when unsure, treat a
#: file as material, because a missed real change is worse than one extra
#: rebuild prompt.
_IMMATERIAL_DIRS = ("test", "tests", "spec", "specs", "__tests__", "fixtures",
                    "testdata", "test-data", "e2e", "docs", "doc", "examples",
                    "sample", "samples", ".github", ".idea", ".vscode")
_IMMATERIAL_NAMES = ("readme.md", "changelog.md", "license", "notice",
                     ".gitignore", ".gitattributes", ".editorconfig",
                     "package-lock.json", "yarn.lock", "pnpm-lock.yaml",
                     "poetry.lock", "gemfile.lock", "cargo.lock", ".ds_store")
_IMMATERIAL_SUFFIX = (".md", ".txt", ".rst", ".png", ".jpg", ".jpeg", ".gif",
                      ".svg", ".ico", ".csv", ".log", ".lock")

#: …except these, which describe the shape of the service and ARE material even
#: though their extension looks inert.
_MATERIAL_NAMES = ("pom.xml", "build.gradle", "build.gradle.kts", "package.json",
                   "requirements.txt", "pyproject.toml", "go.mod", "cargo.toml",
                   "dockerfile", "docker-compose.yml", "openapi.yaml",
                   "openapi.json", "schema.sql")


def is_material(rel_path: str) -> bool:
    """Whether a changed file could change what the project context says."""
    # removeprefix, not lstrip: lstrip("./") strips CHARACTERS, turning
    # ".gitignore" into "gitignore" and ".github/…" into "github/…", so both
    # silently stopped matching their immaterial rules.
    p = rel_path.strip().lower().removeprefix("./")
    if not p:
        return False
    name = p.rsplit("/", 1)[-1]
    if name in _MATERIAL_NAMES:
        return True
    parts = p.split("/")
    if any(seg in _IMMATERIAL_DIRS for seg in parts[:-1]):
        return False
    if name in _IMMATERIAL_NAMES:
        return False
    if name.startswith("test_") or name.endswith(("_test.py", "_test.go", ".test.js",
                                                  ".spec.js", ".test.ts", ".spec.ts")):
        return False
    if p.endswith(_IMMATERIAL_SUFFIX):
        return False
    return True



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
        body = out[:_RESOLVE_CHARS] or "(resolver returned nothing)"
        return f"{await self.freshness()}\n\n{body}"

    async def freshness(self) -> str:
        """One line saying how much this context can be trusted.

        Drift was always detected, and always reported to Arun — never to the
        thing about to answer from the context. So a model could not tell
        knowledge verified this morning from knowledge verified never, and
        answered with the same confidence either way. Telling it lets it hedge
        where hedging is honest.
        """
        try:
            stale = await self._sha_drift()
        except Exception:
            return "[context freshness: unknown — the drift check itself failed]"
        if not stale:
            return "[context freshness: verified against current HEAD]"
        return ("[context freshness: STALE OR UNVERIFIED — treat the facts below as "
                "possibly out of date and say so if it matters:\n  "
                + "\n  ".join(stale[:6]) + "]")

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
        # PER-REPO lessons and pins, which this used to skip entirely. Measured:
        # asked why the booking build fails with a FilerException — cause and fix
        # both written down in that repo's own lessons.md — the answer came back
        # "I couldn't find any reference to that". The workspace-level file is
        # 1,163 characters and says nothing about MapStruct or how anything
        # builds; everything specific lives one directory down. Capturing a
        # lesson and never consulting it is the same as not capturing it.
        repos_dir = self.ctx / "repos"
        if repos_dir.is_dir():
            for repo in sorted(p for p in repos_dir.iterdir() if p.is_dir()):
                for name in ("lessons.md", "_pins.yml"):
                    f = repo / name
                    if not f.is_file():
                        continue
                    body = f.read_text(errors="replace").strip()
                    if body:
                        parts.append(f"### {repo.name}/{name}\n{body[:2500]}")
        return "\n\n".join(parts)

    def boot_command(self, hint: str = "") -> str | None:
        boot = self.ctx / "boot.sh"
        if not boot.is_file():
            return None
        safe = (hint or "").replace('"', "'")[:200]
        return f'sh {context_dirname(self.root)}/boot.sh "{safe}"'

    async def _sha_drift(self) -> list[str]:
        """Repos whose context no longer matches the code, with the noise removed.

        Two failure modes to avoid, in tension:

        FALSE NEGATIVE — the drift script maps staleness through each
          mini-skill's `sources:`, so a context written without mini-skills
          gives it nothing to flag while the SHAs plainly disagree. It reports
          CLEAN and you trust stale context forever.

        FALSE POSITIVE — comparing SHAs alone flags every commit. Adding a test
          fixture or fixing a typo in the README then reads as "your context is
          wrong", and acting on it rebuilds a whole repo for nothing. That is
          exactly the token waste this system exists to avoid.

        So: SHA mismatch opens the question, and the changed FILES answer it.
        Only material changes count (see `is_material`). Immaterial-only commits
        move the SHA forward without invalidating anything the context claims.
        """
        stale = []
        for repo in self.services():
            idx = self.ctx / "repos" / repo / "_index.json"
            repo_dir = self.root / repo
            if not idx.is_file():
                continue
            # A repo that cannot be verified is UNKNOWN, never clean. This used
            # to `continue` past anything without a .git directory, so a
            # workspace whose repos are not checkouts reported healthy forever
            # while its context aged without limit — the exact false negative
            # this function's docstring warns about, found live on a workspace
            # where six of seven repos were skipped.
            if not (repo_dir / ".git").exists():
                stale.append(f"{repo}: cannot verify — no git checkout at {repo_dir}")
                continue
            try:
                recorded = json.loads(idx.read_text()).get("verified_against", "")
            except (OSError, json.JSONDecodeError) as exc:
                stale.append(f"{repo}: cannot verify — unreadable _index.json ({exc.__class__.__name__})")
                continue
            if not recorded:
                stale.append(f"{repo}: cannot verify — _index.json records no verified_against")
                continue
            rc, head = await _run(["git", "rev-parse", "HEAD"], repo_dir, 30)
            head = head.strip()
            if rc != 0 or not head or head.startswith(recorded[:8]) or recorded.startswith(head[:8]):
                continue

            rc, out = await _run(
                ["git", "diff", "--name-only", f"{recorded}..HEAD"], repo_dir, 60)
            if rc != 0:
                # Cannot diff (shallow clone, rewritten history) — do not guess
                # it is fine; an unverifiable SHA gap is real drift.
                stale.append(f"{repo}: {recorded[:8]}..{head[:8]} (could not diff)")
                continue

            changed = [f for f in out.splitlines() if f.strip()]
            material = [f for f in changed if is_material(f)]
            if not material:
                continue
            shown = ", ".join(material[:4]) + (f" +{len(material) - 4} more" if len(material) > 4 else "")
            stale.append(
                f"{repo}: {recorded[:8]}..{head[:8]} — "
                f"{len(material)} of {len(changed)} changed file(s) affect the context "
                f"({shown})")
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
        """Bring the context back in line with the code — the deterministic half.

        This used to be `return await self.provision()`, which copies the runtime
        scripts and re-runs the index generators. Neither of those reads a line of
        source, so a workspace 286 commits out of date came back "enriched" with
        every mini-skill exactly as stale as before. The name promised something
        the body never did.

        What is honest to do here is everything that needs no judgement:
        re-derive the cross-repo links from source (a new producer/consumer really
        does become an edge automatically), rebuild the indexes over whatever the
        mini-skills now say, and then REPORT the mini-skills that still need a
        writer. Rewriting prose about code is the token-costly half and stays
        Arun's call — so this returns the worklist rather than pretending to have
        done it.
        """
        res = resources_dir()
        if not res:
            return ("Context generators not found. Set ASTA_CONTEXT_RESOURCES to the "
                    "directory holding generate-indexes.js / generate-links.js.")
        lines = [f"Enriching {self.root.name}"]

        # Cross-repo edges are derived from source, so this half genuinely
        # self-heals: a new REST client or listener becomes an edge with no writer.
        for label, script, args in (
                ("links", "generate-links.js", ["--write"]),
                ("indexes", "generate-indexes.js", ["--write"]),
                ("symbols", "generate-symbols.js", ["--write"]),
                ("router", "reconcile-router.js", ["--write"])):
            path = res / script
            if not path.is_file():
                continue
            rc, out = await _run(["node", str(path), str(self.root), *args],
                                 self.root, 600, self.ctx.name)
            lines.append(f"  {'✓' if rc == 0 else '⚠'} {label}"
                         + (f": {out.strip()[-160:]}" if out.strip() else ""))

        stale, detail = await self.drift()
        if not stale:
            lines.append("  ✓ every mini-skill matches its repo's HEAD")
            return "\n".join(lines)

        # Name the work rather than claim it. A writer — Arun's yes, or the task
        # the offer spawns — patches these against the Step 5b quality bar.
        skills = sorted(set(re.findall(r"([a-z-]+/[a-z0-9-]+\.md)", detail)))
        lines.append(f"  ⏸ {len(skills)} mini-skill(s) still need a writer "
                     f"(prose about code — not derivable):")
        lines.append("     " + ", ".join(skills[:12]) + (" …" if len(skills) > 12 else ""))
        lines.append("     These are NOT stamped verified_against — staleness stays "
                     "visible until someone actually reads the code.")
        return "\n".join(lines)
