"""Getting better on its own — observe, diagnose, propose, prove, promote.

P6, and R8: nothing turned a failure into a change, so the same kind of bug
came back and he repeated himself. Every day already produces the signal —
approved as-is, amended, rejected, ignored, replied, missed, interrupted for
noise — and it was logged and read by nobody.

The loop is deliberately unexciting, because the only interesting question is
what stops it doing harm:

    OBSERVE    the bench's last run, the simulated day, the scorecard, outcomes
    DIAGNOSE   cluster that into named problems ("interrupted for noise")
    PROPOSE    one bounded change per cluster, from a fixed list of knobs
    PROVE      run the bench with it applied, against the version running now
    PROMOTE    only on a measured gain, and only if the constitution still passes
    ROLLBACK   restore the previous value — as a drill, or when live scores drop

What it may change is the short list in app/settings.py, inside bounds. What it
may never change is the constitution: consent before an outward act, the right
account per repo, plain commits, tests that never touch the live database, a
plan gate on every code job, crisp by default. Those have their own scenarios,
and a candidate that breaks one is rejected however much else it improves —
checked here, not trusted.

L3 (changing Asta's own code) is a PROPOSAL only: it writes the failing scenario
first and hands the fix to the ordinary code lane, where his plan gate governs
it exactly like any other task. Off unless ASTA_EVOLVE_L3=1.

Off unless ASTA_EVOLVE=1; the nightly runner keeps it inside the same window and
brain budget as the live bench.
"""

from __future__ import annotations

import contextlib
import json
import os
import time
from dataclasses import dataclass

from . import settings, store

CONSTITUTION = "constitution"


def enabled() -> bool:
    return os.environ.get("ASTA_EVOLVE", "").strip().lower() in ("1", "true", "yes", "on")


def l3_enabled() -> bool:
    return os.environ.get("ASTA_EVOLVE_L3", "").strip().lower() in ("1", "true", "yes", "on")


@dataclass(frozen=True)
class Candidate:
    level: str
    knob: str
    after: float
    cluster: str
    why: str
    #: What the knob was worth when this was proposed. Read live instead, the
    #: record of a promoted change said "0.9 → 0.9" — it had already moved.
    was: float | None = None

    def before(self) -> float:
        return settings.value(self.knob) if self.was is None else self.was

    def render(self) -> str:
        return f"{self.knob}: {self.before():g} → {self.after:g} ({self.why})"


# --- observe ---------------------------------------------------------------------------

def observe() -> dict:
    """What yesterday actually looked like, from what is already recorded."""
    from .workworld import runner
    try:
        bench = json.loads(runner.LAST.read_text())
    except (OSError, ValueError):
        bench = {}
    day = bench.get("day") or {}
    since = time.time() - 7 * 86400
    rows = store.recent_outcomes(500)
    counts: dict[str, int] = {}
    for o in rows:
        if o["created_at"] >= since:
            counts[f"{o['kind']}/{o['outcome']}"] = counts.get(f"{o['kind']}/{o['outcome']}", 0) + 1
    return {"bench": bench, "day": day, "outcomes": counts,
            "pass_rate": bench.get("pass_rate"), "at": bench.get("at", "")}


# --- diagnose --------------------------------------------------------------------------

def diagnose(seen: dict) -> list[str]:
    """The named problems in what was seen, worst first. [] when the day was fine."""
    day, counts = seen.get("day") or {}, seen.get("outcomes") or {}
    out = []
    if (day.get("false_interrupts") or 0) > 0:
        out.append("interrupted for noise")
    if (day.get("missed") or 0) > 0:
        out.append("missed something he needed")
    if (day.get("pushes") or 0) > 20:
        out.append("too many interruptions")
    if counts.get("attention/ignored", 0) > 3 * max(1, counts.get("attention/he replied", 0)):
        out.append("most of what it pushes is ignored")
    if counts.get("verify_round/failed", 0) > counts.get("verify/passed", 0):
        out.append("checks fail more often than they pass")
    return out


# --- propose ---------------------------------------------------------------------------

#: One bounded move per problem. Small on purpose: a change that cannot be
#: measured in one night's bench is a change nobody can defend.
def propose(clusters: list[str]) -> list[Candidate]:
    out: list[Candidate] = []
    for cluster in clusters:
        if cluster == "too many interruptions":
            # The knob that actually moves this number first. Batching was tried
            # ahead of it and cannot help: the day's events are spread far wider
            # than any coalescing window, so it proposed a change worth nothing.
            out.append(Candidate("L1", "ASTA_ATTENTION_IGNORE_SHARE",
                                 max(0.6, round(settings.value("ASTA_ATTENTION_IGNORE_SHARE") - 0.05, 2)),
                                 cluster, "move a noisy feed to the digest sooner"))
            out.append(Candidate("L1", "ASTA_COALESCE_SECONDS",
                                 min(600, settings.value("ASTA_COALESCE_SECONDS") * 1.5),
                                 cluster, "let one buzz carry more"))
        elif cluster == "most of what it pushes is ignored":
            out.append(Candidate("L1", "ASTA_ATTENTION_IGNORE_SHARE",
                                 max(0.6, round(settings.value("ASTA_ATTENTION_IGNORE_SHARE") - 0.05, 2)),
                                 cluster, "move a noisy feed to the digest sooner"))
        elif cluster == "missed something he needed":
            out.append(Candidate("L1", "ASTA_ATTENTION_IGNORE_SHARE",
                                 min(0.95, round(settings.value("ASTA_ATTENTION_IGNORE_SHARE") + 0.05, 2)),
                                 cluster, "demote less readily — something got through the net"))
        elif cluster == "interrupted for noise":
            out.append(Candidate("L1", "ASTA_ATTENTION_IGNORE_SHARE",
                                 max(0.6, round(settings.value("ASTA_ATTENTION_IGNORE_SHARE") - 0.05, 2)),
                                 cluster, "move a noisy feed to the digest sooner"))
            out.append(Candidate("L1", "ASTA_ATTENTION_MIN_SEEN",
                                 max(5, settings.value("ASTA_ATTENTION_MIN_SEEN") - 2),
                                 cluster, "judge a noisy source on a shorter record"))
        elif cluster == "checks fail more often than they pass":
            out.append(Candidate("L1", "ASTA_RESPOND_MAX_PER_HOUR",
                                 max(1, settings.value("ASTA_RESPOND_MAX_PER_HOUR") - 1),
                                 cluster, "spend fewer investigations while they are unreliable"))
    seen: set[str] = set()
    kept = []
    for c in out:
        if c.knob in seen or not settings.within_bounds(c.knob, c.after) or c.after == c.before():
            continue
        seen.add(c.knob)
        kept.append(Candidate(c.level, c.knob, c.after, c.cluster, c.why,
                              was=settings.value(c.knob)))
    return kept


# --- prove -----------------------------------------------------------------------------

def _score(summary: dict) -> tuple[float, int, int]:
    """(pass rate, pushes, missed) — the three numbers a candidate has to beat."""
    day = summary.get("day") or {}
    return (float(summary.get("pass_rate") or 0),
            int(day.get("pushes") or 0), int(day.get("missed") or 0))


async def measure(k: int = 1) -> dict:
    """Run the bench and the day as they stand now — INCLUDING what has been tuned.

    A tuned knob lives in the database, and the bench runs in a sandbox with a
    database of its own, so a promoted change was invisible to every later
    measurement: tomorrow's baseline measured an Asta nobody was running, and
    the same change could be proposed and "proved" all over again.
    """
    from .workworld import day as day_mod, runner
    was = {name: os.environ.get(name) for name in settings.overrides()}
    for name, value in settings.overrides().items():
        os.environ[name] = str(value)
    try:
        results = await runner.run_all(k=k)
        seen = await day_mod.run()
    finally:
        for name, old in was.items():
            if old is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = old
    return runner.summarise(results, k=k, live=False, day_summary=day_mod.summary(seen))


async def prove(c: Candidate, k: int = 1, baseline: dict | None = None) -> dict:
    """Measure the candidate against the version running now. Never promotes."""
    base = baseline or await measure(k)
    before = c.before()
    settings.set_override(c.knob, c.after)
    was = os.environ.get(c.knob)
    # The bench runs in a sandbox with its own database, so the override above
    # cannot reach it; the environment can.
    os.environ[c.knob] = str(c.after)
    try:
        after = await measure(k)
    finally:
        if was is None:
            os.environ.pop(c.knob, None)
        else:
            os.environ[c.knob] = was
        # The measurement is over: put it back, whatever happens next. Promotion
        # is a separate, recorded decision.
        _restore(c.knob, before)
    b_rate, b_push, b_missed = _score(base)
    a_rate, a_push, a_missed = _score(after)
    sets = (after.get("sets") or {}).get(CONSTITUTION) or {}
    constitution_ok = sets.get("total", 0) == sets.get("passed", 0)
    better = (a_rate > b_rate) or (a_rate == b_rate and a_push < b_push)
    ok = bool(constitution_ok and better and a_missed <= b_missed and a_rate >= b_rate)
    return {"ok": ok, "constitution_ok": constitution_ok,
            "before": {"pass_rate": b_rate, "pushes": b_push, "missed": b_missed},
            "after": {"pass_rate": a_rate, "pushes": a_push, "missed": a_missed},
            "gain": f"pass {b_rate:.3f}→{a_rate:.3f}, pushes {b_push}→{a_push}, "
                    f"missed {b_missed}→{a_missed}"}


# --- promote, and take it back ------------------------------------------------------------

def _restore(knob: str, before: float) -> None:
    """Put a knob back. Back to the shipped value means NO override at all —
    comparing the two as text left "120.0" pinned over a default of 120, so a
    rollback restored the number and quietly kept the pin."""
    default = settings.KNOBS.get(knob, (None,))[0]
    if default is not None and float(before) == float(default):
        settings.clear(knob)
    else:
        settings.set_override(knob, float(before))


def record(c: Candidate, state: str, gain: str = "") -> int:
    with store._connect() as conn:
        cur = conn.execute(
            "INSERT INTO evolutions (level, knob, before_value, after_value, cluster, why, "
            "state, gain, created_at, decided_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (c.level, c.knob, str(c.before()), str(c.after), c.cluster, c.why, state,
             gain[:300], time.time(), time.time()))
        eid = cur.lastrowid
    store.record_outcome("evolution", state, subject=str(eid), detail=c.render()[:200])
    return eid


def promote(c: Candidate, gain: str) -> int:
    """Apply a proven change, and start the day it has to survive."""
    eid = record(c, "promoted", gain)
    settings.set_override(c.knob, c.after)
    store.kv_set(f"evolve_canary:{eid}", json.dumps(
        {"knob": c.knob, "before": c.before(), "until": time.time() + 24 * 3600}))
    return eid


def rollback(evolution_id: int, why: str = "rolled back") -> str:
    """Put a promoted change back the way it was — a drill, or a live regression."""
    with store._connect() as conn:
        row = conn.execute("SELECT * FROM evolutions WHERE id=?", (evolution_id,)).fetchone()
    if not row or row["state"] != "promoted":
        return ""
    knob, before = row["knob"], float(row["before_value"])
    _restore(knob, before)
    with store._connect() as conn:
        conn.execute("UPDATE evolutions SET state='rolled_back', gain=?, decided_at=? WHERE id=?",
                     (why[:300], time.time(), evolution_id))
    store.kv_del(f"evolve_canary:{evolution_id}")
    store.record_outcome("evolution", "rolled_back", subject=str(evolution_id), detail=why[:200])
    return f"{knob} back to {before} ({why})"


def history(limit: int = 20) -> list[dict]:
    with store._connect() as conn:
        rows = conn.execute("SELECT * FROM evolutions ORDER BY id DESC LIMIT ?",
                            (limit,)).fetchall()
    return [dict(r) for r in rows]


def summary() -> str:
    rows = history(8)
    if not rows:
        return "Asta has not changed anything about itself yet."
    out = []
    for r in rows:
        out.append(f"  {r['id']}. [{r['state']}] {r['knob']} {r['before_value']}→{r['after_value']}"
                   f" — {r['cluster']}" + (f" · {r['gain']}" if r["gain"] else ""))
    return "What Asta has changed about itself:\n" + "\n".join(out) + \
           "\nSay “roll back <n>” to undo one."


# --- L3: a code fix is a proposal, never an act -------------------------------------------

def propose_code_fix(cluster: str, evidence: str) -> str:
    """Hand a problem to the ordinary code lane, where his plan gate governs it.

    It writes the failing scenario FIRST — the bench is how anything here is
    judged, including Asta's own fixes — and then stops for his approval like
    every other code task. Nothing is written without it.
    """
    if not l3_enabled():
        return ""
    from . import tasks
    prompt = (f"Asta's own bench says: {cluster}.\n\nEvidence:\n{evidence[:1200]}\n\n"
              "Add a WorkWorld scenario that FAILS for this reason first, then the "
              "smallest change that makes it pass. Do not touch the constitution "
              "scenarios or anything they protect.")
    t = tasks.spawn(f"Asta: {cluster}", prompt, kind="code", workspace=None)
    store.record_outcome("evolution", "code_proposed", subject=str(t["id"]), detail=cluster[:200])
    return f"🧬 Task #{t['id']} — a fix for “{cluster}”, waiting at the plan gate as usual."


# --- the nightly pass -----------------------------------------------------------------------

async def nightly(k: int = 1) -> dict:
    """One pass: look, diagnose, and prove the first candidate. Promote if it wins."""
    if not enabled():
        return {"ran": False, "why": "ASTA_EVOLVE is off"}
    # Measure FIRST, and diagnose what the version running now actually does.
    # Reading yesterday's recorded run instead diagnosed a configuration that
    # may no longer be in force — it reported "nothing to fix" about a day it
    # had not looked at. The same measurement is the baseline every candidate
    # has to beat, so this costs nothing extra.
    baseline = await measure(k)
    seen = {**observe(), "day": (baseline.get("day") or {}),
            "pass_rate": baseline.get("pass_rate")}
    clusters = diagnose(seen)
    from . import learners
    from_him = []
    with contextlib.suppress(Exception):
        from_him = [c.cluster for c in learners.propose()]
    clusters = list(dict.fromkeys([*clusters, *from_him]))
    if not clusters:
        return {"ran": True, "clusters": [], "promoted": None, "why": "nothing to fix"}
    candidates = propose(clusters)
    # What HE decided, not what the bench scored. The bench says whether Asta
    # works; the ledger says whether he agreed with it, and those are different
    # questions — P6 could only ever hear the first one. Same fence either way:
    # every candidate below is proved against the version running now.
    from . import learners
    with contextlib.suppress(Exception):
        candidates = candidates + learners.propose()
    if not candidates:
        return {"ran": True, "clusters": clusters, "promoted": None,
                "why": "no bounded change worth trying"}
    for c in candidates:
        result = await prove(c, k=k, baseline=baseline)
        if result["ok"]:
            eid = promote(c, result["gain"])
            return {"ran": True, "clusters": clusters, "promoted": eid,
                    "change": c.render(), "gain": result["gain"]}
        record(c, "rejected", result["gain"])
    return {"ran": True, "clusters": clusters, "promoted": None,
            "why": "no candidate beat the version running now"}


async def loop(interval: int = 3600) -> None:
    import asyncio
    from .workworld import nightly as night
    while True:
        await asyncio.sleep(interval)
        try:
            # The night and his sleep, not the live bench's flag: proving a
            # candidate runs the deterministic bench and costs no brain at all.
            if not enabled() or night.quiet_window():
                continue
            if store.kv_get("evolve_ran:" + time.strftime("%Y-%m-%d")):
                continue
            store.kv_set("evolve_ran:" + time.strftime("%Y-%m-%d"), "1")
            out = await nightly()
            if out.get("promoted"):
                from . import notify
                await notify.notify(
                    f"🧬 Asta tuned itself: {out['change']}\n{out['gain']}\n"
                    f"Say “roll back {out['promoted']}” to undo it.", "evolution",
                    urgency="ambient")
        except Exception:                                   # noqa: BLE001
            pass
