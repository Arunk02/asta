"""What happened to each comment posted as Arun: fixed, disputed, or still open (12 Nov).

A review is only as good as what the author does with it. Each posted comment is
kept with the head it was made on; later the PR is read again:
  * accepted — a later commit changed the lines the comment was on;
  * disputed — the author replied on its thread that it is not a real issue.
Disputes are fed back into the next review of that repo (`review_proof.needs`),
and the week's numbers go into the journal review. A dispute raised on Teams
instead of GitHub is not seen here.
"""
from __future__ import annotations

import json
import re
import time

from . import store

POSTED = "review_posted:"
DISPUTES = "review_disputes:"
WATCH_DAYS = 21
CHECK_EVERY = 2 * 3600

_DISPUTE = re.compile(
    r"\b(?:not\s+(?:possible|valid|needed|required|an?\s+issue|applicable|true|correct|"
    r"a\s+(?:bug|problem))|(?:can'?t|cannot|won'?t|will\s+not|never)\s+happen|invalid|"
    r"no\s+need|incorrect|wrong|false\s+positive|doesn'?t\s+apply|by\s+design)\b", re.I)
_HUNK = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+\d+(?:,\d+)? @@", re.M)


def record_posted(target: str, number: str, head: str, comments: list[dict],
                  now: float | None = None) -> None:
    """Keep what was posted, on which head — read back later by `sweep`."""
    rows = [{"path": c.get("path", ""), "line": int(c.get("line") or 0),
             "body": str(c.get("body") or "")[:400], "status": "pending"}
            for c in comments or [] if c.get("path")]
    if not rows or not head:
        return
    store.kv_set(f"{POSTED}{target.lower()}#{number}", json.dumps(
        {"target": target, "number": str(number), "head": head,
         "at": time.time() if now is None else now, "checked": 0, "comments": rows}))


def disputes(repo: str) -> list[dict]:
    try:
        rows = json.loads(store.kv_get(f"{DISPUTES}{(repo or '').lower()}") or "[]")
    except ValueError:
        return []
    return rows if isinstance(rows, list) else []


def _add_dispute(repo: str, row: dict) -> None:
    rows = disputes(repo)
    rows.append(row)
    store.kv_set(f"{DISPUTES}{repo.lower()}", json.dumps(rows[-20:]))


def touched(patch: str, line: int, slack: int = 3) -> bool:
    """Does this patch (old head → new head) change lines near `line` of the old file?"""
    for m in _HUNK.finditer(patch or ""):
        start, count = int(m.group(1)), int(m.group(2) or 1)
        if start - slack <= line <= start + max(count, 1) + slack:
            return True
    return False


def is_dispute(reply: str) -> bool:
    return bool(_DISPUTE.search(reply or ""))


async def _check(rec: dict, mine: set[str]) -> dict:
    from pathlib import Path
    from . import review
    target, number, head = rec["target"], rec["number"], rec["head"]
    rc, out = await review._gh(Path.home(), "api", f"repos/{target}/pulls/{number}",
                               "--jq", "[.head.sha, .state] | @tsv", timeout=30)
    if rc != 0:
        return rec
    now_head = (out.strip().split("\t") + [""])[0]
    patches: dict[str, str] = {}
    if now_head and now_head != head:
        rc, raw = await review._gh(Path.home(), "api", f"repos/{target}/compare/{head}...{now_head}",
                                   "--jq", ".files[] | {filename, patch}", timeout=60)
        for line in raw.splitlines() if rc == 0 else []:
            try:
                f = json.loads(line)
            except ValueError:
                continue
            patches[f.get("filename") or ""] = f.get("patch") or ""
    rc, raw = await review._gh(Path.home(), "api", "--paginate", f"repos/{target}/pulls/{number}/comments",
                               "--jq", ".[] | {id, in_reply_to_id, path, line, original_line, body, "
                                       "login: .user.login}", timeout=60)
    threads = []
    for line in raw.splitlines() if rc == 0 else []:
        try:
            threads.append(json.loads(line))
        except ValueError:
            continue
    repo = target.rsplit("/", 1)[-1]
    for c in rec["comments"]:
        if c["status"] != "pending":
            continue
        ours = [t for t in threads if (t.get("login") or "").lower() in mine
                and t.get("path") == c["path"] and not t.get("in_reply_to_id")
                and (t.get("body") or "")[:40] == c["body"][:40]]
        replies = [t for t in threads if ours and t.get("in_reply_to_id") == ours[0]["id"]
                   and (t.get("login") or "").lower() not in mine]
        said = next((r["body"] for r in replies if is_dispute(r.get("body") or "")), "")
        if said:
            c["status"] = "disputed"
            _add_dispute(repo, {"path": c["path"], "body": c["body"], "reply": said[:300],
                                "at": time.time()})
        elif touched(patches.get(c["path"], ""), c["line"]):
            c["status"] = "accepted"
    return rec


async def sweep(now: float | None = None) -> int:
    """Read back the comments posted in the last three weeks. Returns how many were checked."""
    from . import review
    now = time.time() if now is None else now
    try:
        mine = await review.my_logins()
    except Exception:                                            # noqa: BLE001
        mine = set()
    done = 0
    for key, raw in store.kv_like(POSTED + "%").items():
        try:
            rec = json.loads(raw)
        except ValueError:
            store.kv_del(key)
            continue
        if now - float(rec.get("at") or now) > WATCH_DAYS * 86400:
            continue                     # kept for the score, no longer read
        if now - float(rec.get("checked") or 0) < CHECK_EVERY:
            continue
        if not any(c["status"] == "pending" for c in rec.get("comments") or []):
            continue
        try:
            rec = await _check(rec, mine)
        except Exception:                                        # noqa: BLE001
            continue
        rec["checked"] = now
        store.kv_set(key, json.dumps(rec))
        done += 1
    return done


def score(days: int = 7, now: float | None = None) -> dict:
    """Comments posted in the window, by what happened to them."""
    now = time.time() if now is None else now
    out = {"accepted": 0, "disputed": 0, "pending": 0}
    for raw in store.kv_like(POSTED + "%").values():
        try:
            rec = json.loads(raw)
        except ValueError:
            continue
        if now - float(rec.get("at") or 0) > days * 86400:
            continue
        for c in rec.get("comments") or []:
            out[c.get("status", "pending")] = out.get(c.get("status", "pending"), 0) + 1
    return out


def score_line(days: int = 7) -> str:
    s = score(days)
    total = sum(s.values())
    if not total:
        return ""
    judged = s["accepted"] + s["disputed"]
    rate = f" — {round(100 * s['accepted'] / judged)}% of those judged were right" if judged else ""
    return (f"Review comments posted ({days}d): {total} · {s['accepted']} fixed by the author · "
            f"{s['disputed']} disputed · {s['pending']} open{rate}")
