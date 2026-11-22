"""Read-only activity for the opt-in Mac companion."""

from __future__ import annotations

import os
import re
import threading
import time
from collections import OrderedDict

from . import delivery, notify, policy, rehearsal, store, voice_mode

PAGE_SIZE = 100
SPEECH_MAX_AGE = 60
_SPEECH_LIMIT = 512
_speech: OrderedDict[int, tuple[float, str]] = OrderedDict()
_speech_lock = threading.Lock()
_TASK = re.compile(r"^(?:[\W_]+)?(?:Task\s+)?#(\d{1,10})\b", re.IGNORECASE)
_CI_RUN = re.compile(r"https://github\.com/[\w.-]+/[\w.-]+/actions/runs/\d+\b")
_TASK_STATES = {
    "running": "running", "awaiting_approval": "approval", "done": "ready",
    "failed": "blocked", "paused": "blocked", "needs_input": "question",
}


def enabled() -> bool:
    return os.environ.get("ASTA_PET", "").strip().lower() in {"1", "true", "yes", "on"}


def speech_available() -> bool:
    return (not rehearsal.active() and notify.quiet_rule() is None
            and not delivery.quiet_now() and not voice_mode.on()
            and not voice_mode.listening() and not voice_mode.state()["busy"])


def allow_speech(notification_id: int, alert_kind: str) -> None:
    """Remember recent eligible alerts. Catch-up remains visual after a restart."""
    if not speech_available():
        return
    with _speech_lock:
        _speech[notification_id] = (time.time(), alert_kind)
        if len(_speech) > _SPEECH_LIMIT:
            _speech.popitem(last=False)


def _event(row: dict) -> dict:
    text, level = row["text"], row["level"]
    event = {"id": row["id"], "kind": "alert", "level": level, "seen": bool(row["seen"]),
             "created_at": row["created_at"]}
    if level == "ci":
        if text.startswith("🔴"):
            event["kind"] = "ci_failed"
        elif text.startswith(("✅ CI passed", "🟢", "🟡")):
            event["kind"] = "ci_passed"
        if match := _CI_RUN.search(text):
            event["run_url"] = match[0]
    elif level == "reminder":
        event["kind"] = "reminder"
    elif text.startswith("❓"):
        event["kind"] = "question"
    elif level in {"task", "action"} and (match := _TASK.match(text)):
        event["task_id"] = int(match[1])
        if task := store.get_task(event["task_id"]):
            # Approval and questions must reflect what is still waiting now.
            kind = _TASK_STATES.get(task["status"])
            event["kind"] = kind or "alert"
            if text.startswith("❌"):
                event["kind"] = "blocked"
    elif level in {"teams", "mail", "outlook", "email"}:
        event["kind"] = "message"
    return event


def _task(row: dict) -> dict:
    return {"id": row["id"], "kind": _TASK_STATES[row["status"]], "changed_at": row["changed_at"]}


def activity(after: int | None) -> dict:
    rows, unseen, seen_through, latest, tasks = store.pet_activity(after, PAGE_SIZE, tuple(_TASK_STATES))
    now = time.time()
    can_speak = speech_available()
    with _speech_lock:
        eligible = {i: kind for i, (when, kind) in _speech.items()
                    if i <= latest and now - when < SPEECH_MAX_AGE} if can_speak else {}
    speech = [{"id": row["id"], "kind": _event(row)["kind"]}
              for row in store.unseen_notifications_by_id(tuple(eligible))
              if not policy.muted_push(row["text"], row["level"], kind=eligible[row["id"]])]
    return {
        "items": [_event(row) for row in rows],
        "unseen": unseen, "seen_through": seen_through, "latest": latest,
        "cursor": rows[-1]["id"] if rows and after is not None else latest,
        "tasks": [_task(row) for row in tasks],
        "speech_available": can_speak,
        "speech": speech,
    }
