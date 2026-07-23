"""Fallback provider for a workspace with no pre-built index.

Any directory containing git repos can be used as a workspace immediately, with
no setup step. `resolve()` ranks files by keyword hits (ripgrep when present,
`git grep` otherwise) and returns the best candidates with line numbers.

Be honest about what this is: keyword search, not comprehension. It has no
cross-repo link graph, no scenario map and no symbol index, so it will miss the
"the ETA is never written on the VTS path" class of answer that an indexed
workspace gets right. It exists so a new user is productive on day one and so
Asta is never *blocked* on a toolchain it does not own — not as an equal
substitute. `status()` says so, and the setup flow offers the upgrade.
"""

from __future__ import annotations

import asyncio
import os
import re
import shutil
from pathlib import Path

from .base import ContextProvider

_STOP = {
    "the", "and", "for", "with", "that", "this", "から", "into", "from", "when", "what",
    "where", "which", "how", "why", "does", "did", "is", "are", "was", "were", "a", "an",
    "of", "to", "in", "on", "it", "we", "our", "add", "fix", "make", "use", "get", "set",
    "issue", "bug", "please", "need", "want", "should", "can", "not", "any", "all",
}
_MAX_HITS_PER_FILE = 4
_MAX_FILES = 12
_SEARCH_TIMEOUT = 45


def _keywords(task: str, limit: int = 8) -> list[str]:
    """Content words, longest first — long identifiers discriminate best."""
    words = re.findall(r"[A-Za-z_][A-Za-z0-9_]{2,}", task or "")
    seen, out = set(), []
    for w in sorted(words, key=len, reverse=True):
        lw = w.lower()
        if lw in _STOP or lw in seen:
            continue
        seen.add(lw)
        out.append(w)
        if len(out) >= limit:
            break
    return out


async def _run(cmd: list[str], cwd: Path, timeout: int = _SEARCH_TIMEOUT) -> tuple[int, str]:
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd, cwd=str(cwd),
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
    except FileNotFoundError:
        return 127, ""
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        return -1, ""
    return proc.returncode or 0, out.decode(errors="replace")


class PlainProvider(ContextProvider):
    id = "plain"
    label = "Plain repos (keyword search)"

    @classmethod
    def detect(cls, root: Path) -> bool:
        root = Path(root)
        if not root.is_dir():
            return False
        if (root / ".git").is_dir():
            return True
        return any((p / ".git").exists() for p in root.iterdir() if p.is_dir())

    def status(self) -> dict:
        return {
            "provider": self.id,
            "root": str(self.root),
            "exists": self.root.is_dir(),
            "repos": len(self.services()),
            "search": "ripgrep" if shutil.which("rg") else "git grep",
            "note": "keyword search only — no symbol or cross-repo index. "
                    "Provision an index for surgical context.",
        }

    async def resolve(self, task: str) -> str:
        words = _keywords(task)
        if not words:
            return "Could not derive search terms from that task."
        if not self.root.is_dir():
            return f"Workspace root does not exist: {self.root}"

        pattern = "|".join(re.escape(w) for w in words)
        # Search only what the user selected, never the whole root.
        targets = self.services() or ["."]
        if shutil.which("rg"):
            cmd = ["rg", "--line-number", "--no-heading", "--ignore-case",
                   "--max-count", str(_MAX_HITS_PER_FILE), "--max-columns", "200",
                   "-e", pattern, *targets]
            rc, out = await _run(cmd, self.root)
        else:
            # --untracked matters: without it, a file the user just created but
            # has not committed is invisible — exactly the file they're asking
            # about mid-change.
            chunks = []
            for repo in self.services():
                _, o = await _run(
                    ["git", "grep", "-n", "-I", "-i", "--untracked", "-E", pattern],
                    self.root / repo)
                chunks.extend(f"{repo}/{ln}" for ln in o.splitlines() if ln.strip())
            out = "\n".join(chunks)

        if not out.strip():
            return f"No matches for: {', '.join(words)}"

        # Rank files by how many distinct keywords they contain.
        per_file: dict[str, list[str]] = {}
        for line in out.splitlines():
            parts = line.split(":", 2)
            if len(parts) < 3:
                continue
            path = parts[0].lstrip("./")
            per_file.setdefault(path, [])
            if len(per_file[path]) < _MAX_HITS_PER_FILE:
                per_file[path].append(f"{parts[1]}: {parts[2].strip()[:160]}")

        def score(item):
            path, hits = item
            text = " ".join(hits).lower()
            return (sum(1 for w in words if w.lower() in text), len(hits))

        ranked = sorted(per_file.items(), key=score, reverse=True)[:_MAX_FILES]
        lines = [
            f"Keyword match for: {', '.join(words)}",
            f"({len(per_file)} files matched; showing top {len(ranked)}. "
            f"No symbol/link index — verify before relying on this.)",
            "",
        ]
        for path, hits in ranked:
            lines.append(path)
            lines.extend(f"    {h}" for h in hits)
        return "\n".join(lines)[:20_000]

    def conventions(self) -> str:
        """Whatever the repo states about itself. Untrusted — the caller wraps it."""
        parts = []
        for name in ("AGENTS.md", "CONTRIBUTING.md", "README.md"):
            f = self.root / name
            if f.is_file():
                body = f.read_text(errors="replace").strip()
                if body:
                    parts.append(f"### {name}\n{body[:2500]}")
                break
        return "\n\n".join(parts)

    async def provision(self, repos: list[str] | None = None) -> str:
        found = repos or self.services()
        return (f"No index built — '{self.label}' works with no setup.\n"
                f"Repos visible: {', '.join(found) or '(none)'}\n"
                f"For surgical context (symbols, scenarios, cross-repo links), "
                f"bootstrap an index and this workspace will switch to the "
                f"indexed provider automatically.")
