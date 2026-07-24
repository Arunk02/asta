"""Skill-evolution loop — the measure→improve half of the token loop.

`token_audit` MEASURES where a worker burned tokens; this closes the loop. When a
waste category RECURS across runs, it writes a durable lesson-skill teaching the fix,
so the next run avoids it — the "leaner each iteration" behaviour, made real.

Bounded and safe on purpose:
- only RECURRING waste evolves (a one-off run never rewrites a skill),
- each category evolves exactly ONCE (deduped in kv), and is idempotent besides
  (write_skill replaces by slug),
- the lessons are CURATED best-practice, not model-generated prose that could drift.

The category keys match token_audit.detect_waste exactly, so a renamed signal there
is caught by test_fix_map_matches_the_auditor rather than silently going stale.
"""

from __future__ import annotations

import json
from collections import Counter

from . import learn, store, token_audit

#: waste category (from token_audit) -> the fix, shaped for learn.write_skill.
FIX_MAP: dict[str, dict] = {
    "full_reads": {
        "title": "Resolve before reading a repo file",
        "when": "Any task that needs to read code in a workspace repo.",
        "procedure": [
            "Call resolve_context (resolve-task.js) with the task's nouns FIRST.",
            "Open only the returned matches, at source:line, with a line bound.",
            "Never read a whole file just to see what is in it.",
        ],
        "pitfalls": ["A read with no offset/limit dumps the entire class into context."],
        "verification": ["Every read this turn was bounded and came from a resolve match."],
        "confidence": 0.85,
    },
    "duplicate_reads": {
        "title": "Read a file once, carry the anchor",
        "when": "A task that revisits the same file across several steps.",
        "procedure": [
            "Keep the source:line anchors a resolve returned in working notes.",
            "Re-open at the specific anchor instead of re-reading the file from the top.",
        ],
        "pitfalls": ["Re-reading the same path 3+ times re-pays its tokens each time."],
        "verification": ["No path appears more than once in the turn's read list."],
        "confidence": 0.8,
    },
    "fat_outputs": {
        "title": "Bound big tool outputs — they re-cache every turn",
        "when": "Running builds, wide greps, or anything that can emit a large blob.",
        "procedure": [
            "Pipe to head/tail or grep for the signal (errors, the failing test).",
            "Never dump a full build log or a wide grep into context.",
        ],
        "pitfalls": ["A 8k-char result at turn 1 re-caches on every later turn — its true "
                     "cost is many times its size."],
        "verification": ["No single tool result exceeds a few thousand characters."],
        "confidence": 0.85,
    },
    "excess_greps": {
        "title": "Route discovery through the resolver, not blind greps",
        "when": "Locating where a behaviour lives in a repo.",
        "procedure": [
            "Ask resolve_context first — it returns the entry files and matches.",
            "Grep only to confirm a specific anchor, not to explore.",
        ],
        "pitfalls": ["A storm of greps is discovery the resolver already did for ~350 tokens."],
        "verification": ["Fewer than ~8 grep/rg calls in the run."],
        "confidence": 0.8,
    },
    "narration": {
        "title": "Report in pointers, not prose",
        "when": "Summarising work back at a gate or on completion.",
        "procedure": [
            "Hand back findings as file:line pointers and a one-line verdict.",
            "Do not paste diffs, logs, or long restatements the reader can open themselves.",
        ],
        "pitfalls": ["Large narration blocks are re-cached every subsequent turn."],
        "verification": ["No multi-thousand-character text block in the transcript."],
        "confidence": 0.75,
    },
    "replan_recache": {
        "title": "Get the first plan right — a resume re-caches everything",
        "when": "Planning a code change before implementing.",
        "procedure": [
            "Run the discovery/context gate and confirm the flow BEFORE planning.",
            "Resolve ambiguity with ask_user at the gate, not by re-planning mid-run.",
        ],
        "pitfalls": ["A session resume/re-plan re-caches the whole context — the priciest "
                     "avoidable event in a run."],
        "verification": ["The plan was approved without a full re-plan cycle."],
        "confidence": 0.8,
    },
}

_DONE_KEY = "skill_evolution:done"


def recurring(history: list[dict], min_count: int = 2) -> list[str]:
    """Waste categories that were the top offender in >= min_count recent runs.

    One wasteful run is noise; the same waste twice is a pattern worth a skill."""
    counts = Counter(h["top_fix"] for h in history
                     if h.get("top_fix") and h["top_fix"] not in ("none", ""))
    return [cat for cat, n in counts.items() if n >= min_count and cat in FIX_MAP]


def _done() -> set[str]:
    raw = store.kv_get(_DONE_KEY)
    try:
        return set(json.loads(raw)) if raw else set()
    except (ValueError, TypeError):
        return set()


def evolve(history: list[dict] | None = None, min_count: int = 2) -> list[dict]:
    """Write a fix-skill for each newly-recurring waste category. Returns what it
    evolved (empty when nothing recurs or all recurring ones were already done)."""
    hist = history if history is not None else token_audit.trend_series()
    done = _done()
    evolved: list[dict] = []
    for cat in recurring(hist, min_count):
        if cat in done:
            continue
        path = learn.write_skill(FIX_MAP[cat], source="evolution")
        if path:
            evolved.append({"category": cat, "skill": path.stem, "path": str(path)})
            done.add(cat)
    if evolved:
        store.kv_set(_DONE_KEY, json.dumps(sorted(done)))
    return evolved
