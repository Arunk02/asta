"""Read-only activity for the opt-in Mac companion."""

from __future__ import annotations

import os
import re
import threading
import time
from collections import OrderedDict

from . import booking_case, delivery, followup, notify, policy, rehearsal, store, voice_mode

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


def _task_kind(task: dict) -> str:
    status, kind = task["status"], task["kind"]
    if kind == "teams_draft":
        return {"awaiting_approval": "draft_approval", "sending": "sending",
                "sent": "message_sent", "send_uncertain": "send_uncertain"}.get(
                    status, _TASK_STATES.get(status, "alert"))
    if status == "running":
        if kind == "code":
            return "coding"
        if kind == "analysis" and booking_case.investigating(task["title"], task["prompt"]):
            return "debugging"
    return _TASK_STATES.get(status, "alert")


def _waiting_replies() -> list[dict]:
    results = []
    for tracker in followup.list_open():
        task_id = store.kv_get(f"followup_draft:{tracker['id']}")
        if not task_id or not task_id.isdecimal():
            continue
        task = store.get_task(int(task_id))
        if (not task or task["kind"] != "teams_draft" or task["status"] != "sent"
                or task["teams_chat"] != tracker.get("person")):
            continue
        sent_at = next((e["created_at"] for e in reversed(store.task_events(task["id"]))
                        if e["kind"] == "status" and e["detail"] == "sending → sent"), None)
        if sent_at is None or sent_at <= max(tracker.get("created_at", 0), tracker.get("heard_until", 0)):
            continue
        if followup.their_word({**tracker, "heard_until": sent_at}) is None:
            results.append({"id": task["id"], "kind": "waiting_reply", "changed_at": sent_at})
    return results


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
            event["kind"] = _task_kind(task)
            if text.startswith("❌"):
                event["kind"] = "blocked"
    elif level in {"teams", "mail", "outlook", "email"}:
        event["kind"] = "message"
    return event


def _task(row: dict) -> dict:
    return {"id": row["id"], "kind": _task_kind(row), "changed_at": row["changed_at"]}


def activity(after: int | None) -> dict:
    statuses = (*_TASK_STATES, "sending", "send_uncertain")
    rows, unseen, seen_through, latest, tasks = store.pet_activity(after, PAGE_SIZE, statuses)
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
        "tasks": [_task(row) for row in tasks] + _waiting_replies(),
        "speech_available": can_speak,
        "speech": speech,
    }
