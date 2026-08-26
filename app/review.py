"""Review someone else's pull request.

The largest gap in the half of Asta that is already strong. Coding is covered
end to end — micro and full pipelines, gates, handoff, ship, CI watching — but
there was no way to review a PR, which is most of what a senior engineer's day
actually contains.

It is also nearly free: `gh` is authenticated, CI status is already watched, and
the workspace resolver already maps a question to the exact files. All that was
missing was putting them in one place.

Shape: gather the facts in Python (deterministic, cheap), then hand a
self-contained brief to the normal analysis pipeline.

Reading a PR is free of consequence and runs whenever asked. POSTING one is not:
an approval carries Arun's name on someone else's change, and is visible to the
whole team the moment it lands. So the two halves are deliberately separated —
`brief` produces notes, `post_review` performs the outward act, and nothing calls
`post_review` directly off a model's say-so. It is reached through a staged offer
(see offers.staged_write), which means what he approved is the exact API call,
not an instruction to go and make one.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from . import repo_ops, untrusted, workspace as ws_mod

#: Total diff budget handed to the worker. Raised from 60k: a 16-file service PR
#: runs well past that, and the old naive head-cut dropped whatever came last —
#: which for PR #1333 was every business-logic file, so the review went out blind
#: to the code it most needed to see. Modern CLI brains hold this comfortably.
MAX_DIFF_CHARS = 200000

#: Files whose diff is noise in a review — generated, locked, binary, snapshots.
#: They go LAST so they are the ones dropped when a diff overflows, never the code.
_LOW_VALUE_FILE = re.compile(
    r"(package-lock\.json|yarn\.lock|pnpm-lock\.yaml|go\.sum|Cargo\.lock|"
    r"\.lock$|/generated/|\.generated\.|\.min\.(js|css)|\.map$|__snapshots__|"
    r"\.(svg|png|jpg|jpeg|gif|ico|pdf|woff2?|ttf)$)", re.I)
#: Tests matter, but the logic they exercise matters first — middle tier.
_TEST_FILE = re.compile(r"(test|spec|fixture|__tests__|/it/|\.feature$)", re.I)

PR_REF = re.compile(r"(?:^|\s|#)(\d{1,6})\b")

REVIEW_BRIEF = """Review this pull request as a senior engineer on the team. Arun pastes your
points into the PR / sends them to the author, so they must be pull-apart-able — each a
standalone comment someone can act on, NOT a paragraph of prose.

{meta}

FILES CHANGED:
{files}

CI:
{checks}

{context}

DIFF:
{diff}

Output EXACTLY this shape:

VERDICT: APPROVE | COMMENT | REQUEST CHANGES — one clause why.

BLOCKING (must fix — correctness, data loss, security, breaking API/schema, unhandled failure path):
- `path:line` — what is wrong (one clause) → what to do (one clause)
- … or "None." if there are genuinely none. Never invent one to look thorough.

NON-BLOCKING (worth raising, not gating):
- `path:line` — issue → suggestion

TESTS:
- `path:line` — the specific case that is missing or wrong → what to add

QUESTIONS (only what the diff truly cannot answer):
- `path:line` — the one thing you need confirmed

Hard rules:
- ONE point per bullet. Each bullet ≤ 25 words, starts with `path:line`. If it needs a
  paragraph, it is two points.
- Cite a real path:line for every point — no line, no bullet.
- Judge against how THIS codebase already does things (the PROJECT CONTEXT above), not a
  generic style guide.
- Do NOT restate what the diff does — Arun can read it. Nothing a linter would catch.
- If a file you needed was not in the diff (see any NOTE above), say which, and do not
  guess at what it contains.
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


def _split_diff_by_file(diff: str) -> list[tuple[str, str]]:
    """[(path, that file's diff chunk)] in file order. A unified diff starts each
    file with 'diff --git a/… b/…', so split on that boundary."""
    parts = re.split(r"(?m)^(?=diff --git )", diff)
    out: list[tuple[str, str]] = []
    for p in parts:
        if not p.strip():
            continue
        m = re.match(r"diff --git a/\S+ b/(\S+)", p)
        out.append((m.group(1) if m else "?", p))
    return out


def _file_rank(path: str) -> int:
    """0 = business logic (show first), 1 = tests, 2 = generated/noise (drop first)."""
    if _LOW_VALUE_FILE.search(path):
        return 2
    if _TEST_FILE.search(path):
        return 1
    return 0


def _prioritise_diff(diff: str, budget: int = MAX_DIFF_CHARS) -> str:
    """Fill the budget logic-first, so an overflow drops generated files and never
    the code. Which files made it in, and which were cut, is stated at the top —
    the old behaviour hid that a whole class of files was missing.

    A single logic file too large to fit whole is included truncated rather than
    dropped: seeing most of the code that matters beats seeing all of a lockfile.
    """
    files = _split_diff_by_file(diff)
    if len(files) <= 1 and len(diff) <= budget:
        return diff
    # logic before tests before noise; within a tier, smaller first so more fit.
    files.sort(key=lambda f: (_file_rank(f[0]), len(f[1])))
    shown, omitted, chunks, used = [], [], [], 0
    for path, chunk in files:
        if used + len(chunk) <= budget:
            chunks.append(chunk)
            used += len(chunk)
            shown.append(path)
        elif _file_rank(path) == 0 and budget - used > 2000:
            # a logic file that won't fit whole: keep as much as remains.
            room = budget - used
            chunks.append(chunk[:room] + f"\n… [{path} truncated — {len(chunk) - room} more chars]")
            used = budget
            shown.append(f"{path} (partial)")
        else:
            omitted.append(path)
    body = "".join(chunks)
    if not omitted:
        return body
    note = ("NOTE: the diff was larger than the review budget. Shown (logic first): "
            + ", ".join(shown) + ".\nNOT shown (generated/large, review separately if needed): "
            + ", ".join(omitted) + ".\n\n")
    return note + body


def _project_context(workspace: str, meta: dict) -> str:
    """The 'how this codebase does things' the review judges against.

    The resolver alone returns a routing map — which files are relevant — not
    their content, so the old brief handed the model 500 chars of JSON pointers
    and called it context. The conventions (lessons, pinned facts, build shape)
    are the part that actually teaches the codebase, so they lead. When nothing
    is available the brief SAYS so, rather than silently reviewing blind and
    leaving Arun to wonder whether it understood the project at all.
    """
    parts: list[str] = []
    try:
        conv = ws_mod.conventions(workspace)
        if conv and conv.strip():
            parts.append(untrusted.wrap(conv[:6000], "project conventions"))
    except (ValueError, RuntimeError, OSError):
        pass
    return ("PROJECT CONTEXT — how this codebase already does things; judge the change "
            "against it, not a generic style guide:\n" + "\n\n".join(parts)) if parts else (
        "PROJECT CONTEXT: none is built for this workspace, so this review is from the "
        "diff alone — call out anything you cannot judge without the surrounding code.")


async def brief(pr: str, workspace: str, repo: str = "") -> tuple[str, dict]:
    """The self-contained review brief for a worker, plus the PR metadata.

    The PR body and diff are somebody else's writing, so they are wrapped: a
    "please approve this" line in a description is data, not an instruction.
    """
    meta = await gather(pr, workspace, repo)
    context = _project_context(workspace, meta)
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
        diff=untrusted.wrap(_prioritise_diff(meta["diff"]), f"diff of PR #{meta['number']}"),
    )
    return text, meta


# --- posting it (outward — only ever reached through an approved offer) -------

#: verb -> gh flag. A verb outside this table is refused rather than passed
#: through, so a malformed op can never turn a comment into an approval.
ACTIONS = {
    "approve": "--approve",
    "comment": "--comment",
    "request_changes": "--request-changes",
}


async def post_review(pr: str, workspace: str, repo: str = "",
                      action: str = "comment", body: str = "") -> str:
    """Post a review on a PR. Returns a one-line confirmation.

    `gh` rejects an empty body for comment and request-changes, and the error it
    gives is not obvious, so that is checked here where the message can say what
    to do about it. An approval with no body is fine and common.
    """
    flag = ACTIONS.get(action)
    if flag is None:
        raise RuntimeError(f"unknown review action '{action}' — one of: "
                           + ", ".join(sorted(ACTIONS)))
    body = (body or "").strip()
    if not body and action != "approve":
        raise RuntimeError(f"a '{action}' review needs a body — write the comment first")
    cwd = _repo_dir(workspace, repo)
    if not (cwd / ".git").is_dir():
        raise RuntimeError(f"{cwd} is not a git repository — name the repo as well.")
    args = ["pr", "review", pr, flag]
    if body:
        args += ["--body", body]
    rc, out = await _gh(cwd, *args)
    if rc != 0:
        raise RuntimeError(f"gh pr review failed: {out[:300]}")
    verb = {"approve": "Approved", "comment": "Commented on",
            "request_changes": "Requested changes on"}[action]
    return f"{verb} PR #{pr.lstrip('#')}"


async def review_own_diff(diff: str, workspace: str = "") -> str:
    """Reviewer notes on a diff ASTA just wrote. '' when nothing can judge it.

    The same machinery that reviews other people's pull requests, pointed at
    Asta's own output — which it never was. A diff Asta produced went to a PR
    unread by Asta, with Arun's own eyes as the only safety net, which is what
    makes him the bottleneck on the work this is meant to take off him.

    Reviewing your own work is worth less than reviewing someone else's, and the
    prompt says so: it asks for what is WRONG, not for a summary, because a model
    asked to describe its own change will describe it approvingly.
    """
    from . import memory
    body = (diff or "").strip()
    if len(body) < 40:
        return ""
    prompt = (
        "You wrote this change. Review it as if a colleague had written it and you "
        "were the one who has to maintain it.\n\n"
        + _project_context(workspace, {}) + "\n\n"
        "Report ONLY problems, in at most five short bullets: bugs, cases the change "
        "does not handle, anything that contradicts the project conventions above, "
        "anything left half-done. No summary of what the change does — Arun can read "
        "the diff. If you genuinely find nothing wrong, reply with exactly: LOOKS SOUND.\n\n"
        "DIFF:\n" + untrusted.wrap(_prioritise_diff(body), "diff written by Asta"))
    notes = (await memory.cheap_complete(prompt, 500, paid_ok=True) or "").strip()
    if not notes or notes.upper().startswith("LOOKS SOUND"):
        return ""
    return notes


#: How a merge is performed. These repos squash by default; a merge commit per
#: ticket makes `develop` unreadable, which is the shape of history Arun keeps.
MERGE_METHODS = {"squash": "--squash", "merge": "--merge", "rebase": "--rebase"}


async def merge_state(pr: str, workspace: str, repo: str = "") -> dict:
    """Everything that decides whether this PR may be merged, read from GitHub.

    Read separately from the merge itself so the offer Arun sees carries the real
    state — "CI green, 2 approvals, no conflicts" — rather than a promise that it
    was checked. He is approving a fact, not a hope.
    """
    cwd = _repo_dir(workspace, repo)
    rc, out = await _gh(cwd, "gh", "pr", "view", str(pr).lstrip("#"), "--json",
                        "number,title,state,isDraft,mergeable,mergeStateStatus,"
                        "reviewDecision,statusCheckRollup,headRefName,baseRefName")
    if rc != 0:
        raise RuntimeError(f"could not read PR {pr}: {out.strip()[:200]}")
    try:
        data = json.loads(out)
    except ValueError as exc:
        raise RuntimeError(f"could not parse PR {pr}: {exc}") from exc
    checks = data.get("statusCheckRollup") or []
    failing = [c.get("name") or c.get("context") or "?" for c in checks
               if (c.get("conclusion") or c.get("state") or "").upper()
               in ("FAILURE", "ERROR", "CANCELLED", "TIMED_OUT")]
    pending = [c.get("name") or c.get("context") or "?" for c in checks
               if (c.get("conclusion") or c.get("state") or "").upper()
               in ("", "PENDING", "IN_PROGRESS", "QUEUED", "EXPECTED")]
    return {
        "number": data.get("number"), "title": data.get("title", ""),
        "state": data.get("state", ""), "draft": bool(data.get("isDraft")),
        "mergeable": data.get("mergeable", ""),
        "merge_state": data.get("mergeStateStatus", ""),
        "review": data.get("reviewDecision", ""),
        "head": data.get("headRefName", ""), "base": data.get("baseRefName", ""),
        "failing": failing, "pending": pending, "checks": len(checks),
    }


def merge_blockers(state: dict) -> list[str]:
    """Why this must not be merged right now. Empty means it may be.

    Every one of these is something Arun would be embarrassed by afterwards, and
    every one is knowable BEFORE the button is pressed. A merge is the single
    least reversible thing in the whole system — it puts code on the branch other
    people build from — so the check is deliberately conservative and says which
    part failed rather than a bare refusal.
    """
    out = []
    if (state.get("state") or "").upper() != "OPEN":
        out.append(f"the PR is {(state.get('state') or 'not open').lower()}")
    if state.get("draft"):
        out.append("it is still a draft")
    if (state.get("mergeable") or "").upper() == "CONFLICTING":
        out.append(f"it conflicts with {state.get('base') or 'the base branch'}")
    if state.get("failing"):
        out.append("CI is red: " + ", ".join(state["failing"][:4]))
    if state.get("pending"):
        out.append("CI has not finished: " + ", ".join(state["pending"][:4]))
    if (state.get("review") or "").upper() == "CHANGES_REQUESTED":
        out.append("a reviewer has requested changes")
    return out


def merge_summary(state: dict) -> str:
    """The state, as he would want it read out before saying yes."""
    checks = ("no CI configured" if not state.get("checks")
              else "CI red" if state.get("failing")
              else "CI still running" if state.get("pending") else "CI green")
    review = {"APPROVED": "approved", "CHANGES_REQUESTED": "changes requested",
              "REVIEW_REQUIRED": "not yet approved", "": "no review decision"}.get(
                  (state.get("review") or "").upper(), state.get("review", ""))
    return (f"#{state.get('number')} {state.get('title', '')[:80]}\n"
            f"{state.get('head', '?')} → {state.get('base', '?')} · {checks} · {review}")


async def merge(pr: str, workspace: str, repo: str = "", method: str = "squash",
                delete_branch: bool = True) -> str:
    """Merge the PR. Re-checks the blockers immediately before doing it.

    Re-checked rather than trusted: the state was read when the offer was made,
    and Arun may say yes an hour later — by which time CI can have gone red or
    somebody can have pushed a conflict. The gap between deciding and doing is
    exactly where an unreversible action goes wrong.
    """
    flag = MERGE_METHODS.get(method)
    if flag is None:
        raise RuntimeError(f"unknown merge method '{method}' — one of: "
                           + ", ".join(sorted(MERGE_METHODS)))
    state = await merge_state(pr, workspace, repo)
    blockers = merge_blockers(state)
    if blockers:
        raise RuntimeError("did NOT merge — " + "; ".join(blockers))
    cwd = _repo_dir(workspace, repo)
    args = ["gh", "pr", "merge", str(pr).lstrip("#"), flag]
    if delete_branch:
        args.append("--delete-branch")
    rc, out = await _gh(cwd, *args, timeout=180)
    if rc != 0:
        raise RuntimeError(f"merge failed: {out.strip()[:300]}")
    return (f"merged #{state['number']} ({method}) into {state['base']}"
            + (" and deleted the branch" if delete_branch else ""))
