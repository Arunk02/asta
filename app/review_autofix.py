"""A review comment on Asta's PR is answered by Asta: judged, fixed, replied to.

His words, 17 Nov: "if it review comments, it has to auto work on see if the
review is valid it should fix and inform the ppl automatically, even if someone
comments also". Until then a new comment on a task's PR became a phone line —
"Say 'fix #<task>'" — and on a booking-service PR the reviewer's null-pointer comment took
four messages and twenty minutes to reach the task. Inline comments were not
read at all.

The round, for each new human comment on a task's open PR (tasks.check_pr):
  1. judged — valid (a real defect, a reasonable change, a correct convention
     point) or not, with one line of why, by a brain that reads the diff;
  2. the valid ones go to the SAME task as feedback, plan pre-approved — his
     standing instruction is the yes;
  3. when the fix is pushed onto the PR, each fixed comment gets a reply on its
     own thread: what changed and in which commit. GitHub tells the reviewer;
     Arun gets one line on his phone. A comment judged NOT valid is never
     argued with in his name — he gets the reason and decides.
Always on, and only for his own pull requests (his_change).
Never more than three rounds per task: a fourth comment is his to call.
"""
from __future__ import annotations

import json
import re

from . import store

MAX_ROUNDS = 3
_KEY = "review_autofix:"
_ROUNDS = "review_autofix_rounds:"
#: "owner (path:line) [c123]: body" — the id an inline comment is replied to by.
_COMMENT_ID = re.compile(r"\[c(\d+)\]")
_PR = re.compile(r"github\.com/([^/]+)/([^/]+)/pull/(\d+)")


async def his_change(pr: dict) -> bool:
    """Always on, for HIS pull requests only: the PR's author is his GitHub login.

    A task can be pointed at a colleague's PR ("fix CT on AP PR 1252"); their
    reviewers' comments on their PR are theirs to answer, not Asta's. Unknown
    author or login = not his: nothing automatic, he is told as before."""
    from . import ci_watch
    author = ((pr or {}).get("author") or {}).get("login") or ""
    if not author:
        return False
    try:
        me = await ci_watch.my_login()
    except Exception:                                          # noqa: BLE001
        return False
    return bool(me) and author.lower() == me.lower()


def pending(task_id: int) -> dict | None:
    try:
        rec = json.loads(store.kv_get(_KEY + str(task_id)) or "null")
    except ValueError:
        return None
    return rec if isinstance(rec, dict) else None


async def inline_notes(url: str, me: str = "") -> list[str]:
    """Line comments on the diff — `gh pr view` lists reviews and conversation
    comments but not these, which is where most review findings live."""
    from . import repo_ops, tasks
    m = _PR.search(url or "")
    if not m:
        return []
    owner, repo, number = m.groups()
    rc, out = await repo_ops.git(tasks.ROOT, "gh", "api", "--paginate",
                                 f"repos/{owner}/{repo}/pulls/{number}/comments", timeout=60)
    if rc != 0:
        return []
    try:
        rows = json.loads(out)
    except ValueError:
        return []
    notes = []
    for c in rows if isinstance(rows, list) else []:
        author = ((c.get("user") or {}).get("login") or "").lower()
        body = (c.get("body") or "").strip()
        if not body or author.endswith("[bot]") or author in tasks._BOT_AUTHORS:
            continue
        if me and author == me.lower():
            continue
        if c.get("in_reply_to_id"):
            continue                       # a reply in a thread, not a new finding
        where = f"{c.get('path', '')}:{c.get('line') or c.get('original_line') or ''}"
        notes.append(f"{author} ({where}) [c{c.get('id')}]: {body[:400]}")
    return notes


def _judge_prompt(title: str, diff: str, notes: list[str]) -> str:
    listed = "\n".join(f"{i}. {n}" for i, n in enumerate(notes, 1))
    return (
        f"You wrote the change below ({title}). Reviewers left these comments on its PR:\n"
        f"{listed}\n\nFor EACH comment decide: valid — a real defect (null pointer, broken "
        f"compatibility, wrong business logic, a build or test failure), a reasonable change "
        f"they ask for, or a correct convention point; or not valid — wrong about the code, "
        f"already handled, or outside this change. Read the diff before deciding; quote the "
        f"line that proves it.\nReply with JSON only: "
        f'[{{"n": 1, "valid": true, "why": "one line, with the file:line"}}]\n\n'
        f"DIFF:\n{diff[:15000]}")


def parse_verdicts(raw: str, count: int) -> list[dict] | None:
    """The judge's JSON, or None when it is not usable — never half a verdict."""
    m = re.search(r"\[.*\]", raw or "", re.S)
    if not m:
        return None
    try:
        rows = json.loads(m.group(0))
    except ValueError:
        return None
    out = {}
    for r in rows if isinstance(rows, list) else []:
        if isinstance(r, dict) and isinstance(r.get("n"), int) and 1 <= r["n"] <= count:
            out[r["n"]] = {"valid": bool(r.get("valid")), "why": str(r.get("why") or "")[:300]}
    return [out[i] for i in range(1, count + 1)] if len(out) == count else None


def feedback(notes: list[str], verdicts: list[dict]) -> str:
    """What the task is asked to do: the valid comments, verbatim, and how."""
    asks = [f"- {n}\n  (judged valid: {v['why']})" for n, v in zip(notes, verdicts) if v["valid"]]
    return ("Reviewers asked for these changes on this task's PR. Fix each one, add a test "
            "that fails without the fix, run the tests you touched (mvn clean, never 'Tests "
            "run: 0'), and commit on the same branch. Do not reply to anyone; Asta replies "
            "on the PR once the fix is pushed.\n" + "\n".join(asks))


async def handle(task_id: int, url: str, notes: list[str]) -> str:
    """Judge new comments and start the fix. Returns the line for his phone."""
    from . import agent, notify, prname, repo_ops, tasks
    t = store.get_task(task_id) or {}
    name = prname.from_url(url)
    rounds = int(store.kv_get(_ROUNDS + str(task_id)) or 0)
    listed = "\n".join(f"• {n}" for n in notes[:4])
    if rounds >= MAX_ROUNDS:
        return (f"💬 New comment on {name} (#{task_id})\n{url}\n\n{listed}\n\n"
                f"{MAX_ROUNDS} review rounds already ran on this PR — this one is yours. "
                f"Say 'fix #{task_id}' to send it to the task.")
    rc, diff = await repo_ops.git(tasks.ROOT, "gh", "pr", "diff", url, timeout=60)
    verdicts = parse_verdicts(
        await agent.one_shot_any(_judge_prompt(t.get("title", ""), diff if rc == 0 else "",
                                               notes)), len(notes))
    if verdicts is None:
        return (f"💬 New comment on {name} (#{task_id})\n{url}\n\n{listed}\n\n"
                f"I could not judge it reliably — say 'fix #{task_id}' to address it.")
    store.kv_set(_KEY + str(task_id), json.dumps({
        "url": url, "items": [{"note": n, **v} for n, v in zip(notes, verdicts)]}))
    store.kv_set(_ROUNDS + str(task_id), str(rounds + 1))
    valid = [n for n, v in zip(notes, verdicts) if v["valid"]]
    lines = [f"{'✅ valid' if v['valid'] else '✗ not valid'} — {n[:160]}\n   {v['why']}"
             for n, v in zip(notes, verdicts)]
    if not valid:
        # Disagreeing with a colleague in his name, on a judgement nobody checked,
        # is his call: he gets the reasons, the reviewer gets nothing automatic.
        store.kv_del(_KEY + str(task_id))
        return (f"💬 Review on {name} (#{task_id}) — I'd change nothing:\n"
                + "\n".join(lines) + f"\nNot replied on the PR. Say 'fix #{task_id}' "
                f"if you want it done anyway.")
    try:
        await tasks.refine(task_id, feedback(notes, verdicts), code_change=True,
                           approved_plan=True)
    except ValueError as exc:
        store.kv_del(_KEY + str(task_id))
        return (f"💬 Review on {name} (#{task_id}) — {len(valid)} valid, but the task could "
                f"not take it: {exc}\n" + "\n".join(lines))
    store.add_task_event(task_id, "gate", "approved: review comments (his standing rule, "
                                          "17 Nov — valid review comments are fixed)")
    return (f"💬 Review on {name} (#{task_id}) — fixing {len(valid)} valid comment(s) now; "
            f"I'll push to the same PR and reply on each thread:\n" + "\n".join(lines))


def _reply_text(item: dict, sha: str) -> str:
    return (f"Fixed in {sha[:10]}: {item.get('why', '')}" if sha else
            f"Fixed: {item.get('why', '')}")


async def after_push(task_id: int, sha: str = "") -> int:
    """Reply on each comment's thread once the fix is on the PR. Returns replies posted."""
    from . import repo_ops, tasks
    rec = pending(task_id)
    if not rec:
        return 0
    m = _PR.search(rec.get("url", ""))
    if not m:
        store.kv_del(_KEY + str(task_id))
        return 0
    owner, repo, number = m.groups()
    posted = 0
    general = []
    for item in rec.get("items", []):
        if not item.get("valid"):
            continue                       # his to answer — see handle()
        body = _reply_text(item, sha)
        cid = _COMMENT_ID.search(item.get("note", ""))
        if cid:
            rc, _ = await repo_ops.git(
                tasks.ROOT, "gh", "api", "--method", "POST",
                f"repos/{owner}/{repo}/pulls/{number}/comments/{cid.group(1)}/replies",
                "-f", f"body={body}", timeout=60)
            posted += rc == 0
        else:
            who = item.get("note", "").split(":", 1)[0].split(" (", 1)[0].strip()
            general.append(f"@{who} {body}" if who else body)
    if general:
        rc, _ = await repo_ops.git(tasks.ROOT, "gh", "pr", "comment", rec["url"],
                                   "--body", "\n\n".join(general), timeout=60)
        posted += rc == 0
    store.kv_del(_KEY + str(task_id))
    store.add_task_event(task_id, "inform", f"replied on {posted} review thread(s)")
    return posted


async def fix_hazards(task_id: int, url: str) -> str:
    """Fix what the hazard scan found in Asta's own diff, on the same PR, once."""
    from . import prname, tasks
    try:
        found = json.loads(store.kv_get(f"task_hazards:{task_id}") or "[]")
    except ValueError:
        found = []
    store.kv_del(f"task_hazards:{task_id}")
    store.kv_set(f"hazard_round:{task_id}", "1")
    if not found:
        return ""
    store.kv_set(_KEY + str(task_id), json.dumps({"url": url, "items": []}))
    brief = ("Before anyone reviews this PR, fix what a scan of your own diff found. For "
             "each: fix it, add a test that fails without the fix, run the tests you touched "
             "(mvn clean), commit on the same branch:\n" + "\n".join(f"- {h}" for h in found))
    try:
        await tasks.refine(task_id, brief, code_change=True, approved_plan=True)
    except ValueError as exc:
        store.kv_del(_KEY + str(task_id))
        return f"⚠️ {prname.from_url(url)} (#{task_id}) has hazards I could not send back: {exc}"
    store.add_task_event(task_id, "gate", "approved: hazard fix on its own PR (17 Nov rule)")
    return (f"🔧 Fixing on {prname.from_url(url)} (#{task_id}) before review:\n"
            + "\n".join(f"• {h}" for h in found[:6]))
