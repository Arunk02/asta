"""Someone asked for something. Go and find out, before telling him.

The inbound pipeline was all sensor and no actuator:

    read → triage.classify → attention.rank → attention.consider → notify

`triage` already decides that somebody wants a move from him. Then it writes one
line and stops. So Vinish asking whether production Temporal bookings are stuck
produced a notification and nothing else — and Arun, in his words, "doesn't seen
or not i'm not aware". The same shape as the 26 August Teams outage, where every
path ended in "tell Arun" and Arun was asleep.

This is the deciding layer. It reads what the message actually asks to be checked,
goes and checks it, and puts the answer in front of him with the ask.

**Auto-analyse, never auto-reply.** The whole design rests on the asymmetry
`sandbox` already states: reading Temporal, a PR, or a dashboard changes nothing
and can run unprompted; a message to a colleague cannot be taken back and is
staged for his yes like every other outward act. So this spawns `analysis` tasks
only — never `code`, never a send.

**Presence gates the telling, not the working.** `notify` suppresses ambient
pushes while he is at the laptop, which is right. It must never suppress the
investigation: "it should always on someone pings anything whether im online or
not". Nothing here consults presence.

**It decides what to ask; `tasks` does the work.** No second execution path — the
analysis runs on the same read-only parallel engine a delegated question uses, so
everything already true of that (timeouts, quota failover, reporting) stays true.
"""

from __future__ import annotations

import os
import re

from . import attention, store

#: Off by default, like every other behaviour that spends money on his behalf.
#: One flag, read at call time so a restart is not needed to turn it off.
def enabled() -> bool:
    return os.environ.get("ASTA_RESPOND", "").strip() not in ("", "0", "false", "no")


#: Analyses started per hour, at most. A burst of ten people pinging must not
#: become ten agentic investigations — that is a bill and a thundering herd, not
#: attentiveness. Older than the window and the count resets.
MAX_PER_HOUR = int(os.environ.get("ASTA_RESPOND_MAX_PER_HOUR", "4"))

#: Only asks that actually matter get investigated. P_FYI and below are things he
#: was copied on; spending a full agentic turn on each is the noise he already
#: complained about, wearing a different hat.
MAX_PRIORITY = attention.P_TODAY

_RATE_KEY = "responder_recent"
_DONE_KEY = "responder_done"


# --- what is being asked ------------------------------------------------------
# Three shapes, because these are the three he named. Each maps to a question a
# read-only worker can actually answer, which is the test for belonging here: if
# there is no way to check it without changing something, it is not for this.

#: Production is misbehaving. "check the production temporal bookings struck".
_INCIDENT = re.compile(
    r"\b(prod|production|live)\b.{0,40}\b(stuck|struck|down|failing|failed|broken|"
    r"hung|stale|not\s+(?:moving|working|processing|running)|piling|backlog)\b"
    r"|\b(stuck|struck|hung|stale|backlog|piling\s+up)\b.{0,40}\b(booking|workflow|"
    r"activity|queue|job|task|order|shipment|message)s?\b"
    r"|\b(temporal|grafana|loki|kafka)\b.{0,40}\b(stuck|struck|down|error|fail|"
    r"spike|alert|lag|retry|retries)\w*"
    r"|\b(incident|outage|sev\s*[12]|p[12]\b)",
    re.I)

#: Feedback on a pull request. He wants the points verified, not accepted.
#:
#: The URL form is not an extra — it is the common one. Against his real activity
#: feed the word-form matched Vinish's "comments on PR 1409" and missed both rows
#: that actually mattered: "hi vinish arunkumar https github com … /pull/1409" and
#: "please review https …". Teams strips the punctuation out of links in the feed
#: rendering, so `pull/1409` arrives as `pull 1409` — matched either way here.
_PR = re.compile(
    r"\b(?:pr|pull\s*request|mr|merge\s*request)\s*#?\s*(\d{2,6})\b"
    r"|/?\bpull[/\s]+(\d{2,6})\b"
    r"|\bpullrequest[/\s]+(\d{2,6})\b"
    r"|\b(?:pr|pull\s*request)\b.{0,30}\b(?:comment|review|feedback|remark|"
    r"raised|blocking|nit)\w*"
    r"|\b(?:comment|review|feedback)\w*\b.{0,30}\b(?:pr|pull\s*request)\s*#?\s*(\d{2,6})?",
    re.I)

#: "please review <link>" with no PR number rendered — still a review ask.
_REVIEW_ASK = re.compile(
    r"\b(?:please|pls|kindly|can\s+you|could\s+you)\b.{0,24}\breview\b"
    r"|\breview\s+(?:this|these|my|the)\b.{0,20}\b(?:pr|change|code|branch)\b",
    re.I)

#: How an Activity row names the person, so a title reads "Vinish asked: …" and not
#: "vinish kumar mentioned you arunkumar could you please…". The feed renders two
#: shapes ("<name> mentioned you <text>" and "<name> mentioned you — <text> — …"),
#: so the split is on the marker verb rather than on punctuation.
_ROW_MARKER = re.compile(
    r"\s+(?:mentioned\s+(?:you|everyone|\w+)|reacted\s+to|replied\s+to|"
    r"invited\s+you|missed\s+call|sent\s+a\s+message)\b", re.I)


def message_of(raw: str) -> str:
    """Just what was said, with the Activity row's own preamble removed.

    The feed renders "<name> mentioned you <the actual message>". Left in, the
    title reads "vinish kumar asked: vinish kumar mentioned you arunkumar could
    you…" and the worker's brief quotes Teams' chrome back at it as if it were
    the message.
    """
    m = _ROW_MARKER.search(raw or "")
    if not m:
        return (raw or "").strip()
    rest = (raw[m.end():] or "").strip(" —-:\t")
    return rest or (raw or "").strip()


def asker_from(raw: str, fallback: str = "") -> str:
    """The person's name out of an Activity row; `fallback` when it is not one."""
    m = _ROW_MARKER.search(raw or "")
    if not m or m.start() == 0:
        return (fallback or raw or "Someone").strip()[:40] or "Someone"
    return (raw[:m.start()].strip() or fallback or "Someone")[:40]

#: An explicit request to go and look at something.
_DEBUG = re.compile(
    r"\b(?:can|could|would|will)\s+(?:you|u|someone|somebody|anyone)\b.{0,30}"
    r"\b(check|look|verify|confirm|investigate|debug|analyse|analyze|see|find)\b"
    r"|\b(?:please|pls|plz|kindly)\b.{0,20}\b(check|look|verify|confirm|"
    r"investigate|debug|analyse|analyze)\b"
    r"|\b(?:any\s+idea|do\s+you\s+know)\b.{0,20}\bwhy\b"
    r"|\b(?:check|look\s+into|investigate|debug|analyse|analyze)\b.{0,30}"
    r"\b(?:issue|error|failure|bug|problem|why)\b",
    re.I)


def what_it_asks(text: str) -> str:
    """'incident' | 'pr_review' | 'debug' | '' — what a worker could go and check.

    Order matters and is not alphabetical. A production incident mentioned inside
    a PR discussion is still a production incident, and it is the more urgent
    reading; being wrong the other way costs him an outage he was told about in
    the wrong words.
    """
    blob = text or ""
    if _INCIDENT.search(blob):
        return "incident"
    if _PR.search(blob) or _REVIEW_ASK.search(blob):
        return "pr_review"
    if _DEBUG.search(blob):
        return "debug"
    return ""


def pr_number(text: str) -> str:
    m = _PR.search(text or "")
    if not m:
        return ""
    return next((g for g in m.groups() if g), "")


# --- the brief ----------------------------------------------------------------

#: The worker has no chat context — `delegate_task` says so and means it. Every
#: prompt below therefore restates the message verbatim rather than referring to
#: "the above", and every one ends the same way: report, stage, never send.
_CLOSING = (
    "\n\nWhen you have an answer: report what you FOUND, with the evidence you "
    "based it on. If a reply to {who} is warranted, draft it and stage it with "
    "prepare_to_send — never send anything yourself. If you could not determine "
    "it, say exactly what you could not reach rather than guessing."
)

_BRIEFS = {
    "incident": (
        "{who} is reporting a possible production problem on Teams:\n\n"
        "  \"{text}\"\n\n"
        "Find out whether it is actually happening RIGHT NOW. Query the real "
        "systems — Temporal for stuck or failing workflows, Grafana/Loki for "
        "errors and rates — rather than reasoning about whether it is plausible. "
        "State clearly: is it true, how many are affected, since when, and what "
        "the likely cause is."
    ),
    "pr_review": (
        "{who} has left review feedback on pull request {pr} and Arun needs to "
        "know whether it is correct before acting on it:\n\n"
        "  \"{text}\"\n\n"
        "Read the actual PR and the code it touches. Verify EACH point "
        "separately against the current code and say, per point, whether it is a "
        "real defect, already fixed, or mistaken — with the file and line that "
        "settles it. Do not assume the reviewer is right, and do not change any "
        "code."
    ),
    "debug": (
        "{who} is asking Arun to look into something on Teams:\n\n"
        "  \"{text}\"\n\n"
        "Go and find the answer using the workspace, the logs, and the running "
        "systems. Answer the question that was actually asked."
    ),
}


def brief_for(kind: str, who: str, text: str) -> str:
    """The self-contained prompt for one investigation."""
    body = _BRIEFS.get(kind, _BRIEFS["debug"])
    pr = pr_number(text)
    return (body + _CLOSING).format(who=who or "A colleague", text=message_of(text),
                                    pr=f"#{pr}" if pr else "(number not stated)")


def _gist(text: str, limit: int = 52) -> str:
    """A short, readable stub of the message — cut on a word, never mid-word.

    It goes in the title, which is the subject line of the answer he reads hours
    later. "...temporal bookings struc" reads like a bug in Asta.
    """
    flat = re.sub(r"\s+", " ", (text or "").strip())
    if len(flat) <= limit:
        return flat
    cut = flat[:limit].rsplit(" ", 1)[0]
    return (cut or flat[:limit]).rstrip(" ,.;:") + "…"


def title_for(kind: str, who: str, text: str) -> str:
    """The task title — and therefore the subject line of the answer.

    It always names the asker. The completion push reads "✅ Task #N done — <title>"
    and arrives possibly hours later, so a title that omits who asked delivers an
    answer detached from its question. "Vinish asked: are prod bookings stuck" is
    a reply he can act on; "Check production" is a puzzle.
    """
    who = (who or "Someone").strip() or "Someone"
    pr = pr_number(text)
    if kind == "pr_review":
        return f"{who}'s review on PR #{pr}: is it right?" if pr \
            else f"{who}'s PR feedback: is it right?"
    gist = _gist(message_of(text))
    if kind == "incident":
        return f"{who} asked: is that really happening in prod? — {gist}"
    return f"{who} asked: {gist}"


# --- rate limiting and de-duplication ----------------------------------------

def _recent(now: float) -> list[float]:
    import json
    raw = (store.kv_get(_RATE_KEY) or "").strip()
    try:
        stamps = [float(t) for t in json.loads(raw)] if raw else []
    except Exception:                                          # noqa: BLE001
        stamps = []
    return [t for t in stamps if now - t < 3600]


def _note_started(now: float) -> None:
    import json
    store.kv_set(_RATE_KEY, json.dumps(_recent(now) + [now]))


def already_handled(key: str) -> bool:
    """One investigation per thing asked. A caption settling over several polls,
    or the same message arriving twice, must not spawn twice."""
    import json
    raw = (store.kv_get(_DONE_KEY) or "").strip()
    try:
        done = list(json.loads(raw)) if raw else []
    except Exception:                                          # noqa: BLE001
        done = []
    return key in done


def _note_handled(key: str) -> None:
    import json
    raw = (store.kv_get(_DONE_KEY) or "").strip()
    try:
        done = list(json.loads(raw)) if raw else []
    except Exception:                                          # noqa: BLE001
        done = []
    if key not in done:
        done.append(key)
    store.kv_set(_DONE_KEY, json.dumps(done[-200:]))


def should_respond(kind: str, priority: int | None, key: str,
                   now: float | None = None) -> str:
    """"" when it should run, otherwise the reason it must not.

    Returned as a REASON rather than a bool because every one of these is a
    decision he might later ask about — "why didn't you check that one" has an
    answer, and it is written here.
    """
    import time
    now = time.time() if now is None else now
    if not enabled():
        return "responder is off (ASTA_RESPOND)"
    if not kind:
        return "nothing checkable in it"
    if priority is not None and priority > MAX_PRIORITY:
        return f"ranked p{priority} — below the bar for spending a turn"
    if already_handled(key):
        return "already investigated"
    if len(_recent(now)) >= MAX_PER_HOUR:
        return f"rate limit — {MAX_PER_HOUR} investigations already this hour"
    return ""


# --- the act ------------------------------------------------------------------

def respond(source: str, who: str, text: str, priority: int | None = None,
            key: str = "", workspace: str = "") -> dict | None:
    """Start the investigation this message deserves. The spawned task, or None.

    Deliberately synchronous and tiny: it decides and delegates. Everything slow
    happens on the worker, so an inbound-message loop never waits on a brain.
    """
    import time

    from . import tasks
    kind = what_it_asks(text)
    key = key or attention.key_for(text)
    why_not = should_respond(kind, priority, key, now=time.time())
    if why_not:
        return None
    _note_started(time.time())
    _note_handled(key)
    t = tasks.spawn(title_for(kind, who, text),
                    brief_for(kind, who, text),
                    "analysis",                     # read-only. never code.
                    workspace or None)
    store.kv_set(f"responder_task:{t['id']}",
                 f"{source}|{who}|{kind}")
    return t


def line_for(task: dict, who: str, kind: str) -> str:
    """What he reads on his phone the moment it starts.

    Names the person and the fact that it is already running, because "X is
    asking about Y" and "X is asking about Y, I'm checking" are different
    messages: the first is another thing on his list, the second is one fewer.
    """
    what = {"incident": "whether that's actually happening in prod",
            "pr_review": "whether that review is right",
            "debug": "it"}.get(kind, "it")
    return f"🔎 {who or 'Someone'} asked — I'm checking {what} now (task #{task['id']})."
