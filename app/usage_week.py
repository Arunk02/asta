"""One table a week: where the tokens went, and what came of it.

Replaces the per-task "token audit: 49% avoidable" line (17 Nov): a number on
every result that mostly counted the plan he asked for. This says what he can
act on — which kind of work spends, which runs were costly, and how many
prepared answers were never used.

Spend is "new input" (fresh input + cache writes) — what moves the usage limit
most. Cached input and output are shown beside it. Zero model tokens.
"""
from __future__ import annotations

import json
import statistics
import time
from collections import defaultdict

from . import store

WEEK = 7 * 86400


def _k(n: int) -> str:
    return f"{n / 1e6:.1f}M" if n >= 1e6 else f"{n / 1e3:.0f}k"


def _worker_rows(since: float) -> list[dict]:
    out = []
    for raw in store.kv_like("worker_usage:%").values():
        try:
            row = json.loads(raw)
        except ValueError:
            continue
        if float(row.get("started_at") or 0) >= since:
            out.append(row)
    return out


def task_spend(since: float) -> dict[int, dict]:
    """task id -> {new, cached, out, legs, kind, title} for real (non-rehearsal) tasks."""
    from . import rehearsal
    per: dict[int, dict] = {}
    for row in _worker_rows(since):
        tid = int(row.get("task_id") or 0)
        if not tid or rehearsal.is_rehearsal(tid):
            continue
        t = per.setdefault(tid, {"new": 0, "cached": 0, "out": 0, "legs": 0})
        t["legs"] += 1
        u = row.get("usage") or {}
        t["new"] += int(u.get("new_tokens") or 0)
        t["cached"] += int(u.get("cache_read") or 0)
        t["out"] += int(u.get("out_tokens") or 0)
    for tid, t in per.items():
        task = store.get_task(tid) or {}
        t["kind"] = task.get("kind") or "?"
        t["title"] = (task.get("title") or "")[:48]
    return per


def _chat(since: float) -> dict:
    with store._connect() as c:
        row = c.execute("SELECT COUNT(DISTINCT conv_id), SUM(input_tokens + cache_write_tokens), "
                        "SUM(cache_read_tokens), SUM(output_tokens) FROM usage WHERE created_at >= ?",
                        (since,)).fetchone()
        starts = [r[0] for r in c.execute(
            "SELECT context_tokens FROM traces t WHERE created_at >= ? AND context_tokens > 0 "
            "AND id = (SELECT MIN(id) FROM traces WHERE conv_id = t.conv_id)", (since,))]
    return {"count": row[0] or 0, "new": row[1] or 0, "cached": row[2] or 0, "out": row[3] or 0,
            "start": int(statistics.median(starts)) if starts else 0}


def _background(since: float) -> list[tuple[str, int, int]]:
    with store._connect() as c:
        return [(p, n, int(t or 0)) for p, n, t in c.execute(
            "SELECT purpose, COUNT(*), SUM(new_tokens) FROM call_usage WHERE started_at >= ? "
            "GROUP BY purpose ORDER BY 3 DESC", (since,))]


def _answers(since: float) -> dict[str, int]:
    with store._connect() as c:
        return dict(c.execute("SELECT outcome, COUNT(*) FROM outcomes WHERE kind='answer' "
                              "AND created_at >= ? GROUP BY outcome", (since,)).fetchall())


def table(now: float | None = None) -> str:
    now = time.time() if now is None else now
    since = now - WEEK
    tasks = task_spend(since)
    kinds: dict[str, dict] = defaultdict(lambda: {"n": 0, "new": 0, "cached": 0, "out": 0})
    for t in tasks.values():
        k = kinds[t["kind"]]
        k["n"] += 1
        for f in ("new", "cached", "out"):
            k[f] += t[f]
    chat = _chat(since)
    bg = _background(since)
    lines = [f"{'what':<14}{'runs':>6}{'new in':>9}{'cached':>9}{'out':>8}"]
    for kind, k in sorted(kinds.items(), key=lambda kv: -kv[1]["new"]):
        lines.append(f"{kind:<14}{k['n']:>6}{_k(k['new']):>9}{_k(k['cached']):>9}{_k(k['out']):>8}")
    lines.append(f"{'chat':<14}{chat['count']:>6}{_k(chat['new']):>9}{_k(chat['cached']):>9}"
                 f"{_k(chat['out']):>8}")
    if bg:
        lines.append(f"{'background':<14}{sum(n for _, n, _ in bg):>6}"
                     f"{_k(sum(t for *_, t in bg)):>9}")
    a = _answers(since)
    day = time.strftime("%d %b", time.localtime(since)) + "–" + time.strftime("%d %b", time.localtime(now))
    out = [f"📊 Asta's week ({day})", "```", *lines, "```"]
    top = sorted(tasks.items(), key=lambda kv: -kv[1]["new"])[:3]
    if top:
        out.append("Costliest: " + "; ".join(
            f"#{tid} {t['kind']} {_k(t['new'])} in {t['legs']} leg(s)" for tid, t in top))
    if bg:
        out.append("Background: " + ", ".join(f"{p} {_k(t)} ({n})" for p, n, t in bg[:3]))
    out.append(f"Answers: {a.get('presented', 0)} shown · {a.get('he answered', 0)} you answered "
               f"first · {a.get('parked', 0)} parked · {a.get('retired', 0)} retired unsent")
    if chat["start"]:
        out.append(f"Chat starts at {_k(chat['start'])} context (median).")
    return "\n".join(out)
