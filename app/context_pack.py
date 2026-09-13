"""Context packs: what a fresh leg is handed, instead of a transcript.

A leg that starts a fresh session — the first of a task, or one after a brain
switch, an escalation or a lost session — used to get the ticket and whatever
blocks each caller remembered to bolt on. What the job had ALREADY established
lived in the session it could not see.

A pack is the job's working set, most important first, under a cap per kind of
leg so it can never become the next token bleed:

    the approved plan     — the definition of done, once he has signed it off
    job notes             — decisions and facts from earlier legs (graph/notes.py)
    his standing rules    — the typed ones the gate enforces (policy.py)
    people and systems    — facts about who and what the job names (people.py)

Each part is a few hundred characters, each is dropped whole rather than cut
mid-line, and a job only ever sees its own notes and plan.
"""

from __future__ import annotations

from . import people, policy, store

#: Characters a pack may add, per kind of leg. A plan reads widely and needs the
#: least carried in; an implementation leg after a fresh start needs the plan.
CAPS = {"plan": 2500, "implement": 5000, "analysis": 2500, "draft": 1500}


def _approved_plan(task_id: int) -> str:
    plan = store.kv_get(f"task_plan:{task_id}") or ""
    if not plan:
        from . import task_spec
        plan = task_spec.get(task_id)
    return plan.strip()


def build(task_id: int, stage: str) -> str:
    """The pack for this leg, or '' when there is nothing worth carrying."""
    from .graph import notes
    t = store.get_task(task_id) or {}
    cap = CAPS.get(stage, 2500)
    parts: list[str] = []

    if stage != "plan":
        plan = _approved_plan(task_id)
        if plan:
            parts.append("[The plan Arun approved — the definition of done]\n" + plan[:3000])

    job_notes = notes.block(task_id).strip()
    if job_notes:
        parts.append(job_notes)

    ruled = [r.render() for r in policy.rules() if r.kind in ("never", "mute", "prefer")]
    if ruled:
        parts.append("[Arun's standing rules — enforced; do not plan around them]\n"
                     + "\n".join(f"- {x}" for x in ruled[:8]))

    known = people.about(f"{t.get('title', '')} {t.get('prompt', '')}")
    if known:
        parts.append("[What is known about the people and systems this names — dated]\n"
                     + "\n".join(f"- {people.line(r)}" for r in known))

    out, size = [], 0
    for p in parts:
        if size + len(p) > cap:
            continue            # whole blocks only: half a plan is worse than none
        out.append(p)
        size += len(p)
    return ("\n\n" + "\n\n".join(out)) if out else ""
