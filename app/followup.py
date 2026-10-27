"""A commitment that outlives the turn it was made in.

*"track on with alex and get this all these 3 PR's for hot priority, get review
and merged by tmr EOD, if not done let me know what issue notify me"* is not a
question and it is not a code task. It is a promise: keep looking, chase the
person, and speak up BEFORE the deadline rather than after it.

Asta could do none of that. A chat turn answers once and forgets. `pr_watch_loop`
follows only PRs Asta itself shipped — two of those three were raised by hand, so
they were invisible to it. And a reminder fires once and tells nobody but Arun,
which is the opposite of chasing: it moves the work back onto him.

What this adds, and deliberately nothing more:

* Any PR by URL, whoever raised it.
* A DEADLINE, warned before it lands. "Let me know if it isn't done" said after
  EOD is a report; said four hours before, it is still something he can act on.
* A NUDGE to the person, when a PR has not moved. An authorized task or contact
  grant covers it without another yes. Otherwise it stays a `teams_draft`.
  Authorized reminders observe working hours and durable attempt limits.
"""

from __future__ import annotations

import json
import os
import time

from . import store

KEY = "followups"
POLL_SECONDS = int(os.environ.get("ASTA_FOLLOWUP_POLL", "900"))
#: How long before the deadline to speak up. After it, the news is only a report.
WARN_BEFORE = float(os.environ.get("ASTA_FOLLOWUP_WARN_HOURS", "4")) * 3600
#: No movement for this long, with a deadline in sight, is when a nudge earns
#: its keep. Below it the person is simply working.
STALL_SECONDS = float(os.environ.get("ASTA_FOLLOWUP_STALL_HOURS", "5")) * 3600
#: One nudge per person per this window, however many PRs are stuck.
NUDGE_EVERY = float(os.environ.get("ASTA_FOLLOWUP_NUDGE_HOURS", "12")) * 3600


def _load() -> list[dict]:
    try:
        rows = json.loads(store.kv_get(KEY) or "[]")
    except (ValueError, TypeError):
        return []
    return [r for r in rows if isinstance(r, dict) and r.get("id")]


def _save(rows: list[dict]) -> None:
    store.kv_set(KEY, json.dumps(rows[-50:]))


def track(goal: str, urls: list[str], person: str = "", due_at: float = 0.0,
          *, authorized_words: str = "") -> dict:
    """Start following something through. Idempotent on the same URL set, so
    saying it twice does not produce two trackers both nudging the same person."""
    urls = [u.strip() for u in (urls or []) if u and u.strip()]
    if not urls:
        raise ValueError("nothing to track — give at least one PR url")
    rows = _load()
    for r in rows:
        if r.get("status") == "open" and set(r.get("urls") or []) == set(urls):
            r["goal"] = goal or r["goal"]
            r["person"] = person or r.get("person", "")
            r["due_at"] = due_at or r.get("due_at", 0.0)
            if authorized_words:
                r["authorization"] = {"person": person.strip(), "urls": sorted(urls),
                                      "words": authorized_words, "at": time.time()}
            _save(rows)
            return r
    now = time.time()
    row = {"id": max([r["id"] for r in rows], default=0) + 1,
           "goal": (goal or "").strip()[:200], "urls": urls,
           "person": (person or "").strip(), "due_at": float(due_at or 0),
           "created_at": now, "seen": {}, "last_moved_at": now,
           "last_nudged_at": 0.0, "warned": False, "status": "open"}
    if authorized_words:
        row["authorization"] = {"person": person.strip(), "urls": sorted(urls),
                                "words": authorized_words, "at": now}
    rows.append(row)
    _save(rows)
    return row


def list_open() -> list[dict]:
    return [r for r in _load() if r.get("status") == "open"]


def stop(fid: int, why: str = "") -> str:
    rows = _load()
    for r in rows:
        if r["id"] == int(fid) and r.get("status") == "open":
            r["status"] = "cancelled"
            r["why"] = why[:200]
            _save(rows)
            return f"Stopped following up on #{fid} — {r['goal'][:60]}"
    return f"No open follow-up #{fid}."


def _blocker(pr: dict) -> str:
    """Why this PR is not merged yet, in the words that say what to do about it.

    "Not merged" is not a blocker, it is a restatement. He asked to be told
    *what issue* — so this names the one thing standing in the way.
    """
    from . import tasks
    if pr.get("state") == "MERGED" or pr.get("mergedAt"):
        return ""
    if pr.get("state") == "CLOSED":
        return "closed without merging"
    decision = (pr.get("reviewDecision") or "").upper()
    checks = tasks._checks_verdict(pr)
    if decision == "CHANGES_REQUESTED":
        return "changes requested"
    if checks == "red":
        return "CI red"
    if checks == "pending":
        return "CI still running"
    if decision == "APPROVED":
        return "approved, waiting to be merged"
    return "no review yet"


def _state_of(pr: dict) -> str:
    """A fingerprint that changes when something a human would call progress
    happens — so 'still red' is never reported as news."""
    from . import tasks
    return (f"{pr.get('state')}/{pr.get('reviewDecision') or '-'}"
            f"/{tasks._checks_verdict(pr)}")


async def check_one(row: dict, now: float | None = None) -> tuple[list[str], bool]:
    """Poll one follow-up. Returns (things worth saying, all-done)."""
    from . import tasks
    now = time.time() if now is None else now
    notes: list[str] = []
    blockers: list[str] = []
    done = True
    moved = False
    for url in row["urls"]:
        pr = await tasks._pr_state(url)
        if not pr:
            done = False
            continue                      # unreadable is not "merged"
        state = _state_of(pr)
        if row["seen"].get(url) and row["seen"][url] != state:
            moved = True
        row["seen"][url] = state
        why = _blocker(pr)
        if why:
            done = False
            blockers.append(f"• {pr.get('title') or url}\n  {why} — {url}")
    if moved:
        row["last_moved_at"] = now
    if done:
        row["status"] = "done"
        return [f"✅ {row['goal']} — all {len(row['urls'])} merged."], True

    due = row.get("due_at") or 0
    if due and not row.get("warned") and now >= due - WARN_BEFORE:
        row["warned"] = True
        left = max(0, int((due - now) / 3600))
        notes.append(f"⏰ {row['goal']} — {left}h to go and {len(blockers)} not "
                     f"merged:\n" + "\n".join(blockers))
    return notes, False


def _draft_pending(who: str) -> bool:
    """A nudge to this person is already waiting on Arun's yes."""
    return any(t.get("kind") == "teams_draft"
               and t.get("status") in ("awaiting_approval", "sending", "send_uncertain")
               and (t.get("teams_chat") or "") == who
               for t in store.list_tasks(limit=60))


def task_authorized(row: dict) -> bool:
    scope = row.get("authorization") or {}
    return bool(scope.get("words") and scope.get("person") == row.get("person")
                and scope.get("urls") == sorted(row.get("urls") or []))


def authorized(row: dict) -> bool:
    from . import authority, policy, steward
    who = row.get("person") or ""
    if row.get("status") != "open" or steward.sends_held() or not policy.check("send", who, asked=True).ok:
        return False
    if authority.may("send", who):
        return True
    return task_authorized(row)


def _reserve_authorized_nudge(row: dict, now: float) -> bool:
    """Durable daily/contact and total/task limits, claimed before any send.

    A crash after the claim consumes the attempt: uncertain delivery is never
    automatically repeated. The receipt is kept separately from graph state.
    """
    import hashlib
    stamp = time.localtime(now)
    if stamp.tm_wday >= 5 or not 9 <= stamp.tm_hour < 18:
        return False
    who = hashlib.sha256(row["person"].strip().casefold().encode()).hexdigest()
    day = time.strftime("%Y-%m-%d", stamp)
    key, count_key = f"followup_send:{who}:{day}", f"followup_send_count:{row['id']}"
    with store._connect() as conn:
        conn.execute("INSERT OR IGNORE INTO kv(key,value) VALUES (?, '0')", (count_key,))
        count = int(conn.execute("SELECT value FROM kv WHERE key=?", (count_key,)).fetchone()["value"])
        if count >= 3:
            return False
        claimed = conn.execute("INSERT OR IGNORE INTO kv(key,value) VALUES (?,?)",
                               (key, json.dumps({"followup": row["id"], "at": now}))).rowcount
        if not claimed:
            return False
        conn.execute("UPDATE kv SET value=? WHERE key=?", (str(count + 1), count_key))
    return True


async def _nudge(row: dict, blockers: int, now: float) -> str:
    """Execute an authorized chase once; otherwise preserve the draft path."""
    from . import tasks
    who = row.get("person") or ""
    current = next((r for r in _load() if r["id"] == row["id"]), None)
    if current is None or current.get("status") != "open":
        return ""
    if current.get("person") != who or current.get("urls") != row.get("urls"):
        return ""  # a stale graph cannot send to a target the user has changed
    if not who or now - row.get("last_nudged_at", 0) < NUDGE_EVERY:
        return ""
    if now - row.get("last_moved_at", now) < STALL_SECONDS:
        return ""
    # A draft he has not sent is not a reason to write him another one. The time
    # window alone produced four identical "any chance you can take a look at
    # these today?" drafts for Alex over three days — none sent, none
    # rejected, each one asking Arun the same question he had already not
    # answered. Chasing the chaser is not follow-through, it is nagging with
    # extra steps.
    if _draft_pending(who):
        return ""
    automatic = authorized(current)
    if automatic and not _reserve_authorized_nudge(current, now):
        if int(store.kv_get(f"followup_send_count:{row['id']}") or 0) >= 3:
            with store._connect() as conn:
                first = conn.execute("INSERT OR IGNORE INTO kv(key,value) VALUES (?, '1')",
                                     (f"followup_limit_reported:{row['id']}",)).rowcount
            if first:
                return (f"Follow-up #{row['id']}: three reminders attempted; still watching "
                        "for resolution, with no further automatic reminders.")
        return ""
    if automatic and not task_authorized(current):
        from . import authority
        grant = authority.may("send", who)
        if grant is None or not authority.claim_use(grant):
            return ""
    row["last_nudged_at"] = now
    body = ("Hi — any chance you can take a look at these today? "
            + " ".join(row["urls"]))
    t = store.create_task(f"Nudge {who} — {row['goal'][:60]}", "teams_draft",
                          body, None, teams_chat=who)
    store.update_task(t["id"], status="awaiting_approval", result=body)
    if automatic:
        result = await tasks.approve(t["id"])
        store.record_outcome("followup", "authorized nudge", subject=str(row["id"]), detail=result)
        return f"Follow-up #{row['id']}: {result}"
    return (f"✍️ #{t['id']} — {blockers} still stuck and nothing has moved. "
            f"Draft ready for {who}:\n\n{body}\n\n"
            f"👍 *approve task {t['id']}* to send · 👎 *reject task {t['id']}*")


async def check_all(now: float | None = None) -> list[str]:
    now = time.time() if now is None else now
    rows = _load()
    out: list[str] = []
    for row in rows:
        if row.get("status") != "open":
            continue
        # On the graph, a promise is a checkpointed thread: the warning before
        # the deadline can PARK and wait for what he says back, across restarts,
        # without the chase carrying on underneath it. A daemon loop cannot wait.
        from .graph import engine
        if engine.enabled():
            from .graph import bindings
            try:
                state = await bindings.tick_promise(row)
            except Exception:
                continue
            outcome = state.get("outcome") or ""
            if outcome == "kept":
                row["status"] = "done"
                out.append(f"✅ {row['goal']} — all {len(row['urls'])} merged.")
            elif outcome in ("his_call", "dropped"):
                # He said "leave it" (or the promise was dropped). The thread
                # ends there — and a row left OPEN starts a fresh thread on the
                # next tick, which warns him again about the thing he just told
                # it to drop, every fifteen minutes. Found live on 17 Sep.
                row["status"] = "stopped"
                row["stopped_why"] = ("he said to leave it" if outcome == "his_call"
                                      else "dropped")
            continue
        try:
            notes, done = await check_one(row, now)
        except Exception:
            continue                      # a transient gh failure is not news
        out += notes
        if not done:
            stuck = sum(1 for u in row["urls"]
                        if not (row["seen"].get(u) or "").startswith("MERGED"))
            with_nudge = await _nudge(row, stuck, now)
            if with_nudge:
                out.append(with_nudge)
    _save(rows)
    return out


async def loop() -> None:
    """Supervised by daemon.start, so the promise cannot quietly stop being kept."""
    from . import notify, wake
    while True:
        await wake.sleep(POLL_SECONDS)
        for note in await check_all():
            # Every one of these is either a deadline he has to act on or a
            # draft waiting on him. None of it is ambient.
            await notify.notify(note, "task", urgency="direct")
