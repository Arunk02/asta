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
        t = per.setdefault(tid, {"new": 0, "cached": 0, "out": 0, "legs": 0,
                                 "usd": 0.0, "priced": 0})
        t["legs"] += 1
        if isinstance(row.get("cost_usd"), (int, float)):
            t["usd"] += float(row["cost_usd"])
            t["priced"] += 1
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
        out_rows = c.execute("SELECT SUM(output_tokens) FROM usage WHERE created_at >= ?",
                             (max(since, OUTPUT_MEASURED_FROM),)).fetchone()
        costs = c.execute("SELECT conv_id, cost_usd FROM traces WHERE created_at >= ? "
                          "ORDER BY id", (since,)).fetchall()
    return {"count": row[0] or 0, "new": row[1] or 0, "cached": row[2] or 0,
            "out": out_rows[0] or 0, "out_partial": since < OUTPUT_MEASURED_FROM,
            "usd": turn_costs(costs), "turns": len(costs),
            "start": int(statistics.median(starts)) if starts else 0}


#: Chat output tokens are measured from here (asta PR: settled message copy);
#: before it ~16 a turn were recorded whatever the answer's length.
OUTPUT_MEASURED_FROM = 1794940200.0          # 2026-11-18 00:00 IST


def turn_costs(rows) -> float:
    """Total chat $ from (conv_id, cost_usd) rows in order. The CLI reports a
    SESSION's running total on each turn, so a turn costs the difference from
    the conversation's previous row; a drop means a new session began."""
    last: dict[str, float] = {}
    total = 0.0
    for conv, usd in rows:
        usd = float(usd or 0)
        prev = last.get(conv)
        total += usd - prev if prev is not None and usd >= prev else usd
        last[conv] = usd
    return total


def _background_cost(since: float) -> tuple[float, int, int]:
    """($, runs with a figure, runs) for background calls."""
    with store._connect() as c:
        usd, priced, n = c.execute(
            "SELECT SUM(cost_usd), COUNT(cost_usd), COUNT(*) FROM call_usage WHERE started_at >= ?",
            (since,)).fetchone()
    return float(usd or 0), int(priced or 0), int(n or 0)


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
    kinds: dict[str, dict] = defaultdict(lambda: {"n": 0, "new": 0, "cached": 0, "out": 0,
                                                  "usd": 0.0, "priced": 0, "legs": 0})
    for t in tasks.values():
        k = kinds[t["kind"]]
        k["n"] += 1
        for f in ("new", "cached", "out", "usd", "priced", "legs"):
            k[f] += t[f]
    chat = _chat(since)
    bg = _background(since)
    bg_usd, bg_priced, bg_n = _background_cost(since)
    partial = False

    def usd(amount: float, priced: int, runs: int) -> str:
        nonlocal partial
        if not priced:
            partial = partial or runs > 0
            return "—"
        partial = partial or priced < runs
        return f"${amount:.2f}" + ("+" if priced < runs else "")

    lines = [f"{'what':<12}{'runs':>5}{'new in':>8}{'cached':>8}{'out':>7}{'$':>8}"]
    for kind, k in sorted(kinds.items(), key=lambda kv: -kv[1]["new"]):
        label = {"code": "coding", "analysis": "debugging"}.get(kind, kind)
        lines.append(f"{label:<12}{k['n']:>5}{_k(k['new']):>8}{_k(k['cached']):>8}{_k(k['out']):>7}"
                     f"{usd(k['usd'], k['priced'], k['legs']):>8}")
    lines.append(f"{'chat':<12}{chat['count']:>5}{_k(chat['new']):>8}{_k(chat['cached']):>8}"
                 f"{_k(chat['out']) + ('*' if chat['out_partial'] else ''):>7}"
                 f"{usd(chat['usd'], chat['turns'], chat['turns']):>8}")
    if bg:
        lines.append(f"{'background':<12}{sum(n for _, n, _ in bg):>5}"
                     f"{_k(sum(t for *_, t in bg)):>8}{'':>8}{'':>7}"
                     f"{usd(bg_usd, bg_priced, bg_n):>8}")
    total = sum(k["usd"] for k in kinds.values()) + chat["usd"] + bg_usd
    a = _answers(since)
    day = time.strftime("%d %b", time.localtime(since)) + "–" + time.strftime("%d %b", time.localtime(now))
    out = [f"📊 Asta's week ({day})", "```", *lines, "```",
           f"All of it: ${total:.2f} at API prices — Claude Code's own figure per run; "
           "your plan is a subscription."
           + (" — / + : runs before 18 Nov carry no figure." if partial else "")]
    top = sorted(tasks.items(), key=lambda kv: -kv[1]["new"])[:3]
    if top:
        out.append("Costliest: " + "; ".join(
            f"#{tid} {t['kind']} {_k(t['new'])}"
            + (f" (${t['usd']:.2f})" if t["priced"] else "") + f" in {t['legs']} leg(s)"
            for tid, t in top))
    if bg:
        out.append("Background: " + ", ".join(f"{p} {_k(t)} ({n})" for p, n, t in bg[:3]))
    out.append(f"Answers: {a.get('presented', 0)} shown · {a.get('he answered', 0)} you answered "
               f"first · {a.get('parked', 0)} parked · {a.get('retired', 0)} retired unsent")
    if chat["turns"]:
        out.append(f"Chat: {chat['turns']} turns, ${chat['usd']:.2f} at API prices "
                   f"(${chat['usd'] / chat['turns']:.3f} a turn; your plan is a subscription)."
                   + (" *output measured from 18 Nov." if chat["out_partial"] else ""))
    if chat["start"]:
        out.append(f"Chat starts at {_k(chat['start'])} context (median).")
    return "\n".join(out)
