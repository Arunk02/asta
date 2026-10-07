"""Someone asked for something. Go and find out, before telling him.

The inbound pipeline was all sensor and no actuator:

    read → triage.classify → attention.rank → attention.consider → notify

`triage` already decides that somebody wants a move from him. Then it writes one
line and stops. So Alex asking whether production Temporal bookings are stuck
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
import json
import hashlib

from . import attention, store

#: Off by default, like every other behaviour that spends money on his behalf.
#: One flag, read at call time so a restart is not needed to turn it off.
def enabled() -> bool:
    return os.environ.get("ASTA_RESPOND", "1").strip() not in ("", "0", "false", "no")


#: There is no hourly cap any more (30 Sep). The thundering-herd concern it
#: answered is met by the queue: tasks.investigation_slot runs at most
#: ASTA_MAX_PARALLEL_INVESTIGATIONS at once and the rest wait, most urgent first.

#: Only asks that actually matter get investigated. P_FYI and below are things he
#: was copied on; spending a full agentic turn on each is the noise he already
#: complained about, wearing a different hat.
MAX_PRIORITY = attention.P_TODAY

#: How recent an ask has to be before it is worth investigating.
#:
#: The signal that was missing. `chat_watch` opens a thread for the first time and
#: finds the whole day in it — new to Asta, old to the world — and a background
#: task went off to check production issues Arun had already analysed and answered
#: hours earlier. He put it exactly:
#:
#:   "already for the dead old message and done work if this shares then it is
#:    worst , if it is his old work someone giving feedback then it work now
#:    makes sense"
#:
#: Both cases fall out of one test on the message's own timestamp. Feedback about
#: work he finished months ago is still fresh INPUT — it arrived just now — and is
#: acted on. A message from this morning that he has already dealt with is not,
#: however recently Asta happened to read it.
MAX_AGE_MINUTES = float(os.environ.get("ASTA_RESPOND_MAX_AGE_MIN", "90"))

_RATE_KEY = "responder_recent"
_DONE_KEY = "responder_done"


# --- what is being asked ------------------------------------------------------
# Three shapes, because these are the three he named. Each maps to a question a
# read-only worker can actually answer, which is the test for belonging here: if
# there is no way to check it without changing something, it is not for this.

#: Production is misbehaving. "check the production temporal bookings struck".
_INCIDENT = re.compile(
    # A ServiceNow incident number IS an incident, whatever words surround it —
    # "INC0012345 is open, can you check?" read as a debug ask, so muting
    # incidents did not cover the commonest way one reaches him.
    r"\bINC\d{5,}\b"
    r"|\b(prod|production|live)\b.{0,40}\b(stuck|struck|down|failing|failed|broken|"
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
#: feed the word-form matched Alex's "comments on PR 1409" and missed both rows
#: that actually mattered: "hi alex arunkumar https github com … /pull/1409" and
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

#: THEY are asking HIM to review THEIR change. The other reading — a colleague
#: leaving feedback on HIS pull request — is the one the brief used to assume for
#: both, so "please review my PR" sent Asta off to check whether the author's own
#: points about his own code were right. Whose PR it is decides the entire job.
_WANTS_MY_REVIEW = re.compile(
    r"\b(?:please|pls|kindly|can|could|would)\b[^.?!]{0,30}\breview\b"
    r"|\breview\s+(?:this|these|my|the)\b"
    r"|\b(?:my|our)\s+(?:pr|pull\s*request|mr|merge\s*request|change|branch)\b"
    r"|\braise[d]?\s+(?:a\s+)?(?:pr|pull\s*request)\b"
    r"|\b(?:pr|pull\s*request)\s+(?:is\s+)?(?:up|ready|raised|open)\b", re.I)

#: THEY have already reviewed HIS change and he needs to know if they are right.
_THEIR_FEEDBACK = re.compile(
    r"\b(?:left|added|raised|posted|put)\b[^.?!]{0,24}\b(?:comment|review|feedback|nit)"
    r"|\b(?:comment|review|feedback|nit)\w*\b[^.?!]{0,24}\bon\s+your\b"
    r"|\byour\s+(?:pr|pull\s*request|change|branch)\b"
    # "please review MY FEEDBACK on PR 1409" — the thing to look at is their
    # comments, not their code, however much it starts like a review request.
    r"|\bmy\s+(?:feedback|comments?|nits?|remarks?|review)\b"
    r"|\bi\s+(?:have\s+)?review(?:ed)?\b", re.I)

#: "please review <link>" with no PR number rendered — still a review ask.
_REVIEW_ASK = re.compile(
    r"\b(?:please|pls|kindly|can\s+you|could\s+you)\b.{0,24}\breview\b"
    r"|\breview\s+(?:this|these|my|the)\b.{0,20}\b(?:pr|change|code|branch)\b",
    re.I)

#: How an Activity row names the person, so a title reads "Alex asked: …" and not
#: "alex kumar mentioned you arunkumar could you please…". The feed renders two
#: shapes ("<name> mentioned you <text>" and "<name> mentioned you — <text> — …"),
#: so the split is on the marker verb rather than on punctuation.
_ROW_MARKER = re.compile(
    r"\s+(?:mentioned\s+(?:you|everyone|\w+)|reacted\s+to|replied\s+to|"
    r"invited\s+you|missed\s+call|sent\s+a\s+message)\b", re.I)


def message_of(raw: str) -> str:
    """Just what was said, with the Activity row's own preamble removed.

    The feed renders "<name> mentioned you <the actual message>". Left in, the
    title reads "alex kumar asked: alex kumar mentioned you arunkumar could
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


#: Text that is not the sender's words — a URL. Stripped before deciding what an
#: ask IS, because a link is a reference and not a description of the problem.
#: "https://github.com/example-dev/incident-copilot" was classified as a
#: production INCIDENT and queued an approval request, on the strength of the word
#: "incident" inside a repository name.
_URL_IN_TEXT = re.compile(r"https?://\S+")

#: One definition, in the ranking policy — see `attention.is_broadcast`. These
#: names stay because callers and tests already use them, and because "is this a
#: broadcast" must give the same answer to the investigator and to the ranker.
from_bulk_sender = attention.from_bulk_sender
is_broadcast = attention.is_broadcast


#: "Use this and work from booking side <AP PR link>": the PR is the REFERENCE,
#: and the ask is the same change on another service. 7 Oct, Vinish and AP PR
#: 1260 (job reopen): Asta read it as a review, then as "check booking consumes
#: it", told him twice there was nothing to do, and started three reviews.
_COUNTERPART = re.compile(
    r"\b(?:work|do|implement|build|handle|add|make|replicate|port|mirror|copy|"
    r"apply|bring|follow)\b[^.?!\n]{0,40}\b(?:from|on|in|at|for|to)\s+(?:the\s+)?"
    r"(?P<side>[\w-]+)\s+(?:side|service|repo|module|end)\b"
    r"|\b(?:same|similar)\s+(?:change|changes|logic|thing|way|handling|fix)?\s*"
    r"(?:in|on|for)\s+(?:the\s+)?(?P<side2>[\w-]+)\b"
    r"|\b(?:use|take)\s+(?:this|it|that)\s+(?:as\s+(?:a\s+)?reference|and\s+"
    r"(?:work|do|implement|build|make))\b"
    r"|\b(?:replicate|port|mirror)\s+(?:this|it|that|the\s+(?:change|logic|same))\b",
    re.I)


_SIDE = re.compile(r"\b(?:from|on|in|at|for|to)\s+(?:the\s+)?([\w-]+)\s+"
                   r"(?:side|service|repo|module|end)\b", re.I)


def counterpart(text: str) -> dict | None:
    """{pr, side} when a message hands over a PR as the reference for the same
    change elsewhere — work to plan, not a PR to review."""
    from . import review
    link = review._PR_LINK.search(text or "")
    if not link:
        return None
    blob = _URL_IN_TEXT.sub(" ", text or "")
    found = list(_COUNTERPART.finditer(blob))
    if not found or _THEIR_FEEDBACK.search(blob) or re.search(r"\breview\b", blob, re.I):
        return None
    owner = link.group(1) or link.group(4)
    repo = link.group(2) or link.group(5)
    number = link.group(3) or link.group(6)
    sides = [x.lower() for x in _SIDE.findall(blob)]
    sides += [(m.group("side") or m.group("side2") or "").lower() for m in found]
    side = next((x for x in sides if x and x not in ("this", "that", "it", "the")), "")
    return {"pr": f"https://github.com/{owner}/{repo}/pull/{number}", "repo": repo,
            "number": number, "side": side}


#: How to carry a change across a service boundary. Shared by the plan offer
#: and the investigation brief, so both work the same way.
COUNTERPART_METHOD = (
    "Method — the referenced PR is the other side of the contract:\n"
    "1. Read the reference PR as a CONTRACT change: every value that crosses the "
    "service boundary and is new or changed (status, workProcessName/eventName, "
    "enum value, topic, payload field, API), each with file:line.\n"
    "2. For each, find its closest EXISTING sibling (a new JOB_OPENED → the "
    "existing JOB_CLOSURE) and trace how the target service handles the sibling "
    "end to end: enums and mappers, the consumer/feedback branch, milestones, "
    "event history, status updates, tests. Search the target repo for the "
    "sibling's name, not only for the new one.\n"
    "3. The work is the difference: everywhere the sibling is handled explicitly "
    "and the new value is not. \"Falls through a generic path\" counts only if "
    "the sibling takes that same path — show it in code.\n"
    "4. Where a lower environment has it, confirm from the logs how the sibling "
    "arrives and what the target does with it.\n"
    "Name each file to change and the sibling line it mirrors."
)


def what_it_asks(text: str) -> str:
    """'incident' | 'port' | 'pr_review' | 'review_request' | 'debug' | 'ask' | ''.

    `ask` is the catch-all, and it is the important one. The first version
    recognised three shapes and shrugged at everything else, which meant a
    colleague asking anything slightly differently worded got the old behaviour —
    a notification and nothing more. "not just incident, PR feedback , debug any
    kind of stuff". If somebody is asking for something, it is worth finding out.

    Order matters and is not alphabetical. A production incident mentioned inside
    a PR discussion is still a production incident, and it is the more urgent
    reading; being wrong the other way costs him an outage he was told about in
    the wrong words.
    """
    # Judged on what they WROTE, not on what a link happens to be called.
    blob = _URL_IN_TEXT.sub(" ", text or "")
    if _INCIDENT.search(blob):
        return "incident"
    if counterpart(text or ""):
        return "port"
    if _PR.search(blob) or _REVIEW_ASK.search(blob):
        # Their feedback on his change, or their change wanting his eyes. The
        # words decide here; the review itself confirms from the PR's author.
        if _THEIR_FEEDBACK.search(blob):
            return "pr_review"
        return "review_request" if _WANTS_MY_REVIEW.search(blob) else "pr_review"
    if _DEBUG.search(blob):
        return "debug"
    # A question about one booking, id in hand — "does manual customs reach
    # billing for booking MH65W8JZNVNT" — is checked in the logs. It used to read
    # as "nothing checkable": no investigation, one line on his phone (2 Oct).
    from . import booking_case
    if booking_case.ids(blob) and booking_case.asks(blob):
        return "debug"
    # Anything triage reads as a genuine ask. One detector for "is this an ask",
    # shared with the notification path, rather than a second opinion here that
    # could disagree with what he was told.
    from . import triage
    if triage.classify("", blob).action:
        return "ask"
    return ""


def pr_number(text: str) -> str:
    """The PR number named anywhere in the message, or ''.

    Every match, not the first: "please review my PR <link>" matches the
    word-shaped alternative — which carries no number — before it reaches the
    link that has one, and the first match alone answered "" for the most common
    review request there is. The link parser settles anything left over.
    """
    for m in _PR.finditer(text or ""):
        found = next((g for g in m.groups() if g), "")
        if found:
            return found
    from . import review
    number, target = review.pr_target(text or "")
    return number if target and number.isdigit() else ""


def review_scope(text: str) -> str:
    """Keep extra review questions separate from an ordinary whole-PR review."""
    from . import review
    without_links = review._PR_LINK.sub(" ", text or "")
    without_numbers = re.sub(r"\b(?:pr|pull\s+request)\s*#?\s*\d+\b",
                             " ", without_links, flags=re.I)
    routine = {"hi", "hello", "hey", "arun", "arunkumar", "please", "pls",
               "can", "could", "would", "you", "your", "i", "me", "my", "our",
               "the", "this", "it", "a", "an", "for", "if", "all", "good",
               "review", "check", "look", "at", "approve", "approval", "pr",
               "pull", "request", "code", "thanks", "thank", "and", "once",
               "ready", "is", "up"}
    return "|".join(sorted(set(re.findall(r"[a-z]{2,}", without_numbers.lower()))
                           - routine))


# --- the brief ----------------------------------------------------------------

#: The worker has no chat context — `delegate_task` says so and means it. Every
#: prompt below therefore restates the message verbatim rather than referring to
#: "the above", and every one ends the same way: report, stage, never send.
#: The query discipline lives in a skill, and the HARD RULE that loads it lives
#: in the CHAT persona — which a spawned worker never sees. So every rule Arun
#: had already written was invisible to the investigations that needed it most:
#: task #96 queried a single service instead of the namespace, and left "is the
#: country in disable-countries-to-billing?" open as out of scope when the answer
#: was one grep of a prod-values.yml already in its own worktree.
_HOW = (
    "\n\nBEFORE touching Grafana or Temporal, call load_skill('grafana-analyser') "
    "and follow it exactly — namespace-wide first (the prod namespace is in your "
    "guardrails), ONE wide call for an identifier — namespace ONLY, never a "
    "container matcher — then "
    "reason from what came back instead of "
    "querying again. The environment they named; when none was named, every "
    "environment — never assume production.\n"
    "EVERY service in that namespace is in scope, not just the one the question "
    "names — reading logs changes nothing, so 'that service is outside this "
    "read-only pass' is never a reason to stop. The evidence for 'did it "
    "actually land?' is almost always in the service DOWNSTREAM of the one "
    "being asked about.\n"
    "A completed milestone proves only that its own step advanced. For any "
    "dispatch, delivery or integration claim, follow the same attempt across "
    "all involved services to the receiving system's outcome. Report earlier "
    "failures, retries and eventual success separately; do not substitute a "
    "later milestone for proof that an earlier reported failure never occurred.\n"
    "For a case ID, leave the service filter blank: the log tool searches every "
    "service in the requested namespace and follows booking/order IDs paired "
    "in the returned logs. Keep errorCode separate from orderId: a numeric "
    "error code is NOT an order identifier. Do not transplant UAT evidence into "
    "prod, or conclude from zero matching lines that a record does not exist.\n"
    "If an incident time is known, center grafana_logs(at=ISO-time-with-zone, "
    "minutes=10-30) on it. If a wide query hits its 2000-line limit, narrow "
    "to the incident before declaring an event absent.\n"
    "For 'no retry' or other absence claims, give the actual start and end "
    "times searched, environment and services covered. A lookback window "
    "does not prove that no retry occurred for days AFTER a recent failure; "
    "limit the claim to the time observed.\n"
    "If this task already includes log evidence or a prior case finding, use "
    "it before querying. Fetch only an uncovered service, time window or "
    "attempt; never repeat the same identifier/environment query just to "
    "rediscover what is in the brief.\n"
    "The logs decide and the code explains: establish from the trail what "
    "actually happened, then use the code to say why. A cause read out of the "
    "code and never confirmed in logs is a hypothesis — label it as one.\n"
    "Answer plainly: the observed failure, service and time first; then its "
    "verified cause or what is still unknown. Do not list every service or "
    "speculate about unseen services unless the question asks for that detail.\n"
    "For runtime configuration, read the deployed values for the named "
    "environment, not prod's for a UAT case. A key absent there uses the "
    "application.yml default (`${{VAR:default}}`); verify before concluding."
)

_CLOSING = _HOW + (
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
    "review_request": (
        "{who} is asking Arun to REVIEW their pull request {pr}:\n\n"
        "  \"{text}\"\n\n"
        "This is their code, not his — review it as a senior engineer on the team. "
        "Call `review_pr` with the link or number they gave (a link needs no local "
        "clone) to get the PR, its diff, its CI and the project context, then judge "
        "it: correctness first, then data loss, security, breaking changes and "
        "unhandled failure paths. Every point names a `path:line`.\n\n"
        "Finish by calling `propose_pr_review` with your notes — that stages one "
        "GitHub review with a comment on each line, for Arun's yes. Do not change "
        "any code, and do not approve anything yourself. In the REPLY section, "
        "write a neutral, addressee-free answer about this PR that Arun could "
        "send unchanged to anyone who asked the same question."
    ),
    "debug": (
        "{who} is asking Arun to look into something on Teams:\n\n"
        "  \"{text}\"\n\n"
        "Go and find the answer using the workspace, the logs, and the running "
        "systems. Answer the question that was actually asked."
    ),
    "port": (
        "{who} is handing Arun a pull request as the REFERENCE for the same change "
        "on another service:\n\n"
        "  \"{text}\"\n\n"
        "This is not a review of that PR and not a question about it. Work out "
        "what the target service needs so it handles what the reference PR now "
        "sends or expects.\n\n" + COUNTERPART_METHOD + "\n\n"
        "Read only: do not change code. The ANALYSIS is the gap list for Arun "
        "(file:line in the target, the sibling line it mirrors). The REPLY "
        "confirms the scope you understood in one or two plain lines — what "
        "arrives, which existing case it mirrors, where it goes — without "
        "promising when it is done."
    ),
    "ask": (
        "{who} is asking Arun for something on Teams:\n\n"
        "  \"{text}\"\n\n"
        "Work out what they actually need and get it, using the workspace, the "
        "logs, Jira and the running systems. If the ask is ambiguous, say which "
        "readings are possible rather than picking one and answering confidently."
    ),
}


def brief_for(kind: str, who: str, text: str) -> str:
    """The self-contained prompt for one investigation.

    The kind says what the job IS; the role says who does it. The kind is passed
    through rather than letting the role module re-read the message, because a
    second classifier reaching a different answer about the same sentence is how
    two halves of Asta come to disagree.
    """
    from . import roles
    body = _BRIEFS.get(kind, _BRIEFS["debug"])
    pr = pr_number(text)
    hat = roles.brief(roles.role_for(text, kind=kind))
    out = (body + _CLOSING).format(who=who or "A colleague", text=message_of(text),
                                   pr=f"#{pr}" if pr else "(number not stated)")
    return f"{hat}\n\n{out}" if hat else out


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
    answer detached from its question. "Alex asked: are prod bookings stuck" is
    a reply he can act on; "Check production" is a puzzle.
    """
    who = (who or "Someone").strip() or "Someone"
    pr = pr_number(text)
    if kind == "pr_review":
        return f"{who}'s review on PR #{pr}: is it right?" if pr \
            else f"{who}'s PR feedback: is it right?"
    if kind == "review_request":
        return f"Review {who}'s PR #{pr}" if pr else f"Review {who}'s pull request"
    if kind == "port":
        from . import prname
        found = counterpart(text) or {}
        side = f" to {found['side']}" if found.get("side") else ""
        named = prname.from_url(found.get("pr", "")) or f"PR #{found.get('number', pr)}"
        return f"{who} asked: carry {named}{side}"
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


def too_old(sent_at: float | None, now: float | None = None) -> bool:
    """Was this said long enough ago that acting on it now is noise?

    Unknown timestamps are treated as fresh. Refusing everything Teams did not
    render a machine-readable time for would silently drop real asks, and a
    missing timestamp is a scraping gap rather than evidence of age.
    """
    import time
    if not sent_at:
        return False
    return ((now or time.time()) - sent_at) > MAX_AGE_MINUTES * 60


#: Kinds he has told Asta to leave alone. A standing instruction, kept in the
#: database rather than in a prompt, because "don't look into incidents" said
#: once in chat has to still be true tomorrow — and it was not. He said it, the
#: incidents kept being investigated, and his only recourse was to say it again.
_MUTE_KEY = "responder_muted_kinds"


def muted_kinds() -> set[str]:
    return {k for k in (store.kv_get(_MUTE_KEY) or "").split(",") if k}


def muted(kind: str) -> bool:
    return (kind or "") in muted_kinds()


def mute(kind: str) -> str:
    """Stop investigating a kind of ask until he says otherwise."""
    kinds = muted_kinds() | {kind}
    store.kv_set(_MUTE_KEY, ",".join(sorted(kinds)))
    return f"I'll stop investigating {kind} asks. Say 'investigate {kind}s again' to undo."


def unmute(kind: str) -> str:
    kinds = muted_kinds() - {kind}
    store.kv_set(_MUTE_KEY, ",".join(sorted(kinds)))
    return f"Investigating {kind} asks again."


def should_respond(kind: str, priority: int | None, key: str,
                   now: float | None = None, broadcast: bool = False,
                   sent_at: float | None = None) -> str:
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
    if muted(kind):
        return f"he asked me not to investigate {kind} asks"
    from . import policy
    ruled = policy.check("investigate", kind)
    if not ruled.ok:
        return ruled.why
    if broadcast:
        return "addressed to a room, not to him"
    if too_old(sent_at, now):
        return f"said more than {MAX_AGE_MINUTES:.0f} min ago — not a live ask"
    if priority is not None and priority > MAX_PRIORITY:
        return f"ranked p{priority} — below the bar for spending a turn"
    if already_handled(key):
        return "already investigated"
    # No hourly cap. "5 people pinging at a time — you do only 2 now and the
    # rest after hours? No, this is not right" (30 Sep). Every real ask is
    # worked; how many run AT ONCE is the queue's job (tasks.investigation_slot),
    # and duplicates are stopped above.
    return ""


# --- has he worked on this before? -------------------------------------------
#
# "if it related to already he worked he can directly act on and notify me , if it
# is new related ask me do you want me to work on , can i analyse once approved".
#
# The line is not "how confident am I" — it is whether the thing being asked about
# is already in Asta's record of his work. That is checkable rather than guessed:
# a PR number, a Jira key or a repo it has run a task against is territory he has
# been in, and acting there is continuing something. Anything else is a new thread
# of work, and starting one unasked is the substitution failure in another costume.

_IDENT = re.compile(r"\b[A-Z][A-Z0-9]{1,9}-\d+\b")


#: A handle somebody has HANDED OVER — a concrete thing Asta can go and look up.
#: Deliberately narrow: a ticket number, a link into one of his systems, a Jira
#: key. Not a bare alphanumeric blob, which would fire on half of ordinary chat.
_HANDLE = re.compile(
    r"\b(?:INC|CHG|REQ|RITM|TASK)\d{4,}\b"                     # ServiceNow
    r"|\bhttps?://[\w.-]*(?:maersk|github)[\w.-]*/\S{4,}"      # a real link
    # An internal host and a path, however Teams chose to render the gap between
    # them — its link previews turn "host/path" into "host: path", which is what
    # Alex's booking link actually arrived as.
    r"|\b[\w.-]*maersk[\w.-]*\.(?:net|io|com|dev)\b[\s:]{0,3}\S*/\S{3,}"
    r"|\b[A-Z][A-Z0-9]{1,9}-\d+\b"                             # Jira key
    # A pull request someone points at IS the thing to look at, in every shape it
    # arrives: a link, owner/repo#N, or the punctuation-stripped rendering Teams
    # produces. Without this a colleague sending "please review my PR <link>"
    # was new ground, so Asta asked for permission to read a PR it had been
    # handed — which is the shape of not answering.
    r"|(?:github[.\s]+com[/\s]+[\w.-]+[/\s]+[\w.-]+[/\s]+pull[/\s]+\d{1,7})"
    r"|\b[\w.-]+/[\w.-]+#\d{1,7}\b"
    r"|\b(?:pr|pull\s*request)\s*#?\s*\d{2,6}\b", re.I)


def handed_over(text: str) -> str:
    """The concrete thing this message points at, or ''.

    The reason this exists: `familiar` asks "has Asta worked on this before?",
    which is the right question for deciding whether to spend a turn on a vague
    remark — and the wrong one when a colleague has just pasted the exact thing
    to look at. A colleague sent a booking link and "can you check why STF is not
    done?", and because that booking id was new ground Asta asked Arun for
    permission to look instead of looking. His words: "they gave tickets and
    details to check but it is not automatically going and debugging".

    A handle IS the permission. It is somebody saying "here, this one" — and the
    work it unlocks is read-only, so the cost of being wrong is one wasted
    analysis rather than anything that touches production.
    """
    m = _HANDLE.search(text or "")
    return m.group(0)[:80] if m else ""


def _identifiers(text: str) -> set[str]:
    """Tickets, PR numbers and repo names named in a message."""
    out = {m.group(0).upper() for m in _IDENT.finditer(text or "")}
    pr = pr_number(text)
    if pr:
        out.add(f"PR#{pr}")
    return out


def _worked_on() -> set[str]:
    """Identifiers Asta has already run work against, from its own task record."""
    out: set[str] = set()
    try:
        rows = store.list_tasks(200)
    except Exception:                                          # noqa: BLE001
        return out
    for t in rows:
        blob = f"{t.get('title', '')} {t.get('prompt', '')}"
        out |= _identifiers(blob)
        ws = (t.get("workspace") or "").strip().lower()
        if ws:
            out.add(f"WS:{ws}")
    return out


def familiar(text: str) -> tuple[bool, str]:
    """(is this a continuation of his work, why). Empty reason when it is new."""
    named = _identifiers(text)
    known = _worked_on()
    hit = named & known
    if hit:
        return True, "already worked on " + ", ".join(sorted(hit)[:3])
    low = (text or "").lower()
    for key in (k for k in known if k.startswith("WS:")):
        name = key[3:]
        if len(name) > 3 and name in low:
            return True, f"in the {name} workspace"
    return False, ""


# --- the act ------------------------------------------------------------------

#: A colleague waiting on an answer gets it looked into (read-only) without a
#: "want me to look into it?" first. Module constant; tests may set it.
LOOK_FOR_WAITING_COLLEAGUE = True


def respond(source: str, who: str, text: str, priority: int | None = None,
            key: str = "", workspace: str = "", sent_at: float | None = None,
            context: str = "", reply_to: str = "", group: bool = False,
            need: str = "", thread: str = "", questions: list | None = None,
            review_revision: str = "", review_question: str = "",
            source_text: str = "", correction_of: int | None = None,
            kind_override: str = "") -> dict | None:
    """Start the investigation this message deserves. The spawned task, or None.

    Deliberately synchronous and tiny: it decides and delegates. Everything slow
    happens on the worker, so an inbound-message loop never waits on a brain.
    """
    import time

    from . import steward, tasks
    if group and reply_to:
        from . import chat_watch
        if not chat_watch.mentions_him(source_text):
            store.record_outcome("responder", "unaddressed group", subject=who[:80],
                                 detail=(source_text or text)[:100])
            return None
    # The same door `chat_watch` uses for the push decision. Two opinions about
    # whether "Hi" is a message is how one half of Asta holds a conversation
    # while the other half investigates it.
    opening = steward.consider(who, text)
    if opening["hold"]:
        return None
    if opening["opened_with"]:
        context = f"{opening['opened_with']}\n{context}".strip()
    original = store.get_task(correction_of) if correction_of else None
    if correction_of and not original:
        raise ValueError(f"correction task #{correction_of} not found")
    if kind_override and kind_override not in _BRIEFS:
        raise ValueError(f"unknown investigation kind: {kind_override}")
    from . import answers
    kind = (answers._meta(correction_of).get("ask_kind") or kind_override
            or what_it_asks(text) or "ask") if original else (
                kind_override or what_it_asks(text))
    if original and kind != "port" and counterpart(
            answers._meta(correction_of).get("source_text") or ""):
        # The first reading was the wrong JOB, not a wrong detail: the request
        # handed over a reference PR. Rechecking the same reading again is how
        # Vinish was told "nothing to do" twice (7 Oct).
        kind = "port"
    key = key or attention.key_for(text)
    why_not = should_respond(kind, priority, key, now=time.time(),
                             broadcast=is_broadcast(who, text), sent_at=sent_at)
    if why_not:
        # Written down, not dropped. should_respond returns a REASON so that "why
        # didn't you check that one?" has an answer — and on 29 Sep, a day with no
        # investigation at all, there was nowhere to read one.
        store.record_outcome("responder", "skipped", subject=(who or "")[:80],
                             detail=f"{why_not} — {(text or '')[:100]}")
        return None
    # The ASK is judged on the message itself — context must not be able to
    # invent a question nobody asked. Everything else reads the surrounding
    # lines, because people paste the link and then ask about it in the next
    # breath: Alex's booking id was in the message BEFORE "can you check why
    # STF not done?", so the one line handed over here had a question and
    # nothing to check it against.
    grounds = f"{context}\n{text}".strip() if context else text
    known, why = familiar(grounds)
    if not known and reply_to and not is_broadcast(who, grounds) and LOOK_FOR_WAITING_COLLEAGUE:
        # A colleague is waiting on it, and looking is read-only. "Want me to
        # look into it?" was one more approval for work that changes nothing —
        # his words, 30 Sep: "getting too much approval is drag". The offer
        # stays for what nobody is waiting on (feeds, mail, broadcasts).
        known, why = True, "a colleague is waiting on it"
    if not known:
        # A handle is permission only when a PERSON handed it over. "IT Service
        # Desk" mails "Incident INC… has been assigned to group OH - TELIKOS" all
        # day; every one carries a ticket number, and reading that as somebody
        # saying "here, this one" turned a ticket feed into a queue of agentic
        # investigations — six in 24 hours, every one of them burning his Claude
        # session limit and failing. Nobody asked for any of them.
        #
        # `handed_over` still means what it meant: the difference is WHO said it.
        handle = "" if is_broadcast(who, grounds) else handed_over(grounds)
        if handle:
            known, why = True, f"they handed over {handle}"
    _note_handled(key)
    if not known:
        # New ground. Ask before spending a turn on it — and ask in the form that
        # already works everywhere else, so his "yes" runs the analysis with the
        # same brief this would have used.
        from . import offers
        offers.propose(
            subject=f"🔎 {who or 'Someone'} asked about something new",
            context=f"{who or 'Someone'}: {message_of(grounds)[:400]}",
            question=f"Want me to look into it?",
            action=brief_for(kind, who, text),
            kind="investigate",
            payload={"who": who, "source": source, "responder_kind": kind})
        return None
    # The same question, already answered or already being answered, is not
    # investigated again: "u shouldnt do the same operation multiple times". Keyed
    # on what it is ABOUT, so two people asking about one booking in different
    # words share one investigation. See app/results_cache.py.
    from . import results_cache
    # A PR number alone collides across repositories; even a full link says
    # nothing about whether a new commit or failing CI invalidated the review.
    # Only the live-verified revision supplied by the Teams sweep is reusable.
    scope = review_scope(review_question)
    if original:
        ck = f"correction:{correction_of}:{hashlib.sha256(text.encode()).hexdigest()[:12]}"
    elif kind == "review_request":
        ck = (f"review_request:{review_revision}:"
              f"{hashlib.sha256(scope.encode()).hexdigest()[:12]}" if review_revision else "")
    else:
        ck = "" if kind == "pr_review" else results_cache.key_for(kind or "ask", grounds)
    hit = results_cache.lookup(ck) if ck else None
    if hit:
        done = hit["state"] == "done"
        if not done and reply_to:
            from . import answers
            answers.remember_waiter(hit["task_id"], who=who, need=need or message_of(text)[:160],
                                    chat=reply_to, group=group, thread=thread,
                                    source_text=source_text or text, kind=kind)
        store.record_outcome("responder", "reused" if done else "joined",
                             subject=str(hit["task_id"]), detail=f"{who}: {text[:120]}")
        return {"id": hit["task_id"], "title": title_for(kind, who, text),
                "reused": done, "joined": not done, "result": hit.get("result", ""),
                "at": hit.get("at")}
    brief = brief_for(kind, who, grounds)
    if original:
        from . import answers
        prior = answers._meta(correction_of)
        was_analysis, was_reply = answers.split(original.get("result") or "")
        disputed = (f"{was_analysis}\nREPLY they rejected: {was_reply}".strip()
                    if was_reply else (original.get("result") or "")[-1200:])
        brief += (
            f"\n\nCorrection to task #{correction_of}. They rejected the prior answer. "
            "First name the reading of their request the disputed answer took — that "
            "reading is what they rejected, so do NOT re-check it and do not offer it "
            "back to them. Start from the other readings, and test first whether they "
            "want something BUILT (for instance a referenced PR's change carried into "
            "this service) rather than reviewed or explained. Check each reading "
            "against the code and the most recent exchange; do not treat the previous "
            "conclusion as fact. If one reading survives, answer it; ask them only "
            "when the evidence leaves it genuinely open — one open question, never "
            "a choice between your own guesses.\n"
            f"Original request: {(prior.get('source_text') or '')[:500]}\n"
            f"Disputed answer: {disputed[:1500]}")
    if reply_to:
        brief += _waiting_brief(who, text, context, need=need, questions=questions or [])
    t = tasks.spawn(title_for(kind, who, text), brief,
                    "analysis",                     # read-only. never code.
                    workspace or None, teams_chat=reply_to,
                    priority=1 if kind == "incident" else (priority if priority is not None else 2))
    # Counted when a task actually starts — a duplicate stopped by the dedup
    # once used up a slot of the old cap (30 Sep: 3 of 4 were phantoms).
    _note_started(time.time())
    store.kv_set(f"responder_task:{t['id']}",
                 f"{source}|{who}|{kind}|{why}")
    if reply_to:
        from . import answers
        answers.remember_meta(t["id"], who=who, need=need or message_of(text)[:160],
                              chat=reply_to, group=group, thread=thread,
                              source_text=source_text or text, kind=kind)
    if review_revision:
        store.kv_set(f"review_origin:{t['id']}", json.dumps(
            {"ref": review_revision.rsplit("@", 1)[0], "revision": review_revision}))
    if ck:
        results_cache.start(ck, kind or "ask", t["id"])
    return t


_RUN_URL = re.compile(r"github\.com/[\w.-]+/[\w.-]+/actions/runs/\d+")
_PR_URL = re.compile(r"github\.com/[\w.-]+/[\w.-]+/pull/\d+")
_HOW_WHY = re.compile(r"\b(why|how does|how do|how is|where is|which service|flow)\b", re.I)


def playbook(text: str) -> str:
    """How a good engineer would check THIS kind of ask — steps, not a hint.

    29 Sep: "check booking H65ZMWX52B2" was searched in two namespaces, one of
    which does not exist, and answered "no trace — is the ref right?". The
    booking was in preprod with a billing error on it. The steps below make
    "which environment?" the first question an investigation answers."""
    from . import booking_case
    steps: list[str] = []
    ids = booking_case.ids(text)
    env = booking_case.env_of(text)
    if ids:
        where = (f"in {env} — grafana_logs(terms=[id], namespace=\"{env}\", errors_only=false, "
                 "minutes=4320)" if env and env != "lower" else
                 "FIRST find which environment they live in — grafana_logs(terms=[id], "
                 "namespace=\"all\", errors_only=false, minutes=4320) searches every "
                 "environment at once. Never conclude an id does not exist from one environment")
        steps.append(
            f"Identifiers {', '.join(ids[:4])}: {where}. Then temporal_workflows in that "
            "environment for its workflow, and the error signatures around it.")
    if _RUN_URL.search(text or ""):
        steps.append("A GitHub Actions run: `gh run view <id> --repo <owner/repo> --log-failed`; "
                     "name the failing job and step, quote the error line, and check whether a "
                     "re-run attempt passed before calling it a code problem.")
    if _PR_URL.search(text or ""):
        steps.append("A pull request: read the diff and CI state with gh; check it against the "
                     "repo's conventions in its .asta-context notes; list concrete findings.")
    if _HOW_WHY.search(text or ""):
        steps.append("A how/why question about the product: start from the workspace's "
                     ".asta-context index (the repo's _index.json, then its notes) to find the "
                     "service and code path, then confirm in the code; cite file:line.")
    if not steps:
        return ""
    return ("\n\nHow to check this (do these, in order, before concluding):\n"
            + "\n".join(f"- {x}" for x in steps) + booking_case.rule(text))


def _their_questions(who: str, need: str, questions: list) -> str:
    """What THEY asked, as questions the analysis must answer first.

    30 Sep, Vinish: "could you check booking H65ZMWX52B2 … Rini said it's a
    webhook failure … we need to verify one VTS fix". He needed: was VTS
    triggered for it, and what came back. The analysis answered around it — a
    billing error on the same id, a VTS plan document — true, and not his
    question. Arun: "that doesn't require even the 5xx child VTS workflow"."""
    qs = [" ".join(str(q).split()) for q in (questions or []) if str(q).strip()][:4]
    if not qs and not need:
        return ""
    lines = ["\n\nWhat {who} needs answered — answer EACH of these first, one line each "
             "with its evidence, before anything else:".format(who=who or "they")]
    lines += [f"{i}. {q}" for i, q in enumerate(qs, 1)] or [f"1. {need}"]
    lines.append("Stay on their question. Something else you notice (another service's "
                 "error on the same id, an unrelated plan or doc) goes in at most ONE line "
                 "starting \"Also noticed:\" — and only if it bears on their question. "
                 "Leave the rest out.")
    return "\n".join(lines)


def _waiting_brief(who: str, text: str, context: str, need: str = "",
                   questions: list | None = None) -> str:
    """What an investigation needs when a colleague is waiting on the answer.

    The conversation so far and their earlier conversations, so it continues
    rather than restarts; his indexed documents when they are about the ask, so
    an answer about the product comes from the product's own documentation; and
    the two-section ending that lets the finished answer reach him as one
    "send?" — see app/answers.py.
    """
    from . import answers
    parts = [_their_questions(who, need, questions or [])]
    if context.strip():
        parts.append("\n\nWhat has already been said with them (continue from it, do not "
                     "ask again for anything already given):\n" + context.strip())
    parts.append(_said_is_not_proof(who, f"{text}\n{context}"))
    try:
        from . import project_knowledge
        passages = project_knowledge.lookup(text, channel="teams")
    except Exception:                                          # noqa: BLE001
        passages = ""
    if passages:
        parts.append("\n\nFrom Arun's project knowledge (his documents and the repo "
                     "summaries) — use them and name the source when you do:\n" + passages)
    parts.append(playbook(text))
    from . import chat_watch
    shots = chat_watch.image_paths(f"{context}\n{text}")
    if shots:
        # 30 Sep: screenshots used to be dropped before anyone saw them. The
        # answer is often IN the image — an error dialog, a stack trace, a UI.
        parts.append("\n\nThey sent screenshot(s). Open each with the Read tool BEFORE "
                     "concluding — the answer may be in the image:\n"
                     + "\n".join(f"- {p}" for p in shots))
    parts.append(answers.brief_rider(who))
    return "".join(parts)


def _latest_finding(who: str, text: str = "", now: float | None = None) -> tuple[int, str]:
    """A recent finding for the same case AND environment, not merely the sender."""
    import time as _t
    from . import answers, booking_case
    now = _t.time() if now is None else now
    scope = booking_case.case_scope(text)
    if not scope:
        return 0, ""
    for t in store.list_tasks(limit=80):
        if t.get("status") != "done" or now - float(t.get("finished_at") or 0) > 24 * 3600:
            continue
        if booking_case.case_scope(t.get("prompt") or "") != scope:
            continue
        analysis, _reply = answers.split(t.get("result") or "")
        finding = analysis.strip() or (t.get("result") or "").strip()
        if finding:
            return t["id"], finding[:1800]
    return 0, ""


def _said_is_not_proof(who: str, text: str = "") -> str:
    """Old chat is context, never evidence.

    30 Sep, Vinish: asked for "the whole text flow", the investigation read the
    1:1 chat and handed back the 12:12 message — "the ETA/revalidation update
    triggered the cancellation" — as the answer. That line had been corrected
    an hour later from the logs (it was the price-update save). His rule: old
    conversation is context, not proof — think, check, decide."""
    tid, finding = _latest_finding(who, text)
    out = ("\n\nWhat was SAID in the chat is not what is TRUE. An earlier message — one of "
           "Arun's included — may have been corrected since. Before you repeat any earlier "
           "conclusion, check it against the evidence; where an earlier message and later "
           "evidence disagree, the evidence wins, and you say plainly that the earlier "
           "message was superseded rather than quoting it as the answer.")
    if finding:
        out += (f"\nPrevious investigation for this case (task #{tid}) — a lead to "
                "check, not proof. Reuse its evidence and investigate only the gaps "
                f"or later attempts:\n{finding}")
    return out


def line_for(task: dict, who: str, kind: str) -> str:
    """What he reads on his phone the moment it starts.

    Names the person and the fact that it is already running, because "X is
    asking about Y" and "X is asking about Y, I'm checking" are different
    messages: the first is another thing on his list, the second is one fewer.
    """
    what = {"incident": "whether that's actually happening in prod",
            "pr_review": "whether that review is right",
            "review_request": "their PR now — you'll get the review to approve",
            "debug": "it"}.get(kind, "it")
    if task.get("reused"):
        import time as _t
        at = _t.strftime("%H:%M", _t.localtime(float(task.get("at") or _t.time())))
        found = " ".join((task.get("result") or "").split())[:240]
        return (f"🔁 {who or 'Someone'} asked something already checked at {at} "
                f"(task #{task['id']}) — not run again. {found}").rstrip()
    if task.get("joined"):
        return (f"🔎 {who or 'Someone'} asked the same thing that is already being "
                f"checked (task #{task['id']}) — joined it, not started again.")
    return f"🔎 {who or 'Someone'} asked — I'm checking {what} now (task #{task['id']})."


# --- asked on a call ----------------------------------------------------------

#: Said into the brief of a check started on a live call.
_ON_THE_LINE = (
    "\n\nAsked on a LIVE CALL — {who} is waiting on the line for this. Go straight to the "
    "logs and Temporal for this booking; aim to finish within a few minutes. Lead your "
    "analysis with the answer in one or two plain sentences that can be said out loud.")


def check_for_call(who: str, question: str, ids: list[str], env: str) -> dict | None:
    """Start the log check for a booking someone asked about on a call.

    Arun, 2 Oct: "even if someone asks in call also check in logs, ask them to
    wait for couple of mints, check and update". The same read-only analysis a
    Teams ask gets — same brief, same knowledge, same rule — with their 1:1 as
    where the written answer goes, for his "send?". None when the responder is
    off. A question already checked comes back as `reused` with its result."""
    if not enabled() or not ids:
        return None
    from . import answers, results_cache, tasks
    text = f"{question}\n(booking: {', '.join(ids[:3])})" + (f"\n(environment: {env})" if env else "")
    ck = results_cache.key_for("debug", text)
    hit = results_cache.lookup(ck)
    if hit:
        return {"id": hit["task_id"], "reused": hit["state"] == "done",
                "joined": hit["state"] != "done", "result": hit.get("result", "")}
    brief = (brief_for("debug", who, text) + _ON_THE_LINE.format(who=who or "They")
             + _waiting_brief(who, text, "", need=question[:160]))
    t = tasks.spawn(title_for("debug", who, text), brief, "analysis", None,
                    teams_chat=who, priority=1)
    _note_started(__import__("time").time())
    store.kv_set(f"responder_task:{t['id']}", f"call|{who}|debug|asked on a call")
    answers.remember_meta(t["id"], who=who, need=question[:160], chat=who, group=False,
                          thread="")
    results_cache.start(ck, "debug", t["id"])
    store.record_outcome("responder", "call_check", subject=str(t["id"]),
                         detail=f"{who}: {text[:140]}")
    return t
