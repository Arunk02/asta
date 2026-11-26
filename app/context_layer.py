"""The workspace's own Claude-native context, loaded by Claude Code itself on every code leg.

A workspace that carries the layer has a root `CLAUDE.md` and `.asta-context/services.json`
(service key → repo directory), plus `services/<svc>/CLAUDE.md` per repo. booking-workspace PR 2
(26 Nov) added it there.

Before this, a code leg ran in `~/.asta-worktrees/<ws>/task-N/<repo>`, outside the workspace, so
nothing of it loaded on its own. The workspace reached the leg as a text note that said "run
boot.sh". That cost ~1.8k tokens on every leg whatever the task, and its resolver missed every
code-named task. Measured 26 Nov in a scratch copy with claude 2.1.326:

- `--add-dir <ws>` with CLAUDE_CODE_ADDITIONAL_DIRECTORIES_CLAUDE_MD=1 loads the root CLAUDE.md and
  `.claude/rules/*.md`, with `--system-prompt` too (the on-demand code path). Without the env var,
  neither loads.
- A `CLAUDE.local.md` symlink in a repo under the leg's cwd loads when the leg first reads a file
  there.
- `claudeMdExcludes` in `--settings` skips the team's CLAUDE.md, including by `**` glob.
- `--disable-slash-commands` hides skills, so nothing here depends on one.

ASTA_CONTEXT_LAYER=0 turns all of this off. Legs then get the boot.sh note exactly as before.
"""
from __future__ import annotations

import json
import os
import re
from pathlib import Path

ENV_ADDITIONAL_MD = "CLAUDE_CODE_ADDITIONAL_DIRECTORIES_CLAUDE_MD"
SERVICES = Path(".asta-context") / "services.json"
LOCAL = "CLAUDE.local.md"
#: A leg's report of where a doc and the code disagree, the precedence line's output:
#: `doc → says X, code does Y`. Harvested for the night refresh.
_MISMATCH = re.compile(r"^\s*[-*]?\s*`?([\w./-]+\.md)`?\s*(?:→|->)\s*says\s+(.+?),\s*code\s+(?:does|has|is)\s+(.+)$",
                       re.I | re.M)


def enabled() -> bool:
    return os.environ.get("ASTA_CONTEXT_LAYER", "1").strip().lower() not in ("0", "false", "no", "off")


def services(ws_root: Path | str) -> dict[str, str]:
    """service key → repo directory name, or {} when the workspace has no layer."""
    root = Path(ws_root)
    try:
        data = json.loads((root / SERVICES).read_text())
    except (OSError, ValueError):
        return {}
    if not (root / "CLAUDE.md").is_file() or not isinstance(data, dict):
        return {}
    return {str(k): str(v) for k, v in data.items() if isinstance(v, str)}


def active(ws_root: Path | str | None) -> bool:
    return bool(ws_root) and enabled() and bool(services(ws_root))


def root_for_cwd(cwd: str | Path | None) -> Path | None:
    """The layered workspace a leg in `cwd` belongs to: the workspace itself or one of its task
    checkouts under ~/.asta-worktrees/<ws name>/. None when it is neither."""
    if not cwd or not enabled():
        return None
    here = Path(cwd).resolve()
    from . import claude_cli, worktrees
    for raw in claude_cli.workspace_roots():
        root = Path(raw).resolve()
        if not services(root):
            continue
        if here == root or root in here.parents:
            return root
        base = worktrees.base_for(root).resolve()
        if here == base or base in here.parents:
            return root
    return None


def link_service_files(checkout: str | Path, ws_root: str | Path) -> list[str]:
    """Give every service repo under `checkout` its `CLAUDE.local.md` → `services/<svc>/CLAUDE.md`
    symlink, and keep it out of git. Idempotent; it runs on every leg, so a resumed task and a
    checkout cut before this existed get it too. Returns the repos linked now."""
    root = Path(ws_root)
    svc_of = {repo: svc for svc, repo in services(root).items()}
    here = Path(checkout)
    candidates = [here] + ([p for p in here.iterdir() if p.is_dir()] if here.is_dir() else [])
    linked = []
    for repo_dir in candidates:
        svc = svc_of.get(repo_dir.name)
        if not svc or not (repo_dir / ".git").exists():
            continue
        target = root / "services" / svc / "CLAUDE.md"
        if not target.is_file():
            continue
        link = repo_dir / LOCAL
        if link.is_symlink() and os.readlink(link) == str(target):
            continue
        if link.exists() and not link.is_symlink():
            continue          # a real file someone wrote: never replaced
        if link.is_symlink():
            link.unlink()
        link.symlink_to(target)
        _exclude(repo_dir)
        linked.append(repo_dir.name)
    return linked


def _exclude(repo_dir: Path) -> None:
    """Add CLAUDE.local.md to the repo's info/exclude (shared by all its worktrees)."""
    import subprocess
    try:
        out = subprocess.run(["git", "-C", str(repo_dir), "rev-parse", "--git-common-dir"],
                             capture_output=True, text=True, timeout=10, stdin=subprocess.DEVNULL)
        common = Path(out.stdout.strip())
        if not common.is_absolute():
            common = (repo_dir / common).resolve()
        exclude = common / "info" / "exclude"
        lines = exclude.read_text().splitlines() if exclude.is_file() else []
        if LOCAL not in lines:
            exclude.parent.mkdir(parents=True, exist_ok=True)
            with exclude.open("a") as fh:
                fh.write(("\n" if lines and lines[-1] else "") + LOCAL + "\n")
    except (OSError, subprocess.SubprocessError):
        pass


def excludes(ws_root: str | Path) -> list[str]:
    """Absolute globs for the teams' own instruction files, which ours replace: in his checkouts
    and in every task checkout. CLAUDE.local.md is not matched (a different name)."""
    from . import worktrees
    root = Path(ws_root).resolve()
    base = worktrees.base_for(root).resolve()
    out = []
    for repo in services(root).values():
        for name in ("CLAUDE.md", "AGENTS.md", ".claude/CLAUDE.md"):
            out += [str(root / repo / name), f"{base}/*/{repo}/{name}"]
    return out


def deny_his_checkouts(ws_root: str | Path) -> list[str]:
    """Edit/Write rules that keep a leg out of his own service checkouts. `--add-dir <ws>` makes them
    reachable, and a leg works only in its task checkout. Measured 26 Nov: the deny holds under
    bypassPermissions ("File is in a directory that is denied")."""
    root = Path(ws_root).resolve()
    return [f"{tool}(/{root}/{repo}/**)" for repo in services(root).values() for tool in ("Edit", "Write")]


def boot_line(ws_root: str | Path) -> str:
    """What replaces the boot.sh instruction in a code leg's runtime rules."""
    ctx = Path(ws_root) / ".asta-context" / "ctx.js"
    return (f"- Context: the workspace's CLAUDE.md files are already loaded. Find code with ONE call:\n"
            f"  `node {ctx} find \"<key nouns>\"`, which returns ranked repo/path:line with the test\n"
            f"  beside each, plus the docs to read. Read only those hits and what they call.\n"
            f"  Never grep a whole repo first. Do NOT run check-drift.js: drift is Asta's refresh job.\n")


def note(ws_root: str | Path) -> str:
    root = Path(ws_root)
    return (f"\n[Workspace context] Rules: {root}/CLAUDE.md and {root}/services/<svc>/CLAUDE.md "
            f"(Claude Code has them loaded already; any other agent reads them first). "
            f"Find code: `node {root}/.asta-context/ctx.js find \"<key nouns>\"`.\n")


_BOOT_BULLET = re.compile(r"- Boot 0 efficiency:.*?only the cited sections\.\n", re.S)


def rewrite_boot(prompt: str, ws_root: str | Path | None) -> str:
    if not active(ws_root):
        return prompt
    return _BOOT_BULLET.sub(lambda _m: boot_line(ws_root), prompt)


def mismatches(text: str) -> list[dict]:
    """`doc → says X, code does Y` lines from a leg's reply."""
    return [{"doc": m.group(1), "says": m.group(2).strip()[:240], "code": m.group(3).strip()[:240]}
            for m in _MISMATCH.finditer(text or "")]


def record_mismatches(ws_root: str | Path, task_id: int, text: str) -> int:
    """Append a leg's doc/code mismatches to the workspace's local drift inbox (gitignored)."""
    found = mismatches(text)
    if not found or not active(ws_root):
        return 0
    import time
    inbox = Path(ws_root) / ".asta-context" / ".mismatch-log.jsonl"
    try:
        with inbox.open("a") as fh:
            for m in found:
                fh.write(json.dumps({"at": time.strftime("%Y-%m-%dT%H:%M:%S"), "task": task_id, **m}) + "\n")
    except OSError:
        return 0
    return len(found)
