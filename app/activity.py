"""What is Asta doing right now — answered from local state, no model involved.

"what's the update?" used to be routed to Copilot like any other question, which
meant it resumed a session already carrying a couple of hundred tool runs and
billed a full agentic turn just to say "still working". Worse, while a long turn
held the socket the question sat in a client-side queue and got no answer at all.

Everything here comes from the tasks table, the running processes, and Copilot's
own session event log. It costs nothing and answers instantly.
"""

from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path

from . import store

SESSION_DIR = Path.home() / ".copilot" / "session-state"

# A session only counts as "right now" if it was touched this recently.
RECENT_SECONDS = int(os.environ.get("ASTA_STATUS_RECENT", "1800"))
# …but "what did I miss" spans however long Arun was away, so finished work is
# recapped over a much wider window. 30 min was right for "is it still working?"
# and wrong for someone back after five hours.
RECAP_SECONDS = int(os.environ.get("ASTA_STATUS_RECAP", str(24 * 3600)))

_DONE_ICON = {"done": "✅", "failed": "❌", "cancelled": "⏹", "rejected": "⏹",
              "awaiting_approval": "⏸"}

# Deliberately narrow: only phrasings that are unambiguously "what are you doing",
# so a real question never gets swallowed by a canned answer.
_STATUS_ASK = re.compile(
    r"^\s*(what(?:'s| is)?\s+(?:the\s+)?(?:update|status|going on|happening)"
    r"|any\s+update|status|progress|are you (?:done|still working)|done\s*\?)\s*[?.!]*\s*$",
    re.I,
)


def is_status_ask(text: str) -> bool:
    return bool(_STATUS_ASK.match(text or ""))


# A follow-up sent while a turn is already running is one of three things, and
# getting it right is the whole ballgame token-wise: fold a refinement in for
# free, or stop wrong work the moment it turned wrong — never throw away (and
# re-bill) a correct answer that was almost done.
#
# Redirect = "what you're doing is now wrong, stop it." Kept strong and mostly
# anchored to the start so a passing "no" mid-sentence doesn't nuke live work.
_REDIRECT = re.compile(
    r"^\s*(no+\b|nope|nah|stop\b|cancel\b|scrap\b|hold on|wait,? (no|stop|forget)|"
    r"forget (it|that|this|about)|never ?mind|nvm\b|not (that|this)\b|instead\b|"
    r"actually,? (no|forget|stop|don'?t|scrap|drop|change|use|do))"
    r"|(\binstead of\b|\bforget (it|that|this|about)\b|\bscrap (that|it|this)\b|"
    r"\bcancel that\b|\bchange of plan\b|\bnot what i (meant|want|asked)\b|"
    r"\bdrop (that|it|the)\b|\bstart over\b)",
    re.I)

# Augment = "keep going, and also…". Additive / refinement cues.
_ADD = re.compile(
    r"\b(also|and also|additionally|as well|plus\b|one more( thing)?|another( thing)?|"
    r"on top of that|in addition|besides that|don'?t forget( to)?|make sure( to| you)?|"
    r"remember to|be sure to|include|add (in|a|an|the|that|this|also)|"
    r"while you'?re at it|and (add|include|make|do|also|ensure))\b",
    re.I)


def classify_interjection(text: str) -> str:
    """How a follow-up sent mid-turn relates to the work already running:
    'status' (just asking for progress), 'augment' (fold in, keep working),
    'redirect' (stop — it's wrong now), or 'ambiguous' (caller decides)."""
    t = (text or "").strip()
    if not t:
        return "augment"
    if is_status_ask(t):
        return "status"
    # Redirect wins over add: "no, also do X" is still fundamentally a redirect.
    if _REDIRECT.search(t):
        return "redirect"
    if _ADD.search(t):
        return "augment"
    return "ambiguous"


async def resolve_interjection(text: str, model_name: str = "") -> str:
    """classify_interjection, but settle 'ambiguous' on whichever brain this
    conversation is set to (the UI picker decides — copilot, claude, local, …).

    Stays 'ambiguous' when nothing can decide. That used to default to 'augment',
    which silently glued the message onto the running instruction — a plain "Hi"
    got answered "adding that to what I'm doing" instead of a reply. Unresolved
    messages are queued as their own turn instead: the live work is still never
    discarded, and nothing the user typed is swallowed.
    """
    verdict = classify_interjection(text)
    if verdict != "ambiguous":
        return verdict
    from . import agent as agent_mod
    return (await agent_mod.quick_intent(text, model_name)) or "ambiguous"


def _session_progress(session_id: str) -> dict:
    """Model turns / tool runs so far, straight out of Copilot's event log."""
    f = SESSION_DIR / session_id / "events.jsonl"
    if not f.is_file():
        return {}
    turns = tools = subagents = 0
    last_text = ""
    try:
        with f.open() as fh:
            for line in fh:
                try:
                    e = json.loads(line)
                except ValueError:
                    continue
                t = e.get("type", "")
                if t == "assistant.turn_start":
                    turns += 1
                elif t == "tool.execution_start":
                    tools += 1
                elif t == "subagent.started":
                    subagents += 1
                elif t == "assistant.message":
                    txt = ((e.get("data") or {}).get("content") or "").strip()
                    if txt:
                        last_text = txt
    except OSError:
        return {}
    return {"model_turns": turns, "tool_runs": tools, "subagents": subagents,
            "last_text": last_text[:200]}


def _mins(since: float | None) -> str:
    if not since:
        return "?"
    m = (time.time() - since) / 60.0
    if m < 1:
        return f"{int(m * 60)}s"
    if m < 90:
        return f"{m:.1f} min"
    # The recap spans a whole day, where "743.2 min" is unreadable.
    return f"{m / 60:.1f}h"


def snapshot() -> dict:
    from . import tasks as tasks_mod
    running = []
    for t in store.list_tasks():
        if t["status"] != "running":
            continue
        running.append({
            "id": t["id"],
            "title": t["title"],
            "kind": t["kind"],
            "workspace": t["workspace"],
            "elapsed": _mins(t["started_at"] or t["created_at"]),
            "started": bool(t["started_at"]),
            "alive": tasks_mod.is_running(t["id"]),
        })
    turns = []
    for conv in store.list_conversations():
        sid = store.kv_get(f"copilot_session:{conv['id']}")
        if not sid:
            continue
        # "What's the update?" means NOW. Without this, a chat last touched two
        # days ago was reported as current activity — with its lifetime counters
        # (199 model turns, 333 tool runs) reading like live progress.
        f = SESSION_DIR / sid.strip('"') / "events.jsonl"
        try:
            if time.time() - f.stat().st_mtime > RECENT_SECONDS:
                continue
        except OSError:
            continue
        prog = _session_progress(sid.strip('"'))
        if prog:
            turns.append({"conversation": conv["title"][:50], **prog})

    # Work that finished while he was away. Each of these already pushed a
    # notification when it landed, so the recap says so rather than reading
    # like news — it's "here's what you already got pinged about", not a resend.
    now = time.time()
    finished = []
    for t in store.list_tasks():
        fin = t["finished_at"]
        if not fin or t["status"] == "running" or now - fin > RECAP_SECONDS:
            continue
        finished.append({
            "id": t["id"], "title": t["title"], "status": t["status"],
            "kind": t["kind"], "ago": _mins(fin),
        })
    finished.sort(key=lambda x: x["id"], reverse=True)
    return {"tasks_running": running, "sessions": turns, "finished": finished}


def _recap_lines(snap: dict) -> list[str]:
    """What finished while he was away — already-notified work, restated."""
    if not snap["finished"]:
        return []
    hrs = RECAP_SECONDS // 3600
    out = [f"\n🗂 Finished in the last {hrs}h (already pinged to you):"]
    for t in snap["finished"][:8]:
        icon = _DONE_ICON.get(t["status"], "•")
        out.append(f"{icon} #{t['id']} {t['title'][:55]} — {t['status']}, {t['ago']} ago")
    if len(snap["finished"]) > 8:
        out.append(f"  …and {len(snap['finished']) - 8} more")
    return out


def summary() -> str:
    snap = snapshot()
    if not snap["tasks_running"] and not snap["sessions"]:
        head = "📊 Nothing running right now — no background tasks, no active chat work."
        recap = _recap_lines(snap)
        # Being away for hours is the normal case, so an idle "nothing running"
        # with no recap used to read as "nothing happened" — which was wrong.
        return head + ("\n" + "\n".join(recap) if recap else "")
    lines: list[str] = []
    for t in snap["tasks_running"]:
        state = "running" if t["started"] else "QUEUED (waiting for the workspace lock)"
        dead = "" if t["alive"] or not t["started"] else "  ⚠️ no live worker — orphaned"
        lines.append(f"• Task #{t['id']} — {t['title'][:60]}\n"
                     f"  {t['kind']} in {t['workspace'] or 'asta'}, {state}, {t['elapsed']}{dead}")
    if not lines:
        lines.append("• No background tasks running.")
    busiest = sorted(snap["sessions"], key=lambda s: s["tool_runs"], reverse=True)[:2]
    for s in busiest:
        if not s["tool_runs"]:
            continue
        extra = f", {s['subagents']} sub-agents" if s["subagents"] else ""
        lines.append(f"• Chat “{s['conversation']}” — {s['model_turns']} model turns, "
                     f"{s['tool_runs']} tool runs{extra}")
        if s["last_text"]:
            lines.append(f"  latest: {s['last_text']}")
    lines += _recap_lines(snap)
    return "📊 Right now:\n" + "\n".join(lines) + "\n\n(local status — no tokens used)"
