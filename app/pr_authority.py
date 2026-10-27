"""His "review it, approve and merge if good", kept as one durable instruction.

A standalone review used to end in two offers — post the review, then merge —
each waiting for its own yes, although he had already said both in the ask.
And when the PR moved while the review ran, the findings were simply dropped
("Not staged: the PR head changed") with nobody told to look again.

The instruction is recorded once, server-side, from HIS words in the turn that
asked for the review — never from tool arguments, the PR body or a colleague's
message — and only for the PR those words name. It is bound to the review task
and survives restarts with it. The review itself is still the condition: only a
verdict of approve, with no blocking finding, on the exact head the task read,
with green CI, is acted on. Anything else is staged for his yes as before.

Every write claims a receipt first, so a timeout, restart or second poll never
repeats an approval or a merge that may already have reached GitHub. Revocation
("don't merge") is checked again immediately before each write. Repository
protections and GitHub's self-approval rule are respected, never worked around.
"""
from __future__ import annotations

import asyncio
import json
import re
import time

from . import store

PREFIX = "pr_authority:"
RECEIPT = "pr_authority_receipt:"
#: Re-reviews when the PR head moves within scope, before it comes back to him.
MAX_REREVIEWS = 2
#: An instruction nobody has acted on for this long is stale, not standing.
TTL_SECONDS = 3 * 24 * 3600
_APPROVE = re.compile(r"\bapprove\b(?!\s+(?:from|by)\b)", re.I)
_LIVE = ("reviewing", "merge_pending")


def wants_approve(words: str) -> bool:
    from . import go
    for m in _APPROVE.finditer(words or ""):
        if not go._NOT.search((words or "")[:m.start()]):
            return True
    return False


def _names(words: str, ref: str) -> bool:
    """His words name this PR: a link to exactly it, or (with no link) its number."""
    from . import review
    links = []
    for m in review._PR_LINK.finditer(words or ""):
        owner, repo, number = (m.group(1), m.group(2), m.group(3)) if m.group(3) \
            else (m.group(4), m.group(5), m.group(6))
        links.append(f"{owner}/{repo}#{number}".lower())
    if links:
        return ref.lower() in links
    number = ref.rsplit("#", 1)[-1]
    return bool(re.search(rf"(?<![\w/.-])#?{re.escape(number)}\b", words or ""))


def _key(task_id) -> str:
    return f"{PREFIX}{task_id}"


def _save(task_id, rec: dict) -> None:
    store.kv_set(_key(task_id), json.dumps(rec))


def record(task_id: int, ref: str, revision: str, words: str, *, url: str = "",
           author: str = "", workspace: str = "", repo: str = "",
           rereviews: int = 0, now: float | None = None) -> dict | None:
    """Save the instruction when his words grant approve and/or merge for `ref`."""
    approve, merge = wants_approve(words), _wants_merge(words)
    if not (approve or merge) or not _names(words, ref):
        return None
    rec = {"version": 1, "ref": ref, "revision": revision, "url": url or ref,
           "approve": approve, "merge": merge, "words": (words or "")[:300],
           "author": author, "workspace": workspace, "repo": repo,
           "rereviews": rereviews, "state": "reviewing",
           "at": time.time() if now is None else now}
    _save(task_id, rec)
    store.add_task_event(task_id, "authority",
                         f"{'approve' if approve else ''}{' + ' if approve and merge else ''}"
                         f"{'merge' if merge else ''} {ref} if the review is good")
    return rec


def _wants_merge(words: str) -> bool:
    from . import go
    return go.wants_merge(words)


def get(task_id, now: float | None = None) -> dict | None:
    """The live instruction for this task, or None (revoked, finished, stale)."""
    try:
        rec = json.loads(store.kv_get(_key(task_id)) or "null")
    except ValueError:
        return None
    if not isinstance(rec, dict) or rec.get("version") != 1 or rec.get("state") not in _LIVE:
        return None
    if (time.time() if now is None else now) - float(rec.get("at") or 0) > TTL_SECONDS:
        return None
    return rec


def _finish(task_id, rec: dict, state: str) -> None:
    _save(task_id, {**rec, "state": state, "ended_at": time.time()})


def active() -> list[tuple[int, dict]]:
    out = []
    for key in store.kv_like(PREFIX + "%"):
        tid = key[len(PREFIX):]
        rec = get(tid) if tid.isdigit() else None
        if rec:
            out.append((int(tid), rec))
    return out


def revoke(task_id: int | None = None) -> int:
    """End the instruction for one task or PR number, or every live one."""
    ended = 0
    for tid, rec in active():
        if task_id is None or tid == task_id or rec["ref"].endswith(f"#{task_id}"):
            _finish(tid, rec, "revoked")
            store.add_task_event(tid, "authority", f"revoked for {rec['ref']}")
            ended += 1
    return ended


def _claim(name: str, detail: dict) -> bool:
    with store._connect() as conn:
        cur = conn.execute("INSERT OR IGNORE INTO kv(key,value) VALUES (?,?)",
                           (RECEIPT + name, json.dumps({**detail, "state": "sending",
                                                        "at": time.time()})))
        return cur.rowcount == 1


def _receipt(name: str, state: str, **detail) -> None:
    store.kv_set(RECEIPT + name, json.dumps({**detail, "state": state, "at": time.time()}))


def _head(revision: str) -> str:
    return revision.rsplit("@", 1)[-1].split(":", 1)[0]


def _allowed(task_id, act: str, ref: str) -> bool:
    from . import policy
    rec = get(task_id)
    return bool(rec and rec.get(act) and rec["ref"] == ref
                and policy.check(act if act == "merge" else "review", ref, asked=True).ok)


async def on_review(task_id, *, current: str, good: bool, action: str,
                    findings: list[dict], body: str) -> str | None:
    """The review task's verdict, against his saved instruction.

    None: no instruction applies — stage for his yes exactly as before.
    Otherwise the line he reads; nothing is staged.
    """
    rec = get(task_id)
    if not rec or not current.startswith(rec["ref"] + "@"):
        return None
    if _head(current) != _head(rec["revision"]):
        return await rereview(task_id, rec)
    if action != "approve" or any(f.get("blocking") for f in findings) or not good:
        _finish(task_id, rec, "not_good")
        return None       # his condition was "if good": the findings go to him
    lines = []
    if rec["approve"]:
        lines.append(await _approve(task_id, rec, current, findings, body))
    if rec["merge"]:
        rec = {**get(task_id), "state": "merge_pending"} if get(task_id) else None
        if rec is None:
            lines.append("Merge not attempted — the instruction was revoked.")
        else:
            _save(task_id, rec)
            lines.append(await merge_now(task_id) or
                         f"Merge of {rec['ref']} is waiting on GitHub's requirements; "
                         f"I'll merge it when they're met, under your instruction.")
    else:
        _finish(task_id, get(task_id) or rec, "done")
    return "\n".join(line for line in lines if line)


async def _approve(task_id, rec: dict, current: str, findings: list[dict], body: str) -> str:
    from . import ops, review
    if (rec.get("author") or "").lower() in await review.my_logins():
        return (f"Not approved: {rec['ref']} is your own PR, and GitHub does not let an "
                f"author approve it.")
    name = f"{task_id}:approve:{current}"
    if not _claim(name, {"ref": rec["ref"], "revision": current}):
        return f"Approval of {rec['ref']} at this revision was already sent."
    if not _allowed(task_id, "approve", rec["ref"]):
        store.kv_del(RECEIPT + name)
        return f"Not approved: your instruction for {rec['ref']} was revoked."
    try:
        out = await ops.run({"name": "pr_review_inline", "args": {
            "pr": rec["ref"], "workspace": rec.get("workspace", ""), "repo": rec.get("repo", ""),
            "action": "approve", "body": body, "comments": findings,
            "review_origin": {"ref": rec["ref"], "revision": current}}})
    except (RuntimeError, asyncio.CancelledError) as exc:
        _receipt(name, "uncertain", ref=rec["ref"], why=str(exc)[:200])
        if isinstance(exc, asyncio.CancelledError):
            raise
        return f"Approval of {rec['ref']} not confirmed — {exc}. Not retrying blind."
    if out.startswith("⛔"):
        store.kv_del(RECEIPT + name)
        _finish(task_id, rec, "blocked")
        return out
    _receipt(name, "sent", ref=rec["ref"], result=out[:200])
    return f"{out} — under your instruction."


async def merge_now(task_id) -> str | None:
    """Merge once when GitHub's requirements are met. None: still waiting."""
    from . import review
    rec = get(task_id)
    if not rec or rec.get("state") != "merge_pending" or not _allowed(task_id, "merge", rec["ref"]):
        return None
    name = f"{task_id}:merge"
    if store.kv_get(RECEIPT + name):
        return None          # sent or uncertain: reconciled by reading, never resent
    try:
        state = await review.merge_state(rec["ref"], rec.get("workspace", ""), rec.get("repo", ""))
    except RuntimeError:
        return None
    if (state.get("state") or "").upper() == "MERGED":
        _finish(task_id, rec, "done")
        return None
    if state.get("head_oid") and state["head_oid"].lower() != _head(rec["revision"]):
        return await rereview(task_id, rec)
    if review.merge_blockers(state) or not state.get("checks"):
        return None
    if not _claim(name, {"ref": rec["ref"], "head": state["head_oid"]}):
        return None
    try:
        result = await review.merge(rec["ref"], rec.get("workspace", ""), rec.get("repo", ""),
                                    expected_head=state["head_oid"],
                                    authorized=lambda: _allowed(task_id, "merge", rec["ref"]))
    except review.MergeBlocked:
        store.kv_del(RECEIPT + name)     # nothing was sent; current evidence is rechecked
        return None
    except (RuntimeError, asyncio.CancelledError) as exc:
        _receipt(name, "uncertain", ref=rec["ref"], head=state["head_oid"], why=str(exc)[:200])
        if isinstance(exc, asyncio.CancelledError):
            raise
        return f"Merge of {rec['ref']} not confirmed — {exc}. Checking GitHub before any retry."
    _receipt(name, "requested", ref=rec["ref"], head=state["head_oid"], result=result[:200])
    _finish(task_id, rec, "done")
    return f"🔀 {rec['ref']}: {result} — under your instruction."


async def rereview(task_id, rec: dict) -> str:
    """The PR moved while it was being reviewed: review the new head, same instruction."""
    from . import review, tasks
    _finish(task_id, rec, "superseded")
    if rec.get("rereviews", 0) >= MAX_REREVIEWS:
        return (f"{rec['ref']} kept changing ({MAX_REREVIEWS + 1} reviews). Stopped; nothing "
                f"approved or merged. Ask again when it settles.")
    try:
        current, _ = await review.revision(rec["ref"])
        text, meta = await review.brief(rec["url"], rec.get("workspace", ""), rec.get("repo", ""))
    except (RuntimeError, ValueError) as exc:
        return f"{rec['ref']} changed and I could not read it again — {exc}. Nothing approved."
    t = tasks.spawn(f"Re-review {rec['ref']} at its new head", text, "analysis",
                    rec.get("workspace") or None)
    store.kv_set(f"review_origin:{t['id']}", json.dumps({"ref": rec["ref"], "revision": current}))
    _save(t["id"], {**rec, "revision": current, "state": "reviewing",
                    "rereviews": rec.get("rereviews", 0) + 1, "at": time.time(),
                    "from_task": task_id})
    store.add_task_event(t["id"], "authority", f"carried from task #{task_id}: head moved")
    return (f"🔄 {rec['ref']} changed while I reviewed it. Task #{t['id']} is reviewing the "
            f"new head under the same instruction; nothing was approved or merged.")


async def sweep() -> list[str]:
    """Pending merges whose requirements may now be met. Called by the PR watcher."""
    notes = []
    for tid, rec in active():
        if rec.get("state") == "merge_pending":
            note = await merge_now(tid)
            if note:
                notes.append(note)
    return notes
