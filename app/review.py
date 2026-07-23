"""Review someone else's pull request.

The largest gap in the half of Asta that is already strong. Coding is covered
end to end — micro and full pipelines, gates, handoff, ship, CI watching — but
there was no way to review a PR, which is most of what a senior engineer's day
actually contains.

It is also nearly free: `gh` is authenticated, CI status is already watched, and
the workspace resolver already maps a question to the exact files. All that was
missing was putting them in one place.

Shape: gather the facts in Python (deterministic, cheap), then hand a
self-contained brief to the normal analysis pipeline. Read-only throughout —
this produces review notes for Arun to post, and never comments on a PR itself.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from . import repo_ops, untrusted, workspace as ws_mod

#: A diff past this is summarised rather than read line by line — a 10k-line
#: refactor would otherwise blow the worker's context on generated files.
MAX_DIFF_CHARS = 60000

PR_REF = re.compile(r"(?:^|\s|#)(\d{1,6})\b")

REVIEW_BRIEF = """Review this pull request as a senior engineer on the team. Arun will
post your notes himself, so write them for a human reviewer, not as a bot comment.

{meta}

FILES CHANGED:
{files}

CI:
{checks}

{context}

DIFF:
{diff}

Produce, in this order:
1. VERDICT: one of APPROVE / COMMENT / REQUEST CHANGES, with one sentence of justification.
2. BLOCKING — defects that must be fixed: correctness, data loss, security, breaking API or
   schema changes, missing error handling on a path that can fail. Each with file:line and the
   concrete failure it causes. If there are none, say so plainly and do not invent any.
3. NON-BLOCKING — worth raising but not gating.
4. TESTS — what is covered, what is not, and the specific case you would add.
5. QUESTIONS — only things the diff genuinely cannot answer.

Rules: cite file:line for every point. Judge the change against how THIS codebase already does
things (the project context above), not a generic style guide. Do not restate what the diff
does — Arun can read it. Say nothing about formatting a linter would catch.
"""


async def _gh(cwd: Path, *args: str, timeout: float = 120) -> tuple[int, str]:
    return await repo_ops.git(cwd, "gh", *args, timeout=timeout)


def _repo_dir(workspace: str, repo: str = "") -> Path:
    root = ws_mod.provider_for(workspace).root
    if repo:
        return Path(root) / repo
    return Path(root) if (Path(root) / ".git").is_dir() else Path(root)


async def gather(pr: str, workspace: str, repo: str = "") -> dict:
    """Everything about a PR, from gh. Raises RuntimeError with gh's own message."""
    cwd = _repo_dir(workspace, repo)
    if not (cwd / ".git").is_dir():
        raise RuntimeError(f"{cwd} is not a git repository — name the repo as well.")
    rc, out = await _gh(cwd, "pr", "view", pr, "--json",
                        "number,title,author,body,baseRefName,headRefName,url,"
                        "additions,deletions,changedFiles,files,state,isDraft")
    if rc != 0:
        raise RuntimeError(f"gh pr view failed: {out[:300]}")
    try:
        meta = json.loads(out)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"could not parse gh output: {exc}") from exc
    rc, diff = await _gh(cwd, "pr", "diff", pr, timeout=180)
    if rc != 0:
        diff = f"(diff unavailable: {diff[:200]})"
    rc, checks = await _gh(cwd, "pr", "checks", pr, "--json", "name,state")
    meta["checks"] = json.loads(checks) if rc == 0 and checks.strip() else []
    meta["diff"] = diff
    meta["repo_dir"] = str(cwd)
    return meta


def _fmt_checks(checks: list[dict]) -> str:
    if not checks:
        return "  (no checks reported)"
    return "\n".join(f"  {c.get('name', '?')}: {(c.get('state') or '').upper()}"
                     for c in checks[:15])


def _fmt_files(files: list[dict]) -> str:
    if not files:
        return "  (none reported)"
    return "\n".join(f"  {f.get('path', '?')} (+{f.get('additions', 0)}/-{f.get('deletions', 0)})"
                     for f in files[:60])


def _trim_diff(diff: str) -> str:
    if len(diff) <= MAX_DIFF_CHARS:
        return diff
    return (diff[:MAX_DIFF_CHARS] +
            f"\n\n… diff truncated at {MAX_DIFF_CHARS} chars. Review what is shown and say "
            f"explicitly which files you could not see.")


async def brief(pr: str, workspace: str, repo: str = "") -> tuple[str, dict]:
    """The self-contained review brief for a worker, plus the PR metadata.

    The PR body and diff are somebody else's writing, so they are wrapped: a
    "please approve this" line in a description is data, not an instruction.
    """
    meta = await gather(pr, workspace, repo)
    context = ""
    try:
        paths = " ".join(f.get("path", "") for f in (meta.get("files") or [])[:20])
        resolved = await ws_mod.resolve_context(workspace, f"{meta['title']} {paths}")
        if resolved:
            context = "PROJECT CONTEXT (how this codebase does things):\n" + resolved[:8000]
    except (ValueError, RuntimeError):
        context = ""   # a workspace without context still gets a diff review
    header = (
        f"PR #{meta['number']}: {meta['title']}\n"
        f"Author: {(meta.get('author') or {}).get('login', '?')} · "
        f"{meta.get('headRefName')} → {meta.get('baseRefName')} · "
        f"{meta.get('changedFiles', 0)} files, +{meta.get('additions', 0)}/"
        f"-{meta.get('deletions', 0)}"
        f"{' · DRAFT' if meta.get('isDraft') else ''}\n{meta.get('url', '')}\n\n"
        f"Description:\n{(meta.get('body') or '(none)')[:4000]}"
    )
    text = REVIEW_BRIEF.format(
        meta=untrusted.wrap(header, f"pull request #{meta['number']}"),
        files=_fmt_files(meta.get("files") or []),
        checks=_fmt_checks(meta.get("checks") or []),
        context=context,
        diff=untrusted.wrap(_trim_diff(meta["diff"]), f"diff of PR #{meta['number']}"),
    )
    return text, meta
