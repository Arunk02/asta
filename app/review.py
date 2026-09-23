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

Then CALL `propose_pr_review` with the pull request and your notes exactly as written
above. That is what turns a review into comments the author can act on, each on its own
line of the diff; it posts nothing — Arun sees the review and his yes sends it. Finishing
with the notes in your answer and no call is the failure this whole path exists to fix:
"it telling it analysing but it doesnt able to do and summarise or add comments in PR".
"""


async def _gh(cwd: Path, *args: str, timeout: float = 120) -> tuple[int, str]:
    return await repo_ops.git(cwd, "gh", *args, timeout=timeout)


#: A PR named the way a colleague actually sends it: a full link, or owner/repo#N.
#: Teams strips punctuation out of links in its feed, so `pull/1409` arrives as
#: `pull 1409` — matched either way, because that rendering is the common one.
_PR_LINK = re.compile(
    r"(?:https?://)?(?:www[.\s]+)?github[.\s]+com[/\s]+([\w.-]+)[/\s]+([\w.-]+)"
    r"[/\s]+pull[/\s]+(\d{1,7})"
    r"|\b([\w.-]+)/([\w.-]+)#(\d{1,7})\b", re.I)


def pr_target(pr: str) -> tuple[str, str]:
    """(number, "owner/repo") for a PR he names — repo empty when he gave a bare number.

    "Please review https://github.com/acme/booking/pull/1409" carries everything
    needed, and that is how a review request arrives. Requiring a local clone for
    it — which `_repo_dir` did — turned a complete request into "name the repo as
    well", which is the shape of not answering.
    """
    found = _PR_LINK.search(str(pr or ""))
    if found:
        owner, repo, number = (found.group(1), found.group(2), found.group(3)) \
            if found.group(3) else (found.group(4), found.group(5), found.group(6))
        return number, f"{owner}/{repo}"
    return str(pr or "").strip().lstrip("#"), ""


def _repo_dir(workspace: str, repo: str = "") -> Path:
    root = ws_mod.provider_for(workspace).root
    if repo:
        return Path(root) / repo
    return Path(root) if (Path(root) / ".git").is_dir() else Path(root)


def _where(pr: str, workspace: str, repo: str = "") -> tuple[str, list[str], Path]:
    """(number, extra gh args, cwd) — from the link when there is one, else the clone.

    `gh -R owner/repo` needs no repository around it, so a link is answered from
    anywhere. A bare number still needs the clone that says which repo it means.
    """
    number, target = pr_target(pr)
    if target:
        return number, ["-R", target], Path.home()
    cwd = _repo_dir(workspace, repo)
    if not (cwd / ".git").is_dir():
        raise RuntimeError(f"{cwd} is not a git repository — send the PR link, "
                           f"or name the repo as well.")
    return number, [], cwd


async def gather(pr: str, workspace: str = "", repo: str = "") -> dict:
    """Everything about a PR, from gh. Raises RuntimeError with gh's own message."""
    number, where, cwd = _where(pr, workspace, repo)
    rc, out = await _gh(cwd, "pr", "view", number, *where, "--json",
                        "number,title,author,body,baseRefName,headRefName,url,"
                        "additions,deletions,changedFiles,files,state,isDraft,"
                        "headRepositoryOwner,headRepository")
    if rc != 0:
        raise RuntimeError(f"gh pr view failed: {out[:300]}")
    try:
        meta = json.loads(out)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"could not parse gh output: {exc}") from exc
    rc, diff = await _gh(cwd, "pr", "diff", number, *where, timeout=180)
    if rc != 0:
        diff = f"(diff unavailable: {diff[:200]})"
    rc, checks = await _gh(cwd, "pr", "checks", number, *where, "--json", "name,state")
    meta["checks"] = json.loads(checks) if rc == 0 and checks.strip() else []
    meta["diff"] = diff
    meta["repo_dir"] = str(cwd)
    meta["target"] = (where[1] if where else
                      f"{(meta.get('headRepositoryOwner') or {}).get('login', '')}/"
                      f"{(meta.get('headRepository') or {}).get('name', '')}".strip("/"))
    return meta


async def my_logins() -> set[str]:
    """Every GitHub login that is HIM, lowercased.

    He has two: the work account and the personal one this repository lives
    under. `ci_watch.my_login` answers with whichever `gh` is active, so asking
    it alone called his own pull request somebody else's — and "whose PR is it"
    decides the entire job.
    """
    import os
    from . import ci_watch
    out = {n.strip().lower() for n in
           (os.environ.get("ASTA_GITHUB_LOGINS") or "").split(",") if n.strip()}
    active = (await ci_watch.my_login() or "").strip().lower()
    if active:
        out.add(active)
    # Whatever else gh is signed in as, on any host it knows.
    rc, said = await repo_ops.git(Path.home(), "gh", "auth", "status", timeout=20)
    if rc == 0:
        out |= {m.group(1).lower() for m in
                re.finditer(r"account\s+([\w-]+)\s+\(", said)}
    return out


async def whose_pr(meta: dict) -> str:
    """"his" when Arun opened it, "theirs" otherwise — which decides the whole job.

    "Please review my PR" was read as "a colleague left feedback on YOUR PR", so
    Asta went off to check whether their points were right, on a PR that had no
    points and was not his. The author settles it.
    """
    author = ((meta.get("author") or {}).get("login") or "").lower()
    return "his" if author and author in await my_logins() else "theirs"


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
    number, where, cwd = _where(pr, workspace, repo)
    args = ["pr", "review", number, *where, flag]
    if body:
        args += ["--body", body]
    rc, out = await _gh(cwd, *args)
    if rc != 0:
        raise RuntimeError(f"gh pr review failed: {out[:300]}")
    verb = {"approve": "Approved", "comment": "Commented on",
            "request_changes": "Requested changes on"}[action]
    return f"{verb} PR #{pr.lstrip('#')}"


async def review_own_diff(diff: str, workspace: str = "", reviewer: str = "",
                          cwd: str = "") -> str:
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
    notes = ""
    if reviewer:
        notes = await _second_brain(reviewer, prompt, cwd)
    if not notes:
        notes = (await memory.cheap_complete(prompt, 500, paid_ok=True) or "").strip()
    if not notes or notes.upper().startswith("LOOKS SOUND"):
        return ""
    return notes


async def _second_brain(reviewer: str, prompt: str, cwd: str) -> str:
    """The review, read by a brain OTHER than the one that wrote the change.

    A model grading its own diff shares every blind spot that produced it. A
    different model, with the repo open and writing denied, is the cheapest
    second pair of eyes there is. '' on any failure — the caller falls back.
    """
    from . import claude_cli, copilot_cli
    cli = {"claude": claude_cli, "copilot": copilot_cli}.get(reviewer)
    if cli is None:
        return ""
    try:
        return (await cli.one_shot(prompt.replace("You wrote this change.",
                                                  "A colleague's AI wrote this change."),
                                   cwd=cwd or None, timeout=300, effort="medium",
                                   plan_only=True) or "").strip()
    except Exception:                                   # noqa: BLE001
        return ""


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


# --- findings, as comments on the lines they are about --------------------------------
#
# "PR review it telling it analysing but it doesnt able to do and summarise or add
# comments in PR". The brief already asks for `path:line — what is wrong → what to
# do`, and every one of those was flattened into one blob of text posted as a
# single comment, if it was posted at all. A review the author can act on is a
# comment ON THE LINE, and GitHub takes them all in one review.

#: `- \`path/to/File.java:88\` — what is wrong → what to do`, in any of the shapes
#: a model writes it: backticks or not, an em dash, a colon or an arrow.
_FINDING = re.compile(
    r"^\s*[-*]\s*`?([\w./+-]+\.[\w]{1,8})`?\s*[:#]\s*L?(\d{1,6})`?\s*"
    r"(?:[—\-–:]|->|→)?\s*(.+)$", re.M)

#: The headings the brief asks for, and whether a finding under one blocks a merge.
_SECTIONS = (("BLOCKING", True), ("NON-BLOCKING", False), ("TESTS", False),
             ("QUESTIONS", False))


def parse_findings(notes: str) -> list[dict]:
    """[{path, line, body, blocking}] from a worker's review notes.

    Anything that does not name a file and a line stays out: a comment GitHub
    cannot attach is a comment that silently disappears, and a review that half
    lands is worse than one that does not.
    """
    out: list[dict] = []
    section, blocking = "", False
    for raw in (notes or "").splitlines():
        head = raw.strip().rstrip(":").upper()
        for name, blocks in _SECTIONS:
            if head.startswith(name):
                section, blocking = name, blocks
                break
        found = _FINDING.match(raw)
        if not found:
            continue
        body = found.group(3).strip(" -—–").strip()
        if not body:
            continue
        if section == "QUESTIONS":
            body = "Question: " + body
        out.append({"path": found.group(1), "line": int(found.group(2)),
                    "body": body[:1500], "blocking": blocking and section == "BLOCKING"})
    return out


def verdict_of(notes: str) -> str:
    """APPROVE / COMMENT / REQUEST CHANGES as the worker stated it, lowercased to
    the action names. Anything unclear is a comment: never an approval by default."""
    found = re.search(r"^\s*VERDICT:\s*(APPROVE|COMMENT|REQUEST[ _]CHANGES)",
                      notes or "", re.I | re.M)
    if not found:
        return "comment"
    said = found.group(1).upper().replace(" ", "_")
    return {"APPROVE": "approve", "COMMENT": "comment",
            "REQUEST_CHANGES": "request_changes"}[said]


async def post_inline_review(pr: str, workspace: str = "", repo: str = "",
                             action: str = "comment", body: str = "",
                             comments: list | None = None) -> str:
    """One review carrying inline comments, through GitHub's own reviews API.

    `gh pr review` can only post a single body, which is why every finding used
    to arrive as one wall of text. The API takes `comments: [{path, line, body}]`,
    and they land on the lines they are about.

    A comment on a line outside the diff is rejected by GitHub with the whole
    review — so a rejection is retried once, as a plain review body carrying the
    same findings, rather than losing them.
    """
    flag = ACTIONS.get(action)
    if flag is None:
        raise RuntimeError(f"unknown review action '{action}' — one of: "
                           + ", ".join(sorted(ACTIONS)))
    number, where, cwd = _where(pr, workspace, repo)
    target = where[1] if where else ""
    if not target:
        rc, out = await _gh(cwd, "repo", "view", "--json", "nameWithOwner",
                            "--jq", ".nameWithOwner")
        target = out.strip() if rc == 0 else ""
    if not target:
        raise RuntimeError("could not tell which repository that PR is in")
    rows = [c for c in (comments or []) if c.get("path") and c.get("line")]
    if not rows:
        return await post_review(pr, workspace, repo, action, body)
    payload = {"event": {"approve": "APPROVE", "comment": "COMMENT",
                         "request_changes": "REQUEST_CHANGES"}[action],
               "body": (body or "").strip(),
               "comments": [{"path": c["path"], "line": int(c["line"]), "side": "RIGHT",
                             "body": str(c["body"])[:1500]} for c in rows]}
    rc, out = await repo_ops.git(cwd, "gh", "api", "--method", "POST",
                                 f"repos/{target}/pulls/{number}/reviews",
                                 "--input", "-", stdin=json.dumps(payload), timeout=120)
    if rc != 0:
        # Most often: a line that is not part of the diff. Say so, and still
        # deliver the findings rather than dropping them on the floor.
        listed = "\n".join(f"- `{c['path']}:{c['line']}` — {c['body']}" for c in rows)
        note = ((body or "").strip() + "\n\n" + listed).strip()
        await post_review(pr, workspace, repo, action, note)
        return (f"Posted the review on PR #{number} as one comment — GitHub would not "
                f"take the inline ones ({out.strip()[:120]})")
    verb = {"approve": "Approved", "comment": "Commented on",
            "request_changes": "Requested changes on"}[action]
    return f"{verb} PR #{number} with {len(rows)} inline comment(s)"
