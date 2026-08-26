"""The approved plan, kept as a durable definition of done.

GSD's one good idea worth stealing: the plan a task is built against should be a
first-class artifact that survives session boundaries — not something that lives
only inside the brain's session and evaporates the moment that context is
compacted or handed to a fresh window. Asta already gates on a plan (solo.md
Stage 2) and records whether Arun approved it as-is; this keeps the approved plan
itself and re-anchors a resumed implementation leg to it, so a worker that lost
its context rebuilds against the SAME definition of done instead of drifting into
a subtly different change.

Captured at the one unambiguous moment it means something: when Arun approves the
plan. That approved text IS the definition of done — no fragile mid-output marker
parsing. Off behind ASTA_TASK_SPEC; additive; a pure no-op until flipped, exactly
like the verify and relevance gates.
"""

from __future__ import annotations

import os

from . import store

_TRUEY = ("1", "true", "yes", "on")
_MAX = 2000     # a plan, not a transcript — keep the tail where the plan lives


def enabled() -> bool:
    return os.environ.get("ASTA_TASK_SPEC", "").strip().lower() in _TRUEY


def _key(task_id: int) -> str:
    return f"task_spec:{task_id}"


def capture(task_id: int, plan_text: str) -> None:
    """Record the approved plan as the task's definition of done — once.

    A later re-plan-and-approve does NOT overwrite the first: the original bar is
    what the finished work should still be judged against, so the earliest
    approved plan wins. Keeps the tail, where the plan sits after discovery.
    """
    if not enabled():
        return
    plan_text = (plan_text or "").strip()
    if not plan_text or store.kv_get(_key(task_id)):
        return
    store.kv_set(_key(task_id), plan_text[-_MAX:])


def get(task_id: int) -> str:
    """The stored definition of done, or "" — safe to read even when disabled."""
    return store.kv_get(_key(task_id)) or ""


def preamble(task_id: int) -> str:
    """A prompt block re-anchoring a resumed worker to the approved plan, or "".

    Cheap under prompt caching and authoritative: the safety net for an
    implementation leg whose session context was compacted or started fresh, so it
    rebuilds against the plan Arun signed off rather than re-deriving one.
    """
    if not enabled():
        return ""
    spec = get(task_id)
    if not spec:
        return ""
    return ("[Definition of done — the plan Arun approved. Implement exactly this, "
            "no more and no less; if it proves wrong, say so and stop rather than "
            f"silently substituting another.]\n{spec}\n\n")
