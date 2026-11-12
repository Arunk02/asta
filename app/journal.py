"""Asta's working journal: what he asked, what it did, and how each thing ended.

He asked for two weeks of real data to improve Asta from — every request of his,
every task and piece of coding, every reply to a colleague — rather than spot
checks after something has already gone wrong. Most of it is already written
somewhere (ui_messages, tasks, task_events, outcomes, traces, call_usage, worker
usage), but nothing joins it per day, and the turn timings are pruned to the
last 500. So once an hour the day is snapshotted into one file, kept for
`ASTA_JOURNAL_DAYS` (21) days, and `review()` reads those files and says where
it went wrong.

Rules this module keeps:

  READ, NEVER WRITE BEHAVIOUR. Like the scorecard, it reads tables other code
  fills; it changes nothing about how Asta acts.

  LOCAL ONLY. Files live under data/journal/, which is gitignored. The review is
  for him and for improving Asta; it is never pushed, posted or sent.
"""
from __future__ import annotations

import asyncio
import datetime as dt
import gzip
import json
import os
import re
import time
from collections import Counter
from pathlib import Path

from . import store

JOURNAL_DIR = store.ROOT / "data" / "journal"
#: What he asked, read on every journal pass so a change needs no restart.
KEEP_DAYS_DEFAULT = 21
#: Decisions worth keeping. The chat-watch reads and knowledge lookups are noise.
DECISION_KINDS = ("answer", "thread", "authority", "responder", "turn", "offer", "steward",
                  "task", "ask", "route", "session", "notify", "frontdesk", "checkout")
#: How a relayed draft verdict reads in the conversation: his edit of a draft.
_NOT_APPROVED = re.compile(r"arun did not approve sending the draft as-is\.\s*his feedback:\s*(.*?)"
                           r"(?:\s*revise accordingly\b|$)", re.I | re.S)


def keep_days() -> int:
    try:
        return max(1, int(os.environ.get("ASTA_JOURNAL_DAYS", KEEP_DAYS_DEFAULT)))
    except ValueError:
        return KEEP_DAYS_DEFAULT


def _bounds(day: dt.date) -> tuple[float, float]:
    start = dt.datetime.combine(day, dt.time.min).timestamp()
    return start, start + 86400


def _rows(sql: str, args: tuple) -> list[dict]:
    with store._connect() as conn:
        return [dict(r) for r in conn.execute(sql, args).fetchall()]


def collect(day: dt.date) -> dict:
    """Everything that day, as plain data."""
    s, e = _bounds(day)
    tasks = _rows("SELECT id, title, kind, status, workspace, teams_chat, created_at, started_at, "
                  "finished_at, pr_urls, pr_state, error, substr(result, -1500) AS result_tail "
                  "FROM tasks WHERE (created_at >= ? AND created_at < ?) "
                  "OR (finished_at >= ? AND finished_at < ?)", (s, e, s, e))
    for t in tasks:
        for name in ("executor", "tier", "rounds", "pipeline", "branch"):
            t[name] = store.kv_get(f"task_{name}:{t['id']}") or ""
    worker = []
    for raw in store.kv_like("worker_usage:%").values():
        try:
            row = json.loads(raw)
        except ValueError:
            continue
        if isinstance(row, dict) and s <= float(row.get("started_at") or 0) < e:
            worker.append(row)
    return {
        "day": day.isoformat(), "recorded_at": time.time(), "version": 1,
        "his_messages": _rows("SELECT id, conv_id, content, meta, created_at FROM ui_messages "
                              "WHERE role='user' AND created_at >= ? AND created_at < ?", (s, e)),
        "asta_replies": _rows("SELECT id, conv_id, substr(content, 1, 2500) AS content, meta, created_at "
                              "FROM ui_messages WHERE role='assistant' AND created_at >= ? "
                              "AND created_at < ?", (s, e)),
        "tasks": tasks,
        "task_events": _rows("SELECT task_id, kind, detail, created_at FROM task_events "
                             "WHERE created_at >= ? AND created_at < ?", (s, e)),
        "decisions": _rows("SELECT kind, outcome, subject, substr(detail, 1, 600) AS detail, created_at "
                           "FROM outcomes WHERE created_at >= ? AND created_at < ? AND kind IN (%s)"
                           % ",".join("?" * len(DECISION_KINDS)), (s, e, *DECISION_KINDS)),
        "traces": _rows("SELECT * FROM traces WHERE created_at >= ? AND created_at < ?", (s, e)),
        "call_usage": _rows("SELECT * FROM call_usage WHERE started_at >= ? AND started_at < ?", (s, e)),
        "worker_usage": worker,
        "questions": _rows("SELECT id, text, source, answer, status, created_at, answered_at "
                           "FROM questions WHERE created_at >= ? AND created_at < ?", (s, e)),
        # What went out under his name: his own lines and Asta's (marked automatic).
        "sent_as_him": _rows("SELECT chat, sender, substr(text, 1, 1500) AS text, sent_at, key "
                             "FROM teams_messages WHERE sent_at >= ? AND sent_at < ? "
                             "AND sender LIKE 'Arunkumar%'", (s, e)),
        "chat_volume": _rows("SELECT chat, COUNT(*) AS n FROM teams_messages WHERE sent_at >= ? "
                             "AND sent_at < ? GROUP BY chat", (s, e)),
    }


def path_for(day: dt.date) -> Path:
    return JOURNAL_DIR / f"{day.isoformat()}.json.gz"


def record(day: dt.date | None = None) -> Path:
    """Write (or rewrite) one day's file, then drop files past the keep window."""
    day = day or dt.date.today()
    JOURNAL_DIR.mkdir(parents=True, exist_ok=True)
    target = path_for(day)
    tmp = target.with_suffix(".tmp")
    with gzip.open(tmp, "wt", encoding="utf-8") as f:
        json.dump(collect(day), f, default=str)
    tmp.replace(target)
    prune()
    return target


def prune(today: dt.date | None = None) -> int:
    cutoff = (today or dt.date.today()) - dt.timedelta(days=keep_days())
    gone = 0
    for p in JOURNAL_DIR.glob("*.json.gz"):
        try:
            day = dt.date.fromisoformat(p.name.split(".")[0])
        except ValueError:
            continue
        if day < cutoff:
            p.unlink(missing_ok=True)
            gone += 1
    return gone


def load(days: int = 14, today: dt.date | None = None) -> list[dict]:
    today = today or dt.date.today()
    out = []
    for i in range(days - 1, -1, -1):
        p = path_for(today - dt.timedelta(days=i))
        if p.exists():
            with gzip.open(p, "rt", encoding="utf-8") as f:
                out.append(json.load(f))
    return out


def _pct(values: list[float], q: float):
    if not values:
        return None
    s = sorted(values)
    return s[min(len(s) - 1, int(round(q * (len(s) - 1))))]


def review(days: int = 14, today: dt.date | None = None) -> str:
    """Where Asta went wrong over the window, in the order he would fix it."""
    from . import scorecard
    logs = load(days, today)
    if not logs:
        return f"No journal yet for the last {days} days — it starts recording on the next pass."
    his = [m for d in logs for m in d["his_messages"]]
    replies = [m for d in logs for m in d["asta_replies"]]
    tasks = {t["id"]: t for d in logs for t in d["tasks"]}
    decisions = [x for d in logs for x in d["decisions"]]
    traces = [t for d in logs for t in d["traces"]]
    first, last = logs[0]["day"], logs[-1]["day"]
    lines = [f"# Asta journal review, {first} → {last} ({len(logs)} days recorded)", ""]

    # 1. His corrections — what he had to say twice, with what he was answering.
    by_time = sorted([("him", m) for m in his] + [("asta", m) for m in replies],
                     key=lambda x: x[1]["created_at"])
    corrections, edits = [], []
    prev_asta = ""
    for who, m in by_time:
        if who == "asta":
            prev_asta = m["content"]
            continue
        text = m["content"] or ""
        edited = _NOT_APPROVED.search(text)
        if edited:
            edits.append((m["created_at"], edited.group(1).strip()[:300]))
            continue
        words = scorecard.his_words(text) or text
        if scorecard._SYNTHETIC.match(text):
            continue
        if scorecard._CORRECTION.search(words):
            corrections.append((m["created_at"], words.strip()[:250], prev_asta.strip()[:200]))
    lines.append(f"## His corrections: {len(corrections)}")
    for at, said, before in corrections[-15:]:
        lines.append(f"- {_when(at)} — he said “{said}”" + (f"\n  after Asta: “{before}”" if before else ""))
    lines += ["", f"## Drafts he did not approve as written: {len(edits)}"]
    for at, said in edits[-15:]:
        lines.append(f"- {_when(at)} — his change: “{said}”")

    # 2. Things that went wrong without him having to say so.
    kinds = Counter(f"{x['kind']}/{x['outcome']}" for x in decisions)
    bad = {"turn/false_send_claim": "claimed a send that did not happen",
           "turn/false_schedule_claim": "claimed a schedule that does not exist",
           "turn/false_denial_of_send": "denied a send that did happen",
           "answer/he answered": "answer arrived after he had answered himself (stale)",
           "answer/retired": "drafts retired unsent",
           "answer/auto reply held": "automatic replies held back",
           "thread/held back": "lines held back",
           "turn/stopped_ceiling": "turns cut at the time ceiling",
           "turn/stopped_idle": "turns stopped idle",
           "ask/orphaned": "questions whose turn died before he answered",
           "ask/already_named": "questions refused because he had named the answer",
           "ask/timeout": "questions to him that timed out",
           "turn/empty_local": "local-brain answers that said nothing",
           "turn/stall_handoff": "brains that stalled and were replaced mid-turn",
           "answer/duplicate": "answers that duplicated one already shown",
           "answer/task draft dropped": "task drafts dropped"}
    lines += ["", "## Things that went wrong on their own"]
    for key, label in bad.items():
        if kinds.get(key):
            lines.append(f"- {kinds[key]} × {label} (`{key}`)")

    # 3. Tasks and coding.
    st = Counter(t["status"] for t in tasks.values())
    kinds_t = Counter(t["kind"] for t in tasks.values())
    lines += ["", f"## Tasks: {len(tasks)} ({', '.join(f'{k} {n}' for k, n in kinds_t.most_common())})",
              "- by outcome: " + ", ".join(f"{k} {n}" for k, n in st.most_common())]
    failed = [t for t in tasks.values() if t["status"] in ("failed", "timeout", "rejected", "cancelled")]
    for t in failed[-10:]:
        lines.append(f"  - #{t['id']} {t['status']}: {t['title'][:90]}"
                     + (f" — {t['error'][:120]}" if t.get("error") else ""))
    done = [float(t["finished_at"]) - float(t["created_at"]) for t in tasks.values()
            if t.get("finished_at") and t.get("created_at") and t["status"] in ("done", "merged", "shipped")]
    if done:
        lines.append(f"- time to finish: median {_dur(_pct(done, 0.5))}, slow end {_dur(_pct(done, 0.9))}")

    # 4. Colleagues: who asked, and how it ended.
    people = Counter()
    for x in decisions:
        if x["kind"] == "answer" and x["outcome"] in ("presented", "queued", "followup", "he answered"):
            who = (x.get("detail") or "").split(":", 1)[0].strip()
            if who and who.lower() not in ("answer", "task", ""):
                people[(who, x["outcome"])] += 1
    names = sorted({w for w, _ in people})
    if names:
        lines += ["", "## Colleagues"]
        for w in names:
            parts = [f"{o} {people[(w, o)]}" for o in ("presented", "followup", "queued", "he answered")
                     if people[(w, o)]]
            lines.append(f"- {w}: " + ", ".join(parts))
    sent = [s for d in logs for s in d["sent_as_him"]]
    auto = kinds.get("authority/auto reply", 0) + kinds.get("authority/used", 0)
    lines.append(f"- lines in Teams under his name: {len(sent)}; under a standing permission: {auto}")
    try:
        from . import review_outcomes
        posted = review_outcomes.score_line(min(days, 7))
    except Exception:                                          # noqa: BLE001
        posted = ""
    if posted:
        lines.append(f"- {posted}")

    # 5. Speed and cost.
    total = [float(t["total_ms"]) / 1000 for t in traces if t.get("total_ms")]
    if total:
        lines += ["", "## Speed",
                  f"- chat turns: {len(total)}; median {_pct(total, 0.5):.0f} s, slow end {_pct(total, 0.9):.0f} s"]
    return "\n".join(lines)


def _when(ts) -> str:
    return time.strftime("%d %b %H:%M", time.localtime(float(ts)))


def _dur(v) -> str:
    if v is None:
        return "—"
    return f"{v / 60:.0f} min" if v < 3600 else (f"{v / 3600:.1f} h" if v < 172800 else f"{v / 86400:.1f} d")


#: How often today's file is rewritten, so a restart loses at most this much.
RECORD_EVERY = 3600


async def loop() -> None:
    """Record yesterday (if missed) and today, then today again every hour.

    Supervised by daemon. Yesterday is rewritten once just after midnight, so a
    day's file holds the whole day."""
    await asyncio.sleep(180)
    # The tables it reads go back months, so the first start fills the whole
    # window at once and a restart fills any day it missed.
    for back in range(keep_days() - 1, 0, -1):
        day = dt.date.today() - dt.timedelta(days=back)
        if not path_for(day).exists():
            await asyncio.to_thread(record, day)
    last_day = dt.date.today()
    while True:
        await asyncio.to_thread(record, dt.date.today())
        await asyncio.sleep(RECORD_EVERY)
        if dt.date.today() != last_day:
            await asyncio.to_thread(record, last_day)    # close yesterday completely
            last_day = dt.date.today()


def main(argv: list[str] | None = None) -> int:
    import argparse
    ap = argparse.ArgumentParser(prog="python -m app.journal")
    ap.add_argument("what", choices=("record", "review"))
    ap.add_argument("--days", type=int, default=14)
    args = ap.parse_args(argv)
    if args.what == "record":
        print(record())
    else:
        print(review(args.days))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
