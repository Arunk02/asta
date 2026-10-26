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
    how the flow works    — a plan leg only: his documents and the repo summaries
                            on the task's subject (project_knowledge.py)

Each part is a few hundred characters, each is dropped whole rather than cut
mid-line, and a job only ever sees its own notes and plan.
"""

from __future__ import annotations

import json
import time

from . import people, policy, store, task_spec

#: Characters a pack may add, per kind of leg. A plan reads widely and needs the
#: least carried in; an implementation leg after a fresh start needs the plan.
CAPS = {"plan": 4500, "implement": 5000, "analysis": 2500, "draft": 1500}


def _approved_plan(task_id: int) -> str:
    # task_plan is updated even when history capture is explicitly disabled.
    plan = store.kv_get(f"task_plan:{task_id}") or task_spec.get(task_id) or ""
    return plan.strip()


#: What a plan leg may spend on his documents, inside the plan cap.
KNOWLEDGE_BUDGET = 1800


def _how_it_works(t: dict) -> str:
    """His documents and the repo summaries on what the task names.

    9 Oct, task 272: the plan for a manual customs status change never said the
    same update goes on to IOM, though his flow doc says CUSTOMS_UPDATE →
    manualCustomsUpdate → execution status → IOM → EH. The plan leg was handed
    his plan, notes, rules and people — never the knowledge he wrote down."""
    from . import project_knowledge
    try:
        found = project_knowledge.lookup(f"{t.get('title', '')}\n{t.get('prompt', '')}"[:1500],
                                         t.get("workspace") or "", budget=KNOWLEDGE_BUDGET,
                                         channel="plan")
    except Exception:                                          # noqa: BLE001
        return ""
    if not found:
        return ""
    return ("[How this flow works — his documents and the repo summaries. Trace the "
            "change upstream and downstream from here; where the code disagrees, the "
            "code wins and the plan says so]\n" + found)


def build(task_id: int, stage: str) -> str:
    """The pack for this leg, or '' when there is nothing worth carrying."""
    from .graph import notes
    t = store.get_task(task_id) or {}
    cap = CAPS.get(stage, 2500)
    parts: list[str] = []

    if stage != "plan":
        plan = _approved_plan(task_id)
        if plan:
            parts.append("[The plan Arun approved — the definition of done]\n"
                         + task_spec.prompt_block(task_id, plan, limit=min(3000, cap // 2)))

    # Reserve project evidence before optional conversational notes. Previously
    # a full notes block could silently evict the very flow docs used to plan.
    if stage == "plan":
        flow = _how_it_works(t)
        if flow:
            parts.append(flow)

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

    out, size, omitted = [], 0, []
    # Leave room for a visible omission notice, including when every block is too big.
    reserve = 260
    for p in parts:
        if size + len(p) + 2 > cap - reserve:
            omitted.append(p.split("\n", 1)[0][:100])
            continue
        out.append(p)
        size += len(p) + 2
    if omitted:
        out.append("[Context budget: omitted " + str(len(omitted)) + " block(s): "
                   + "; ".join(omitted)[:120]
                   + ". Retrieve required evidence before acting; absence here does not mean it was checked.]")
    store.kv_set(f"context_manifest:{task_id}:{stage}", json.dumps({
        "at": time.time(), "workspace": t.get("workspace"), "stage": stage,
        "cap_chars": cap, "included": [p.split("\n", 1)[0][:100] for p in out],
        "omitted": omitted}))
    return ("\n\n" + "\n\n".join(out)) if out else ""
