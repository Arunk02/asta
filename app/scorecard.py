"""The daily scorecard — is Asta getting better, in numbers he can read on a phone.

Every complaint this summer was found by Arun, in production, and fixed one at a
time: a 60-second reply, a plan announced as done, 150 pushes in a day, a rule he
gave that did not stick. Each fix was real and none of them could be SEEN to have
helped, because nothing measured the thing he felt. This measures it.

Rules this module keeps:

  READ, NEVER WRITE BEHAVIOUR. It reads tables other code already fills
  (traces, notifications, tasks, outcomes, ui_messages, kv). It changes nothing
  about how Asta acts, so it can be wrong without making Asta worse.

  EVERY ROW HAS A TARGET. A number without a target is trivia. The targets are
  the plan's scorecard table (Astra-class Asta, 11 Sep 2026), and each row says
  good / warn / bad against it — or "n/a" when there is no data yet, which is
  never shown as good.

  SNAPSHOTS, NOT JUST NOW. A daily snapshot (kv `scorecard:YYYY-MM-DD`) is what
  makes "better" a line rather than a feeling; the traces table keeps only its
  last 500 rows, so without the snapshot the history evaporates.

  QUIET. It pushes nothing. The page is where it lives.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import json
import re
import time
from dataclasses import asdict, dataclass
from pathlib import Path

from . import store

WINDOW_DAYS = 14
PHONE_CHANNELS = ("whatsapp", "telegram")
SNAPSHOT_PREFIX = "scorecard:"
#: Where the test bench leaves its last run. A module constant so a test can
#: point it somewhere else instead of racing the real file.
WORKWORLD_LAST = Path(__file__).resolve().parent.parent / "data" / "workworld" / "last.json"

#: What "he had to correct me" looks like in his own messages. Deliberately his
#: vocabulary from this summer, not a sentiment model: a phrase list is cheap,
#: explainable and wrong in ways that are easy to see.
_CORRECTION = re.compile(
    r"\b(why (?:is|are|did|does|still|this|these|it|u|you|stopped|not|again)|still same|"
    r"see (?:still|here|how)|not (?:that|this) (?:one|task)|i (?:told|asked|said)|"
    r"how many times|what is blocking|i (?:don'?t|dont) see|can'?t able|cant able|"
    r"or not yet|or not\s*\.*\?|doesn'?t make sense|stupid|dumb|worst|wrong|"
    r"not following|confus)\w*", re.I)

#: Rows in ui_messages with role "user" that Asta wrote itself — the loop's
#: auto-continue, a relayed draft verdict, an injected context block. Counting
#: them as his messages made "how often did he have to correct Asta" a count of
#: Asta talking to itself. "His feedback: …" is kept, because that part IS his.
_SYNTHETIC = re.compile(
    r"^\s*(?:continue now —|resume:|context:|\[now:|\[prior investigation|"
    r"arun did not approve sending the draft as-is\.)", re.I)
_HIS_FEEDBACK = re.compile(r"his feedback:\s*(.*?)(?:\s*revise accordingly\b.*)?$", re.I | re.S)


def his_words(text: str) -> str:
    """The part of a stored 'user' message Arun actually typed, or ''."""
    t = (text or "").strip()
    m = _HIS_FEEDBACK.search(t)
    if m and t.lower().startswith("arun did not approve"):
        return m.group(1).strip()
    return "" if _SYNTHETIC.match(t) else t


@dataclass
class Row:
    key: str
    label: str
    value: float | int | str | None
    shown: str
    target: str
    state: str          # good | warn | bad | na
    note: str = ""


# --- tiny helpers -------------------------------------------------------------

def _pct(values: list[float], q: float) -> float | None:
    if not values:
        return None
    s = sorted(values)
    i = min(len(s) - 1, max(0, int(round(q * (len(s) - 1)))))
    return s[i]


def _rows(sql: str, args: tuple = ()) -> list[dict]:
    with store._connect() as conn:
        return [dict(r) for r in conn.execute(sql, args).fetchall()]


def _one(sql: str, args: tuple = ()):
    with store._connect() as conn:
        r = conn.execute(sql, args).fetchone()
    return r[0] if r else None


def _judge(value, good, warn, lower_is_better: bool = True) -> str:
    """good/warn/bad against two thresholds; na when there is nothing to judge."""
    if value is None:
        return "na"
    if lower_is_better:
        return "good" if value <= good else "warn" if value <= warn else "bad"
    return "good" if value >= good else "warn" if value >= warn else "bad"


# --- the measurements ---------------------------------------------------------

def replies(since: float, until: float) -> dict:
    rows = _rows("SELECT channel, total_ms, first_token_ms, input_tokens, cached_tokens, "
                 "context_tokens FROM traces WHERE created_at >= ? AND created_at < ?",
                 (since, until))
    phone = [r for r in rows if r["channel"] in PHONE_CHANNELS]
    totals = [r["total_ms"] / 1000 for r in phone if r["total_ms"]]
    firsts = [r["first_token_ms"] / 1000 for r in phone if r["first_token_ms"]]
    per_turn = [(r["input_tokens"] or 0) + (r["cached_tokens"] or 0) for r in rows]
    ctx = [r["context_tokens"] for r in rows if r["context_tokens"]]
    return {
        "turns": len(rows),
        "phone_turns": len(phone),
        "p50_s": _pct(totals, 0.5),
        "p90_s": _pct(totals, 0.9),
        "first_word_p50_s": _pct(firsts, 0.5),
        "tokens_per_turn_avg": (sum(per_turn) / len(per_turn)) if per_turn else None,
        "context_per_call_max": max(ctx) if ctx else None,
        "context_per_call_avg": (sum(ctx) / len(ctx)) if ctx else None,
    }


def reply_length(since: float, until: float) -> dict:
    rows = _rows("SELECT content, meta FROM ui_messages WHERE role='assistant' "
                 "AND created_at >= ? AND created_at < ?", (since, until))
    lengths = []
    for r in rows:
        try:
            ch = (json.loads(r["meta"] or "{}") or {}).get("channel", "")
        except ValueError:
            ch = ""
        if ch in PHONE_CHANNELS:
            lengths.append(len(r["content"] or ""))
    return {"replies": len(lengths),
            "avg_chars": (sum(lengths) / len(lengths)) if lengths else None,
            "over_600": sum(1 for n in lengths if n > 600)}


def corrections(since: float, until: float) -> dict:
    rows = _rows("SELECT content FROM ui_messages WHERE role='user' "
                 "AND created_at >= ? AND created_at < ?", (since, until))
    his = [w for w in (his_words(r["content"]) for r in rows) if w]
    hits = [w for w in his if _CORRECTION.search(w)]
    return {"messages": len(his), "corrections": len(hits)}


def pushes(since: float, until: float) -> dict:
    rows = _rows("SELECT text, level, created_at FROM notifications "
                 "WHERE created_at >= ? AND created_at < ?", (since, until))
    days = max(1.0, (until - since) / 86400)
    by_day: dict[str, int] = {}
    prefix: dict[str, int] = {}
    per_task: dict[str, int] = {}
    for r in rows:
        d = dt.datetime.fromtimestamp(r["created_at"]).date().isoformat()
        by_day[d] = by_day.get(d, 0) + 1
        k = (r["text"] or "")[:80]
        prefix[k] = prefix.get(k, 0) + 1
        if r["level"] == "task":
            m = re.search(r"#(\d{1,5})\b", r["text"] or "")
            if m:
                per_task[m.group(1)] = per_task.get(m.group(1), 0) + 1
    dup = sum(n - 1 for n in prefix.values() if n > 1)
    return {"total": len(rows), "per_day": len(rows) / days,
            "peak_day": max(by_day.values()) if by_day else 0,
            "duplicate_share": (dup / len(rows)) if rows else None,
            "most_for_one_task": max(per_task.values()) if per_task else 0}


def attention_ignored(since: float, until: float) -> dict:
    rows = _rows("SELECT outcome, COUNT(*) n FROM outcomes WHERE kind='attention' "
                 "AND created_at >= ? AND created_at < ? GROUP BY outcome", (since, until))
    counts = {r["outcome"]: r["n"] for r in rows}
    total = sum(counts.values())
    return {"tracked": total,
            "ignored_share": (counts.get("ignored", 0) / total) if total else None}


def task_health(since: float, until: float) -> dict:
    from . import agent as agent_mod, tasks
    rows = _rows("SELECT kind, status, result, error FROM tasks "
                 "WHERE created_at >= ? AND created_at < ?", (since, until))
    code = [r for r in rows if r["kind"] == "code"]
    failed = [r for r in rows if r["status"] in ("failed", "timeout")]
    limit_failures = [r for r in failed if agent_mod.transient_limit(r["error"] or "")]
    # A plan announced as done: finished code work whose own tail reads as a gate.
    false_done = [r for r in code if r["status"] == "done"
                  and tasks._is_gate((r["result"] or "")[-2500:])]
    return {
        "created": len(rows),
        "code": len(code),
        "code_merged": sum(1 for r in code if r["status"] == "merged"),
        "code_cancelled": sum(1 for r in code if r["status"] in ("cancelled", "rejected")),
        "failed": len(failed),
        "limit_failures": len(limit_failures),
        "paused": sum(1 for r in rows if r["status"] == "paused"),
        "false_done": len(false_done),
    }


def tool_failures(since: float, until: float) -> dict:
    rows = _rows("SELECT subject, COUNT(*) n FROM outcomes WHERE kind='capability' "
                 "AND outcome='failed' AND created_at >= ? AND created_at < ? "
                 "GROUP BY subject ORDER BY n DESC", (since, until))
    return {"total": sum(r["n"] for r in rows),
            "top": [f"{r['subject']} ×{r['n']}" for r in rows[:3]]}


def front_desk(since: float, until: float) -> dict:
    """How his messages were routed — and how many never needed a brain."""
    from .frontdesk import NO_BRAIN
    rows = _rows("SELECT outcome, COUNT(*) n FROM outcomes WHERE kind='frontdesk' "
                 "AND created_at >= ? AND created_at < ? GROUP BY outcome", (since, until))
    counts = {r["outcome"]: r["n"] for r in rows}
    total = sum(counts.values())
    free = sum(n for k, n in counts.items() if k in NO_BRAIN)
    return {"total": total, "no_brain_share": (free / total) if total else None,
            "counts": counts}


def task_tiers(since: float, until: float) -> dict:
    """Which tier task legs ran at (app/routing.py), and how often a tier escalated."""
    rows = _rows("SELECT outcome, COUNT(*) n FROM outcomes WHERE kind='route' "
                 "AND created_at >= ? AND created_at < ? GROUP BY outcome", (since, until))
    counts = {r["outcome"]: r["n"] for r in rows}
    max_on_small = _one("SELECT COUNT(*) FROM outcomes WHERE kind='route' AND outcome='T1' "
                        "AND (detail LIKE '%@ max%' OR detail LIKE '%@ xhigh%') "
                        "AND created_at >= ? AND created_at < ?", (since, until)) or 0
    return {"counts": counts, "max_on_small": int(max_on_small)}


def interruptions(since: float, until: float) -> dict:
    """Buzzes a day — what actually reached his phone, not what the bell recorded.

    The pushes row above counts notification ROWS, and the bell gets everything
    including what the digest absorbed. Counted at the delivery door instead
    (app/budget.py), so the digest shows up as fewer interruptions rather than
    as the same number with different words.
    """
    from . import budget, digest
    days, total = 0, 0
    day = dt.date.fromtimestamp(since)
    last = dt.date.fromtimestamp(until)
    while day <= last:
        days += 1
        try:
            total += int(store.kv_get("pushes:" + day.isoformat()) or 0)
        except ValueError:
            pass
        day += dt.timedelta(days=1)
    digests = _one("SELECT COUNT(*) FROM outcomes WHERE kind='digest' AND outcome='sent' "
                   "AND created_at >= ? AND created_at < ?", (since, until)) or 0
    digested = _one("SELECT COUNT(*) FROM outcomes WHERE kind='attention' AND outcome='digested' "
                    "AND created_at >= ? AND created_at < ?", (since, until)) or 0
    return {"per_day": (total / days) if days else None, "total": total,
            "budget": budget.cap(), "digests": int(digests), "digested": int(digested),
            "waiting": len(digest.pending())}


def done_alone(since: float, until: float) -> dict:
    """Acts Asta carried out under a standing permission, and how many exist."""
    from . import authority
    used = _one("SELECT COUNT(*) FROM outcomes WHERE kind='authority' AND outcome='used' "
                "AND created_at >= ? AND created_at < ?", (since, until)) or 0
    return {"used": int(used), "grants": len(authority.grants())}


def self_changes(since: float, until: float) -> dict:
    """What Asta changed about itself, and what became of it."""
    from . import settings
    rows = _rows("SELECT outcome, COUNT(*) n FROM outcomes WHERE kind='evolution' "
                 "AND created_at >= ? AND created_at < ? GROUP BY outcome", (since, until))
    counts = {r["outcome"]: r["n"] for r in rows}
    return {"promoted": counts.get("promoted", 0), "rejected": counts.get("rejected", 0),
            "rolled_back": counts.get("rolled_back", 0), "live": len(settings.overrides())}


def rules_holding() -> dict:
    """His standing rules, and whether his own corrections still hold in replay."""
    from . import policy
    try:
        card = json.loads((WORKWORLD_LAST.parent / "last-replays.json").read_text())
    except (OSError, ValueError):
        card = {}
    return {"rules": len(policy.rules()), "replayed": card.get("total"),
            "held": card.get("passed"), "at": card.get("at", "")}


def sessions_rotated(since: float, until: float) -> int:
    return int(_one("SELECT COUNT(*) FROM outcomes WHERE kind='session' AND outcome='rotated' "
                    "AND created_at >= ? AND created_at < ?", (since, until)) or 0)


def health_now() -> list[str]:
    try:
        return list(json.loads(store.kv_get("health_problems") or "[]"))
    except ValueError:
        return []


def brains_now() -> dict[str, str]:
    """Which brain could run work right now, and if not, why."""
    from . import agent as agent_mod
    out: dict[str, str] = {}
    for name in agent_mod.fallback_order():
        try:
            if not agent_mod.available(name):
                out[name] = "not set up"
            elif agent_mod.quota_down(name):
                out[name] = ("out for the billing period" if agent_mod.quota_exhausted(name)
                             else "rate-limited")
            else:
                out[name] = "up"
        except Exception:                                     # noqa: BLE001
            out[name] = "unknown"
    return out


def workworld_last() -> dict:
    """The last test-bench run, deterministic tier, with the last live run's
    sets folded in beside it. FILES, not rows: the bench runs with the database
    patched to a sandbox, so its results cannot live in one."""
    try:
        card = json.loads(WORKWORLD_LAST.read_text())
    except (OSError, ValueError):
        try:
            card = json.loads(store.kv_get("workworld:last") or "{}")     # tests
        except ValueError:
            card = {}
    try:
        live = json.loads(WORKWORLD_LAST.with_name("last-live.json").read_text())
    except (OSError, ValueError):
        live = {}
    for name, s in (live.get("sets") or {}).items():
        card.setdefault("sets", {})[name] = dict(s, tier="live", at=live.get("at", ""))
    return card


# --- the card -----------------------------------------------------------------

def _fmt_s(v):
    return "—" if v is None else f"{v:.0f} s"


def _fmt_k(v):
    return "—" if v is None else f"{v / 1000:.0f}k"


def _fmt_pct(v):
    return "—" if v is None else f"{v * 100:.0f}%"


def compute(days: int = WINDOW_DAYS, now: float | None = None) -> dict:
    """The scorecard over the last `days`, as rows with targets and states."""
    until = now or time.time()
    since = until - days * 86400
    r = replies(since, until)
    ln = reply_length(since, until)
    cr = corrections(since, until)
    p = pushes(since, until)
    a = attention_ignored(since, until)
    t = task_health(since, until)
    tf = tool_failures(since, until)
    ww = workworld_last()
    health = health_now()
    fd = front_desk(since, until)
    tt = task_tiers(since, until)
    rh = rules_holding()
    sc = self_changes(since, until)
    it = interruptions(since, until)
    da = done_alone(since, until)

    rows = [
        Row("reply_p50", "WhatsApp reply time, typical", r["p50_s"], _fmt_s(r["p50_s"]),
            "≤ 25 s (status answers come from state)", _judge(r["p50_s"], 25, 45),
            f"slow end {_fmt_s(r['p90_s'])} · first word {_fmt_s(r['first_word_p50_s'])}"),
        Row("context_per_call", "Largest session context in one call", r["context_per_call_max"],
            _fmt_k(r["context_per_call_max"]), "≤ 100k (sessions retire above it)",
            _judge(r["context_per_call_max"], 100_000, 150_000),
            "measured from 11 Sep; older turns did not record it"),
        Row("no_brain", "Messages answered without waking a brain", fd["no_brain_share"],
            _fmt_pct(fd["no_brain_share"]), "rising — status, commands, job replies",
            "na" if fd["no_brain_share"] is None else
            _judge(fd["no_brain_share"], 0.3, 0.1, lower_is_better=False),
            " · ".join(f"{k} {n}" for k, n in sorted(fd["counts"].items())) or "counted from 12 Sep"),
        Row("task_tiers", "Task legs by tier (one-file work at max effort)", tt["max_on_small"],
            " · ".join(f"{k} {n}" for k, n in sorted(tt["counts"].items())) or "—",
            "0 small changes at max effort", "good" if tt["max_on_small"] == 0 else "bad",
            "recorded while ASTA_ROUTING is on"),
        Row("tokens_per_turn", "Tokens read per chat turn", r["tokens_per_turn_avg"],
            _fmt_k(r["tokens_per_turn_avg"]), "≤ 50k", _judge(r["tokens_per_turn_avg"], 50_000, 150_000)),
        Row("reply_length", "Asta's phone reply length", ln["avg_chars"],
            "—" if ln["avg_chars"] is None else f"{ln['avg_chars']:.0f} chars",
            "≤ 400 unless asked to elaborate", _judge(ln["avg_chars"], 400, 700),
            f"{ln['over_600']} replies over 600 chars"),
        Row("corrections", "Times you had to correct or chase Asta", cr["corrections"],
            f"{cr['corrections']} of {cr['messages']} messages", "falling week on week",
            "na" if not cr["messages"] else _judge(cr["corrections"] / cr["messages"], 0.05, 0.15),
            "counted from your own words: why…, still same, I told you…"),
        Row("pushes_per_day", "Pushes to your phone per day", p["per_day"], f"{p['per_day']:.0f}",
            "≤ 20, none missed", _judge(p["per_day"], 20, 45),
            f"peak {p['peak_day']} · most for one task {p['most_for_one_task']}"),
        Row("buzzes_per_day", "Times your phone actually buzzed, per day", it["per_day"],
            "—" if it["per_day"] is None else f"{it['per_day']:.0f}",
            f"≤ {it['budget']} (the rest is read in the digest)",
            "na" if it["per_day"] is None else _judge(it["per_day"], it["budget"] or 20, 45),
            f"{it['digested']} moved to the digest · {it['digests']} digests sent · "
            f"{it['waiting']} waiting"),
        Row("done_alone", "Acts Asta did under a standing permission", da["used"],
            str(da["used"]), "only what you granted",
            "good" if da["grants"] or not da["used"] else "bad",
            f"{da['grants']} permission(s) granted — say “my permissions”"),
        Row("duplicate_pushes", "Pushes that repeat an earlier one", p["duplicate_share"],
            _fmt_pct(p["duplicate_share"]), "≤ 5%", _judge(p["duplicate_share"], 0.05, 0.15)),
        Row("ignored", "Tracked items you ignored", a["ignored_share"], _fmt_pct(a["ignored_share"]),
            "≤ 10%", _judge(a["ignored_share"], 0.10, 0.25)),
        Row("self_changes", "Changes Asta proved and kept", sc["promoted"],
            f"{sc['promoted']} kept · {sc['rejected']} thrown away", "each one measured",
            "good" if sc["rolled_back"] == 0 else "warn",
            f"{sc['live']} setting(s) tuned · {sc['rolled_back']} rolled back"),
        Row("rules_hold", "Your corrections that still hold in replay", rh["held"],
            "—" if rh["replayed"] is None else f"{rh['held']} of {rh['replayed']}",
            "all of them", "na" if not rh["replayed"] else
            ("good" if rh["held"] == rh["replayed"] else "bad"),
            f"{rh['rules']} standing rule(s) enforced · replayed {rh['at']}"),
        Row("false_done", "Plans announced as done", t["false_done"], str(t["false_done"]),
            "0", "good" if t["false_done"] == 0 else "bad"),
        Row("limit_failures", "Usage limits that became failures", t["limit_failures"],
            str(t["limit_failures"]), "0 (a limit pauses and resumes)",
            "good" if t["limit_failures"] == 0 else "bad",
            f"{t['paused']} paused instead"),
        Row("code_merged", "Code tasks that reached a merged PR", t["code_merged"],
            f"{t['code_merged']} of {t['code']}", "rising; set from the P1 baseline",
            "na" if not t["code"] else _judge(t["code_merged"] / t["code"], 0.5, 0.2, lower_is_better=False),
            f"{t['code_cancelled']} cancelled or rejected"),
        Row("tool_failures", "Tools that failed when called", tf["total"], str(tf["total"]),
            "≤ 5 a fortnight", _judge(tf["total"], 5, 20), ", ".join(tf["top"])),
        Row("health", "Open health problems", len(health), str(len(health)), "0",
            "good" if not health else "warn" if len(health) <= 3 else "bad", ", ".join(health[:6])),
    ]
    if ww:
        sets = ww.get("sets") or {}
        for name, s in sets.items():
            total, passed = s.get("total", 0), s.get("passed", 0)
            share = (passed / total) if total else None
            want = 1.0 if name == "constitution" else 0.8
            gaps = s.get("known_gaps", 0)
            rows.append(Row(f"ww_{name}", f"Test bench · {name}", share,
                            f"{passed} of {total}", "100%" if name == "constitution" else "≥ 80%",
                            _judge(share, want, want - 0.3, lower_is_better=False),
                            f"{gaps} known gap(s) · {ww.get('tier', '')} pass^{ww.get('k', 1)} "
                            f"· run {ww.get('at', '')}"))
    return {
        "generated_at": until,
        "window_days": days,
        "rows": [asdict(x) for x in rows],
        "brains": brains_now(),
        "sessions_rotated": sessions_rotated(since, until),
        "workworld": ww,
    }


# --- history ------------------------------------------------------------------

def _day_key(day: dt.date) -> str:
    return f"{SNAPSHOT_PREFIX}{day.isoformat()}"


def snapshot(day: dt.date | None = None) -> dict:
    """Freeze one finished day's card (values only) under kv `scorecard:<date>`."""
    day = day or (dt.date.today() - dt.timedelta(days=1))
    end = dt.datetime.combine(day + dt.timedelta(days=1), dt.time()).timestamp()
    card = compute(days=1, now=end)
    compact = {r["key"]: r["value"] for r in card["rows"]}
    store.kv_set(_day_key(day), json.dumps(compact))
    return compact


def history(days: int = 30, today: dt.date | None = None) -> list[dict]:
    """Oldest first: [{date, values}] for each snapshotted day in the range."""
    today = today or dt.date.today()
    out = []
    for i in range(days, 0, -1):
        d = today - dt.timedelta(days=i)
        raw = store.kv_get(_day_key(d))
        if raw:
            try:
                out.append({"date": d.isoformat(), "values": json.loads(raw)})
            except ValueError:
                continue
    return out


def backfill(days: int = 14, today: dt.date | None = None) -> int:
    """Snapshot any recent day that has none — so the first week has a trend."""
    today = today or dt.date.today()
    made = 0
    for i in range(days, 0, -1):
        d = today - dt.timedelta(days=i)
        if not store.kv_get(_day_key(d)):
            snapshot(d)
            made += 1
    return made


def _seconds_to_next(hour: int = 0, minute: int = 5, now: dt.datetime | None = None) -> float:
    now = now or dt.datetime.now()
    nxt = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if nxt <= now:
        nxt += dt.timedelta(days=1)
    return (nxt - now).total_seconds()


async def loop() -> None:
    """Snapshot yesterday just after midnight, every day. Supervised by daemon."""
    await asyncio.sleep(120)
    await asyncio.to_thread(backfill)
    while True:
        await asyncio.sleep(_seconds_to_next())
        await asyncio.to_thread(snapshot)
