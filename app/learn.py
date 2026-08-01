"""Turn finished work into skills, so the same lesson isn't relearned.

The gap this closes: after five days of heavy use Asta had one fact file and one
skill file against eighteen episodes. Episodes are prose digests — diary entries,
not procedures. The lessons that actually matter (`mvn clean` before a MapStruct
build, `--allow-all-paths` is separate from `--allow-all-tools`, Jira acceptance
criteria hide in comments) lived in Arun's head and nowhere in Asta.

Two halves, and they only pay off together:

  extraction  a run that took real work is distilled into a structured skill —
              when to use it, the procedure, the pitfalls, how to verify.
  teacher     when a cheap tier fails and a stronger one rescues it, the STRONGER
              run writes the skill. Escalation without this teaches nothing about
              tomorrow: the same task escalates again next week.

Guards, because a memory that fills with noise is worse than an empty one:
  - only runs that took ≥2 rounds or escalated are candidates;
  - the model must return usable JSON with a confidence, and below MIN_CONFIDENCE
    nothing is written;
  - a near-duplicate title updates the existing skill instead of adding a rival;
  - unused low-confidence skills are pruned.

Extraction runs on the local model when LM Studio is up — free, and this fires
after every substantial task.
"""

from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path

from . import memory, skills, store

ROOT = Path(__file__).resolve().parent.parent
USAGE_FILE = skills.SKILLS_DIR / ".usage.json"

#: Below this, the model is guessing. Odysseus's threshold, and it holds up:
#: a wrong procedure is followed confidently, which is worse than none.
MIN_CONFIDENCE = float(os.environ.get("ASTA_SKILL_MIN_CONFIDENCE", "0.6"))

#: An unused skill below this confidence is pruned after PRUNE_AFTER_DAYS.
PRUNE_CONFIDENCE = 0.75
PRUNE_AFTER_DAYS = 30

MAX_TRANSCRIPT = 12000

EXTRACT_PROMPT = """You are distilling a completed engineering task into a REUSABLE skill —
a procedure the next run can follow without rediscovering anything.

TASK: {title}
OUTCOME: {outcome}
{escalation_note}
WHAT HAPPENED (tail of the run):
{transcript}

Return ONLY a JSON object, no prose around it:
{{
  "title": "short imperative name, e.g. 'Rebuild MapStruct mappers before running tests'",
  "when": "the situation that should trigger this skill, in one or two sentences",
  "procedure": ["ordered", "concrete", "steps"],
  "pitfalls": ["what goes wrong and how it looks when it does"],
  "verification": ["how to know it worked"],
  "tags": ["short", "lowercase"],
  "confidence": 0.0
}}

Rules:
- Only write a skill if this run taught something GENERAL. A task that just ran a
  standard flow with no surprise teaches nothing — return {{"confidence": 0}}.
- Nothing specific to this one ticket: no issue keys, no one-off file paths, no dates.
- confidence: how sure you are this generalises. Below 0.6 it will be discarded, so
  be honest rather than generous.
"""

_JSON = re.compile(r"\{.*\}", re.DOTALL)


def should_extract(rounds: int = 0, escalated: bool = False, status: str = "done") -> bool:
    """Was there enough work here to be worth distilling?

    A one-shot answer teaches nothing; a run that needed several rounds, or that
    a weaker tier could not finish, is exactly where the lesson lives.
    """
    if status not in ("done", "sent"):
        return False
    return escalated or rounds >= 2


def _usage() -> dict:
    try:
        return json.loads(USAGE_FILE.read_text())
    except (OSError, json.JSONDecodeError):
        return {}


def _save_usage(data: dict) -> None:
    try:
        skills.SKILLS_DIR.mkdir(parents=True, exist_ok=True)
        USAGE_FILE.write_text(json.dumps(data, indent=2, sort_keys=True))
    except OSError:
        pass


def record_use(name: str) -> None:
    """Called when a skill is actually loaded."""
    data = _usage()
    entry = data.setdefault(name, {"uses": 0, "confidence": 1.0, "created": time.time()})
    entry["uses"] = int(entry.get("uses", 0)) + 1
    entry["last_used"] = time.time()
    _save_usage(data)


#: A skill loaded within this window of a run finishing is treated as having been
#: in play for it. Wide enough for a long task, narrow enough that yesterday's
#: reading does not get the credit for today's result.
CREDIT_WINDOW = 3 * 3600


def credit(status: str, since: float, window: float = CREDIT_WINDOW) -> list[str]:
    """Record whether the skills in play actually helped. Returns the ones judged.

    `uses` alone was never evidence of worth — it counts being LOADED, which a
    skill earns by having a matching title, not by being right. A wrong procedure
    followed confidently is loaded just as often as a correct one, and scored
    identically, so the archive could only ever grow.

    This is the missing signal: a run that finished credits what it read, a run
    that failed debits it. Nothing is deleted on one bad result — a skill can be
    loaded for a task that was doomed for unrelated reasons — but a procedure that
    keeps being present when things go wrong stops being protected from pruning.
    """
    good = status in ("done", "sent", "shipped")
    cutoff = max(0.0, since - 60)      # a skill read just before the clock started counts
    data, judged = _usage(), []
    for name, entry in data.items():
        last = float(entry.get("last_used") or 0)
        if last < cutoff or time.time() - last > window:
            continue
        key = "helped" if good else "missed"
        entry[key] = int(entry.get(key, 0)) + 1
        judged.append(name)
    if judged:
        _save_usage(data)
    return judged


def scoreboard() -> list[dict]:
    """Every skill with its evidence, worst first — what to read before trusting one."""
    rows = []
    for name, e in _usage().items():
        helped, missed = int(e.get("helped", 0)), int(e.get("missed", 0))
        rows.append({"name": name, "uses": int(e.get("uses", 0)),
                     "helped": helped, "missed": missed,
                     "confidence": float(e.get("confidence", 1.0)),
                     "net": helped - missed})
    return sorted(rows, key=lambda r: (r["net"], -r["missed"]))


def stats() -> dict:
    return _usage()


def _slug(title: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", (title or "skill").lower()).strip("-")[:48] or "skill"


def _render(data: dict) -> str:
    """The structured body. Fixed headings on purpose — a model reading this
    mid-task needs to find the procedure without parsing prose."""
    def bullets(key: str) -> str:
        items = [str(x).strip() for x in (data.get(key) or []) if str(x).strip()]
        return "\n".join(f"- {x}" for x in items) or "- (none recorded)"

    def steps() -> str:
        items = [str(x).strip() for x in (data.get("procedure") or []) if str(x).strip()]
        return "\n".join(f"{i}. {x}" for i, x in enumerate(items, 1)) or "1. (none recorded)"

    tags = ", ".join(str(t).strip() for t in (data.get("tags") or []) if str(t).strip())
    return (
        f"---\nname: {_slug(data['title'])}\n"
        f"description: {data.get('when', '').strip()[:280]}\n"
        f"tags: {tags}\n"
        f"confidence: {float(data.get('confidence', 0)):.2f}\n"
        f"source: {data.get('source', 'extracted')}\n"
        f"---\n\n"
        f"# {data['title'].strip()}\n\n"
        f"## When to use\n{data.get('when', '').strip()}\n\n"
        f"## Procedure\n{steps()}\n\n"
        f"## Pitfalls\n{bullets('pitfalls')}\n\n"
        f"## Verification\n{bullets('verification')}\n"
    )


def _existing_match(slug: str) -> Path | None:
    """A skill about the same thing should be REPLACED, not shadowed by a rival —
    two procedures for one situation is how a memory starts contradicting itself."""
    path = skills.SKILLS_DIR / f"{slug}.md"
    if path.exists():
        return path
    for p in skills.SKILLS_DIR.glob("*.md"):
        if p.stem == slug or _slug(p.stem) == slug:
            return p
    return None


def write_skill(data: dict, source: str = "extracted") -> Path | None:
    """Persist one extracted skill. None when it didn't clear the bar."""
    title = (data.get("title") or "").strip()
    confidence = float(data.get("confidence") or 0)
    if not title or confidence < MIN_CONFIDENCE:
        return None
    if not (data.get("procedure") or []):
        return None
    data = {**data, "source": source}
    slug = _slug(title)
    path = _existing_match(slug) or (skills.SKILLS_DIR / f"{slug}.md")
    skills.SKILLS_DIR.mkdir(parents=True, exist_ok=True)
    path.write_text(_render(data))
    usage = _usage()
    entry = usage.setdefault(slug, {"uses": 0, "created": time.time()})
    entry["confidence"] = confidence
    entry["source"] = source
    entry["updated"] = time.time()
    _save_usage(usage)
    return path


def _parse(raw: str) -> dict | None:
    m = _JSON.search(raw or "")
    if not m:
        return None
    try:
        data = json.loads(m.group(0))
    except json.JSONDecodeError:
        return None
    return data if isinstance(data, dict) else None


async def _distil(prompt: str) -> str | None:
    """Cheapest brain that can do the job.

    Local first — this fires after every substantial task, and paying an API call
    each time would make the learning loop something you'd want to switch off. But
    it does not stop at local: `should_extract` has already decided this run taught
    something (two rounds, or an escalation), and refusing to spend anything on the
    one lesson worth keeping is how an archive that "learns day by day" ends up
    learning on the few days LM Studio happened to be running.
    """
    return await memory.cheap_complete(prompt, 900, paid_ok=True)


async def extract(title: str, transcript: str, *, outcome: str = "done",
                  escalated: bool = False, source: str = "extracted") -> Path | None:
    """Distil one finished run into a skill. Returns the file, or None.

    Never raises: learning is a side effect of finishing work, and a failure here
    must not fail the work.
    """
    try:
        note = ("This run ESCALATED: a cheaper tier could not finish it and a stronger one "
                "did. Write the skill so the cheaper tier succeeds alone next time — that is "
                "the entire point of this extraction.\n" if escalated else "")
        prompt = EXTRACT_PROMPT.format(
            title=title, outcome=outcome, escalation_note=note,
            transcript=(transcript or "")[-MAX_TRANSCRIPT:])
        raw = await _distil(prompt)
        data = _parse(raw or "")
        if not data:
            return None
        path = write_skill(data, source="teacher" if escalated else source)
        if path:
            store.record_outcome("skill", "written", subject=path.stem,
                                 detail=f"confidence={data.get('confidence')} escalated={escalated}")
        return path
    except Exception:
        return None


#: Loaded this many more times on runs that failed than on runs that worked, and
#: the skill is actively misleading rather than merely unused.
HARMFUL_NET = -2


def prune() -> list[str]:
    """Drop skills that never earned their place, or that earned the wrong one.

    Two grounds, and the second is the one that makes this a learning loop rather
    than a garbage collector: unused-and-unconfident after a month, OR present for
    materially more failures than successes. Without the second, the only skill
    that could ever leave was one nobody read — so a confidently wrong procedure,
    which is read constantly, was the single thing pruning could not touch.
    """
    usage, removed = _usage(), []
    cutoff = time.time() - PRUNE_AFTER_DAYS * 86400
    for name, entry in list(usage.items()):
        helped, missed = int(entry.get("helped", 0)), int(entry.get("missed", 0))
        harmful = (helped - missed) <= HARMFUL_NET
        if not harmful:
            if entry.get("uses", 0) > 0:
                continue
            if float(entry.get("confidence", 1.0)) >= PRUNE_CONFIDENCE:
                continue
            if float(entry.get("created", time.time())) > cutoff:
                continue
        path = skills.SKILLS_DIR / f"{name}.md"
        if path.exists():
            path.unlink()
        usage.pop(name, None)
        removed.append(name)
    if removed:
        _save_usage(usage)
    return removed


async def daily_pass() -> str:
    """The once-a-day learning pass. Returns one line, or "" when nothing changed.

    It exists because every learning path was previously hung off the end of a
    BACKGROUND TASK — so a week of chat, corrections, and CI investigations taught
    nothing at all, and the archive only moved on days he happened to delegate
    something. Waste patterns recur across everything, not just delegated work, so
    this runs on the whole recent history regardless of what produced it.

    Never raises: this is housekeeping, and housekeeping that can break the
    morning brief is worse than housekeeping that quietly skips a day.
    """
    from . import skill_evolution
    parts = []
    try:
        evolved = skill_evolution.evolve()
        if evolved:
            parts.append("learned " + ", ".join(e["skill"] for e in evolved))
    except Exception:
        pass
    try:
        dropped = prune()
        if dropped:
            parts.append(f"dropped {len(dropped)} that weren't earning it "
                         f"({', '.join(dropped[:3])})")
    except Exception:
        pass
    if not parts:
        return ""
    line = "🧬 " + "; ".join(parts) + "."
    store.record_outcome("learning", "daily_pass", detail=line[:200])
    return line
