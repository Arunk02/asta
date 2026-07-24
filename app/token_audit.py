"""Token-waste auditor — predicts and tracks where a coding/analysis worker burns
credits, so each iteration of the pipeline can be made leaner than the last.

It reads the executor's own session log (Claude Code `*.jsonl` or Copilot
`events.jsonl`), classifies avoidable spend into named categories, estimates the
token cost of each (including the cache-read amplification — a fat tool output
re-cached on every later turn costs far more than its raw size), and scores the
run. Every audit is persisted to the `waste_audits` table so the trend line —
is waste actually going down? — is queryable.

Why this exists: the expensive things in agent coding are not the edits, they're
(1) re-discovering what project context already knows, (2) dumping raw build logs / wide
greps that re-cache every turn, (3) re-planning on a wrong understanding (a
session resume re-caches everything). This module names those so they can be
fixed, and measures whether the fixes worked.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

from . import store

CLAUDE_PROJECTS = Path.home() / ".claude" / "projects"
COPILOT_SESSIONS = Path.home() / ".copilot" / "session-state"


def _encode_project(path) -> str:
    """Claude Code names a project dir after its cwd with every '/' and '.'
    turned into '-' (e.g. /Users/arun.k.k/booking-workspace →
    -Users-arun-k-k-booking-workspace)."""
    return str(path).replace("/", "-").replace(".", "-")

# A tool result bigger than this re-caches on every subsequent turn — the main
# silent cost. Wide greps and raw build logs are the usual offenders.
FAT_OUTPUT_CHARS = 4000
CHARS_PER_TOKEN = 4  # rough; good enough for relative waste, which is the point


# --------------------------------------------------------------------------- #
# Parsing — normalise both executor log formats into one record shape.
# --------------------------------------------------------------------------- #

def _norm() -> dict:
    return {"executor": "", "calls": 0, "out_tokens": 0, "new_tokens": 0,
            "cache_read": 0, "reads": [], "bash": [], "results": {},
            "result_turn": {}, "text_blocks": [], "resumes": 0}


def _parse_claude(path: Path) -> dict:
    r = _norm(); r["executor"] = "claude"
    turn = 0
    for line in _lines(path):
        try:
            e = json.loads(line)
        except ValueError:
            continue
        m = e.get("message") or {}
        u = m.get("usage") or {}
        typ = e.get("type")
        if typ == "assistant" and u:
            r["calls"] += 1
            turn += 1
            r["out_tokens"] += u.get("output_tokens", 0)
            r["new_tokens"] += u.get("input_tokens", 0) + u.get("cache_creation_input_tokens", 0)
            r["cache_read"] += u.get("cache_read_input_tokens", 0)
        for b in (m.get("content") or []):
            if not isinstance(b, dict):
                continue
            t = b.get("type")
            if t == "text" and len(b.get("text", "")) > 1500:
                r["text_blocks"].append(len(b["text"]))
            elif t == "tool_use":
                n = b.get("name"); i = b.get("input", {})
                if n in ("Read", "read_file"):
                    r["reads"].append((i.get("file_path", ""), bool(i.get("limit") or i.get("offset"))))
                elif n == "Bash":
                    r["bash"].append(i.get("command", ""))
            elif t == "tool_result":
                c = b.get("content")
                txt = c if isinstance(c, str) else " ".join(
                    x.get("text", "") for x in (c or []) if isinstance(x, dict))
                tid = b.get("tool_use_id")
                r["results"][tid] = len(txt)
                r["result_turn"][tid] = turn
    return r


def _parse_copilot(path: Path) -> dict:
    r = _norm(); r["executor"] = "copilot"
    turn = 0
    for line in _lines(path):
        try:
            e = json.loads(line)
        except ValueError:
            continue
        typ = e.get("type", "")
        d = e.get("data") or {}
        if typ == "assistant.message":
            r["calls"] += 1
            turn += 1
            r["out_tokens"] += d.get("outputTokens") or 0
        elif typ == "tool.execution_start":
            args = d.get("arguments") or {}
            name = (d.get("name") or "").lower()
            cmd = args.get("command") or args.get("query") or ""
            if "read" in name or "file" in name and args.get("path"):
                r["reads"].append((args.get("path", ""), bool(args.get("limit") or args.get("offset"))))
            elif cmd:
                r["bash"].append(str(cmd))
        elif typ == "assistant.turn_start":
            r["resumes"] += 1  # each resume opens a new turn_start block
    # Copilot's log doesn't expose input/cache tokens; leave new_tokens=0 and note it.
    return r


def _lines(path: Path):
    try:
        with path.open() as fh:
            yield from fh
    except OSError:
        return


# --------------------------------------------------------------------------- #
# Waste detection
# --------------------------------------------------------------------------- #

def detect_waste(r: dict) -> dict:
    """Named waste categories with an estimated token cost each. The estimate
    folds in cache amplification: a fat result at turn T is re-cached on every
    later turn, so its true cost ≈ size × (calls − T)."""
    w: dict[str, dict] = {}
    calls = max(r["calls"], 1)

    # 1. Duplicate file reads — the second+ open of a path is redundant.
    seen: dict[str, int] = {}
    dup = 0
    for p, _ in r["reads"]:
        seen[p] = seen.get(p, 0) + 1
    dup = sum(c - 1 for c in seen.values() if c > 1)
    if dup:
        w["duplicate_reads"] = {"count": dup, "est_tokens": dup * 800,
                                "detail": [p.split('/')[-1] for p, c in seen.items() if c > 1]}

    # 2. Full-file reads — no line bound dumps a whole class into context.
    full = [p for p, bounded in r["reads"] if not bounded]
    if full:
        w["full_reads"] = {"count": len(full), "est_tokens": len(full) * 1500,
                           "detail": [p.split('/')[-1] for p in full]}

    # 3. Fat tool outputs — the cache-amplified cost. Waste = the EXCESS over a
    # reasonable output (a tail/head would have kept ~FAT_OUTPUT_CHARS), and that
    # excess is re-read at 10% cost on each later turn (cache reads are cheap but
    # not free). avoidable ≈ excess × (1 + 0.1 × remaining_turns).
    fat_tokens = 0
    fat_n = 0
    for tid, size in r["results"].items():
        if size > FAT_OUTPUT_CHARS:
            fat_n += 1
            excess = (size - FAT_OUTPUT_CHARS) // CHARS_PER_TOKEN
            # Cap the amplification window: beyond ~40 turns context is usually
            # compacted, so a huge output doesn't sit in cache literally forever.
            remaining = min(max(calls - r["result_turn"].get(tid, 0), 0), 40)
            fat_tokens += int(excess * (1 + 0.1 * remaining))
    if fat_n:
        w["fat_outputs"] = {"count": fat_n, "est_tokens": fat_tokens,
                            "detail": f"{fat_n} outputs >4k chars, cache-amplified"}

    # 4. Excessive greps — discovery that should have been resolver-guided.
    greps = [c for c in r["bash"] if "grep" in c or "rg " in c]
    if len(greps) > 8:
        over = len(greps) - 8
        w["excess_greps"] = {"count": len(greps), "est_tokens": over * 600,
                             "detail": f"{len(greps)} greps (resolver should cut discovery)"}

    # 5. Narration bloat.
    if r["text_blocks"]:
        nt = sum(r["text_blocks"]) // CHARS_PER_TOKEN
        if nt > 1500:
            w["narration"] = {"count": len(r["text_blocks"]), "est_tokens": nt,
                              "detail": f"{len(r['text_blocks'])} large text blocks"}

    # 6. Re-plan / resume re-cache — real but hard to isolate, and it overlaps
    # the cache already counted above, so estimate conservatively to avoid
    # double-counting: flag only a strong signature (cache-write ≫ output) and
    # attribute a modest slice (one avoided re-plan ≈ ~15% of new tokens).
    if r["executor"] == "claude" and r["new_tokens"] > 10 * max(r["out_tokens"], 1):
        w["replan_recache"] = {"count": r["resumes"],
                               "est_tokens": int(0.15 * r["new_tokens"]),
                               "detail": "cache-write ≫ output — session resumed/re-planned; "
                                         "avoid wrong first plans (context gate + lessons)"}
    return w


def audit_session(path: str | Path) -> dict:
    path = Path(path)
    r = _parse_copilot(path) if path.name == "events.jsonl" else _parse_claude(path)
    w = detect_waste(r)
    avoidable = sum(c["est_tokens"] for c in w.values())
    # Denominator: real new tokens if we have them (Claude), else a proxy.
    total = r["new_tokens"] or (r["out_tokens"] * 6) or 1
    ratio = min(avoidable / total, 0.99)
    return {
        "session": path.name,
        "executor": r["executor"],
        "calls": r["calls"],
        "out_tokens": r["out_tokens"],
        "new_tokens": r["new_tokens"],
        "cache_read": r["cache_read"],
        "waste": w,
        "avoidable_tokens": avoidable,
        "waste_ratio": round(ratio, 3),
        "grade": _grade(ratio),
        "top_fix": max(w.items(), key=lambda kv: kv[1]["est_tokens"])[0] if w else "none",
    }


def _grade(ratio: float) -> str:
    return ("A (lean)" if ratio < 0.05 else "B (ok)" if ratio < 0.12
            else "C (trim)" if ratio < 0.25 else "D (wasteful)")


# --------------------------------------------------------------------------- #
# Discovery + trend (the "each iteration" loop)
# --------------------------------------------------------------------------- #

def _session_file(task_id: int) -> Path | None:
    """Resolve the session log a finished worker task actually wrote."""
    from . import tasks
    for ex in ("claude", "copilot"):
        sid = store.kv_get(f"task_session:{task_id}:{ex}")
        if not sid:
            continue
        if ex == "copilot":
            f = COPILOT_SESSIONS / sid / "events.jsonl"
        else:
            cwd = tasks._cwd(store.get_task(task_id).get("workspace"))
            f = CLAUDE_PROJECTS / _encode_project(cwd) / f"{sid}.jsonl"
        if f.is_file():
            return f
    return None


def audit_task(task_id: int) -> dict | None:
    """Audit one task's worker session, record it to the trend, and return the
    report. Called automatically when a code/analysis task finishes so waste is
    measured every iteration, not just when someone asks."""
    f = _session_file(task_id)
    if not f:
        return None
    rep = audit_session(f)
    rep["task_id"] = task_id
    hist = _history()
    hist.append({"at": time.time(), "task_id": task_id, "waste_ratio": rep["waste_ratio"],
                 "calls": rep["calls"], "avoidable": rep["avoidable_tokens"],
                 "top_fix": rep["top_fix"], "executor": rep["executor"]})
    store.kv_set("token_audit:history", json.dumps(hist[-60:]))
    return rep


def _worker_claude_dirs() -> list[str]:
    """Claude-project dir names for the workspaces workers run in — so the audit
    measures WORKER sessions, never Asta's own assistant session."""
    from . import workspace_tools
    return [_encode_project(root) for root in workspace_tools.WORKSPACES.values()]


_MIN_SESSION_BYTES = 40_000  # a run with ≥8 model calls is far bigger; skips fragments


def recent_sessions(hours: float = 24, limit: int = 60) -> list[Path]:
    cutoff = time.time() - hours * 3600

    def fresh_and_real(p: Path) -> bool:
        try:
            st = p.stat()
        except OSError:
            return False
        return st.st_mtime > cutoff and st.st_size >= _MIN_SESSION_BYTES

    # Claude worker sessions live in one workspace dir and are few — keep every
    # fresh one. Copilot's session-state holds thousands of fragments, so sample
    # only the most-recent `limit` of those (mtime desc) before the size filter.
    claude: list[Path] = []
    for dname in _worker_claude_dirs():
        d = CLAUDE_PROJECTS / dname
        if d.is_dir():
            claude += [p for p in d.glob("*.jsonl") if fresh_and_real(p)]
    copilot: list[Path] = []
    if COPILOT_SESSIONS.is_dir():
        cand = sorted(COPILOT_SESSIONS.glob("*/events.jsonl"),
                      key=lambda p: p.stat().st_mtime if p.exists() else 0, reverse=True)
        for p in cand:
            if fresh_and_real(p):
                copilot.append(p)
            if len(copilot) >= limit:
                break
    return sorted(claude + copilot, key=lambda p: p.stat().st_mtime, reverse=True)


def audit_recent(hours: float = 24, min_calls: int = 8) -> dict:
    """Audit every worker session in the window; persist a snapshot; return the
    aggregate + the worst offenders + the trend vs the previous snapshot."""
    sessions = [audit_session(p) for p in recent_sessions(hours)]
    sessions = [s for s in sessions if s["calls"] >= min_calls]
    if not sessions:
        return {"sessions": [], "note": "no worker sessions in window"}
    agg_avoidable = sum(s["avoidable_tokens"] for s in sessions)
    agg_total = sum(s["new_tokens"] or s["out_tokens"] * 6 for s in sessions)
    ratio = round(min(agg_avoidable / max(agg_total, 1), 0.99), 3)
    # Which category costs the most across all sessions → what to fix next.
    cat_totals: dict[str, int] = {}
    for s in sessions:
        for cat, v in s["waste"].items():
            cat_totals[cat] = cat_totals.get(cat, 0) + v["est_tokens"]
    top = sorted(cat_totals.items(), key=lambda kv: -kv[1])
    prev = _last_snapshot()
    snapshot = {"at": time.time(), "hours": hours, "sessions": len(sessions),
                "waste_ratio": ratio, "avoidable_tokens": agg_avoidable,
                "top_categories": top[:5]}
    _save_snapshot(snapshot)
    return {
        "window_hours": hours,
        "sessions_audited": len(sessions),
        "aggregate_waste_ratio": ratio,
        "aggregate_avoidable_tokens": agg_avoidable,
        "top_fix_categories": top[:5],
        "trend_vs_previous": _trend(prev, ratio),
        "worst_sessions": sorted(sessions, key=lambda s: -s["avoidable_tokens"])[:5],
    }


def _trend(prev: dict | None, ratio: float) -> str:
    if not prev:
        return "first snapshot — baseline recorded"
    d = ratio - prev.get("waste_ratio", ratio)
    if abs(d) < 0.005:
        return f"flat ({ratio:.1%}, was {prev['waste_ratio']:.1%})"
    return (f"↓ improved {(-d):.1%} (now {ratio:.1%}, was {prev['waste_ratio']:.1%})"
            if d < 0 else
            f"↑ worse {d:.1%} (now {ratio:.1%}, was {prev['waste_ratio']:.1%})")


def _save_snapshot(s: dict) -> None:
    store.kv_set("token_audit:last", json.dumps(s))
    hist = _history()
    hist.append({"at": s["at"], "waste_ratio": s["waste_ratio"],
                 "sessions": s["sessions"], "avoidable": s["avoidable_tokens"]})
    store.kv_set("token_audit:history", json.dumps(hist[-60:]))


def _last_snapshot() -> dict | None:
    raw = store.kv_get("token_audit:last")
    return json.loads(raw) if raw else None


def _history() -> list:
    raw = store.kv_get("token_audit:history")
    return json.loads(raw) if raw else []


def trend_series() -> list:
    """Waste-ratio over time — the 'is each iteration leaner?' line for the UI."""
    return _history()


if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1 and sys.argv[1] not in ("recent",):
        print(json.dumps(audit_session(sys.argv[1]), indent=2))
    else:
        hrs = float(sys.argv[2]) if len(sys.argv) > 2 else 24
        print(json.dumps(audit_recent(hrs), indent=2, default=str))
