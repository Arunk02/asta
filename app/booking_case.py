"""A question about ONE booking — checked in the logs, never answered blind.

Arun, 2 Oct: "if someone shares … does manual customs reach billing for this
booking … u have to use logs to confirm … if they split and send in as two
messages, first message and then bookingid, wait and understand, ask them and
then respond either question or debugging. dont be blind in this." And: "ask
user which env have to check irrespective of always checking in prod".

What went wrong before this, measured on the real code:
  * "does manual customs reach billing for booking MH65W8JZNVNT" read as
    "nothing checkable" — no investigation, one line on his phone;
  * the question first and the id a few seconds later were judged apart: the
    question answered from the documents, the bare id dropped;
  * "for this booking" said to voice got the documents' general answer;
  * an id with no environment was searched in prod, or everywhere, by guess.

One definition here, used by every door — Teams, WhatsApp, voice, calls — and
every brain, so no two halves of Asta disagree about what a booking question
is or what checking one means.
"""
from __future__ import annotations

import re

#: Booking refs (H65ZMWX52B2, MH65W8JZNVNT), CBK/job numbers, long numeric ids.
#: Typed in any case; a word never has two digits in it, so case-insensitive is
#: safe once letters AND digits are both required.
ID = re.compile(r"\b(?=[A-Za-z0-9]*\d[A-Za-z0-9]*\d)(?=[A-Za-z0-9]*[A-Za-z][A-Za-z0-9]*[A-Za-z])"
                r"[A-Za-z0-9]{9,14}\b|\bCBK\d{5,}\b|\b\d{8,10}\b", re.I)

#: The environments, as people say them → the name the tools take.
_ENV = re.compile(r"\b(?:(pre-?\s?prod|pp)|(prod(?:uction)?|live)|(sit)|(uat)|(dev)|(qa)|(perf)"
                  r"|(lower\s+env\w*))\b", re.I)
_ENV_NAMES = ("preprod", "prod", "sit", "uat", "dev", "qa", "perf", "lower")
ENVS = "prod, preprod, sit or uat"

_THING = (r"(?:booking(?:\s+(?:id|number|no|ref))?|order|container|shipment|equipment|bkg|"
          r"id|ref(?:erence)?|transport\s+order|service\s*plan)s?")
#: Points at a case it does not include — the id is coming next.
_FORWARD = re.compile(rf"\b(?:this|these|below|following|attached|given|the\s+below|"
                      rf"the\s+following)\s+{_THING}\b|\b(?:for|check|in)\s+this\s*[:?.!]*\s*$"
                      r"|[:：]\s*$|\.\.\.\s*$", re.I)
#: Points at a case said before.
_BACK = re.compile(rf"\b(?:that|the\s+same|above|mentioned|same)\s+{_THING}\b", re.I)
#: Asking something, rather than handing something over.
_ASKS = re.compile(r"\?|\b(?:why|how|what|when|where|which|did|does|do|is|was|were|has|have|"
                   r"can|could|whether|check|verify|confirm|look|debug|see|reach(?:ed)?|"
                   r"fail(?:ed|ing)?|stuck|not\s+(?:done|happen\w*|received|sent|created))\b", re.I)
#: What may sit around a bare id without making it a question.
_FILLER = re.compile(r"\b(?:booking|bkg|id|no|number|ref|reference|here|this|is|it|the|in|env|"
                     r"environment|bro|da|sir|pls|please|ok|okay|thanks|hi|hey)\b", re.I)


def ids(text: str) -> list[str]:
    """The ids in it, upper-cased, in order, once each."""
    out: list[str] = []
    for m in ID.finditer(text or ""):
        v = m.group(0).upper()
        if v not in out:
            out.append(v)
    return out


def env_of(text: str) -> str:
    """The environment named in it — 'preprod', 'prod', 'sit'… — or ''."""
    for m in _ENV.finditer(text or ""):
        for name, g in zip(_ENV_NAMES, m.groups()):
            if g:
                return name
    return ""


def points_at_one(text: str) -> bool:
    """It is about one particular booking (or order, container…), named or not."""
    return bool(_FORWARD.search(text or "") or _BACK.search(text or "") or ids(text))


def asks(text: str) -> bool:
    return bool(_ASKS.search(ID.sub(" ", text or "")))


def bare(text: str) -> bool:
    """Only an id (and maybe an environment, a 'booking:' label) — no question."""
    rest = ID.sub(" ", text or "")
    rest = _ENV.sub(" ", rest)
    rest = _FILLER.sub(" ", rest)
    return bool(ids(text) or env_of(text)) and not re.search(r"[A-Za-z]{2,}|\?", rest)


def waiting_for_more(text: str, before: str = "") -> str:
    """Why this message should wait for the next one, or ''.

    People split one ask over two messages — the question, then the id; or the
    id, then the question. Judged alone, the first half is either answered from
    the documents (blind) or dropped as nothing. `before` is what the same
    person said just before, and an open question already waiting on them."""
    t = (text or "").strip()
    if not t:
        return ""
    if _FORWARD.search(t) and not ids(t):
        return "names a booking it does not include"
    if bare(t) and ids(t) and not asks(before):
        return "an id with no question yet"
    return ""


def missing(text: str, context: str = "") -> list[str]:
    """What a question about one booking still lacks: 'booking', 'environment'.

    Empty for a general question — "does manual customs reach billing?" is the
    documents' to answer, and asking which booking would be the blind reply in
    the other direction."""
    if not points_at_one(text) and not (bare(text) and points_at_one(context)):
        return []
    whole = f"{context}\n{text}"
    out = []
    if not ids(whole):
        out.append("booking")
    if not env_of(whole):
        out.append("environment")
    return out


def question_for(lacking: list[str], found: list[str] | None = None) -> str:
    """The one short line that gets what is missing — politely, both at once."""
    found = found or []
    if lacking == ["booking", "environment"]:
        return f"Sure — which booking is it, and in which environment ({ENVS})? I'll check the logs for it."
    if lacking == ["booking"]:
        return "Sure — which booking is it? Share the booking number and I'll check the logs."
    if lacking == ["environment"]:
        which = found[0] if found else "it"
        return f"Sure — which environment is {which} in: {ENVS}? I'll check the logs there."
    return ""


def ask_line(text: str, context: str = "") -> str:
    """The one question to send back for a half-given booking ask, or ''.

    An id on its own, with nothing asked anywhere around it, gets "what would
    you like me to check?" — not "which environment?", which would answer a
    question nobody asked."""
    whole = f"{context}\n{text}"
    found = ids(whole)
    if bare(text) and not asks(context):
        if not found:
            return ""
        env = "" if env_of(whole) else f", and in which environment ({ENVS})"
        return f"Sure — what would you like me to check on {found[0]}{env}?"
    return question_for(missing(text, context), found)


#: Billing is another team's system, and in the lower environments it mostly
#: does not work: checked only when they ask about it (Arun, 2 Oct).
_ABOUT_BILLING = re.compile(r"\b(?:bill\w*|invoic\w*|financ\w*|revenue|FACT)\b", re.I)
_BILLING = ("   billing (asked about): AP→billing is outbound, from Send to TMS — manual customs "
            "reaches billing for S4 countries only; billing statuses (Send To Finance, Ready For "
            "Invoicing, Invoice Triggered) are inbound only.\n")
_NOT_BILLING = ("   Billing is out of scope unless they ask about it: its errors are not this "
                "booking's problem — leave them out, or one 'Also noticed' line at most.\n")


def rule(text: str, context: str = "", heard: bool = False) -> str:
    """How to answer a question about one booking: the documents give the rule,
    the logs say what happened to THIS one. '' when no id is in hand."""
    whole = f"{context}\n{text}"
    found = ids(whole)
    if not found:
        return ""
    env = env_of(whole)
    where = (f"the {env} environment" if env and env != "lower" else
             "the lower environments (sit, uat, preprod)" if env == "lower" else
             "an environment nobody named — search every one (namespace=\"all\") and say which "
             "it was found in; never assume prod")
    out = (f"\n\nThis is about a specific case — {', '.join(found[:3])}, in {where}. Answer "
           "WHATEVER they asked about it — a send, an ack, a status, a milestone, a failure, "
           "why something did or did not happen — never from documents alone:\n"
           "1. From the project knowledge, what SHOULD happen for their question (name the "
           "document).\n"
           "2. From this booking's logs and Temporal history, what DID happen: "
           "grafana_logs(terms=[id]) with the id ALONE first — see which services touched it "
           "(filters are case-sensitive, and 'custom' matches 'customer': never conclude 'not "
           "sent' from one keyword). Milestones and the event log are how the UI tracks a "
           "booking — use them. Mind each flow's real direction:\n"
           "   two-way (sent, then ack back): booking→AP (→ ACTIVITYPLAN_FEEDBACK); booking→TMS "
           "(SEND_TO_TMS → SAP_TMS_ACK_FEEDBACK); AP→customs UNITED (→ CIP_CHASSIS / CIP_GOT "
           "Ack).\n"
           "   inbound only (received or not — never 'no ack'): SAP_TMS_EXECUTION_STATUS; "
           "customs status (CIP_GOT); manual customs (CUSTOMS_UPDATE).\n"
           "   outbound only (sent or not): booking→IOM; →event history; AP→email and "
           "documents (SEND_DOCUMENTS).\n"
           + (_BILLING if _ABOUT_BILLING.search(whole) else _NOT_BILLING) +
           "3. Where the logs and the document disagree, say so — for this booking the logs "
           "win — and that the document may need correcting.\n"
           "Summarise SIMPLY, in the ANALYSIS: the answer to their question first, in one or "
           "two plain lines; then only the few facts that prove it (time, service, event, "
           "milestone — a log line or class:line where it matters); then what is missing or "
           "could not be checked. No table or per-flow list unless they asked what happened "
           "overall. At most one 'Also noticed'.\n"
           "If the logs or Temporal cannot be read, say 'from the documents only — not "
           "confirmed in the logs' and what failed. Never present a document's answer as checked.")
    if heard:
        out += ("\nThe id was HEARD on a call and may be misheard: if it is not found, try the "
                "nearest spellings (one character off) before saying it does not exist.")
    return out


def for_turn(text: str, context: str = "") -> str:
    """What a chat brain (Claude or Copilot, WhatsApp or a voice job) carries for
    a turn about one booking: how to check it, or what to ask first. ''
    otherwise — a general question carries nothing extra."""
    lacking = missing(text, context)
    if lacking:
        found = ids(f"{context}\n{text}")
        ask = question_for(lacking, found)
        what = " and ".join({"booking": "the booking number",
                             "environment": "the environment"}[x] for x in lacking)
        return (f"\n\nThis is about one specific booking, but {what} "
                f"{'are' if len(lacking) > 1 else 'is'} not given. Look in "
                f"the conversation first; if it is not there, ask for it in one short line "
                f"(\"{ask}\") and stop — do not answer as if the documents confirmed this "
                f"booking, and never assume prod.")
    return rule(text, context)


# --- ids said out loud --------------------------------------------------------

_SPOKEN_TOKEN = re.compile(r"^(?:[A-Za-z0-9]|[A-Z0-9]{2,4}|[A-Za-z]*\d[A-Za-z0-9]*)$")


def spoken_ids(text: str) -> list[str]:
    """Ids as speech recognition writes them: "M H 6 5 W 8 J Z N V N T", "MH65 W8JZ
    NVNT", "m-h-6-5…". A run of single characters, digits and short capitals is
    joined; it counts only when the result has the shape of an id."""
    found = ids(text)
    words = re.sub(r"(?<=\w)[-.](?=\w)", " ", text or "").replace(",", " ").split()
    run: list[str] = []
    for w in words + [""]:
        w = w.strip(".?!;:")
        if w and _SPOKEN_TOKEN.match(w):
            run.append(w)
            continue
        if len(run) >= 2:
            joined = "".join(run).upper()
            for v in ids(joined):
                if v not in found:
                    found.append(v)
        run = []
    return found


# --- the evidence, fetched before anyone reasons about it --------------------------
#
# 2 Oct, live on uat: asked "have we sent to TMS? customs UNITED? billing?", the
# investigation searched by keyword in errors-only mode and answered "customs:
# yes, touched" from 40 lines of payload fields and an email PDF heading — while
# the SAP TMS ack that DID come back went unmentioned. The facts are in the logs;
# they are now read in code, per flow, in each flow's real direction.

#: (flow, direction, what a send/receipt looks like, what its ack looks like).
#: Messages taken from the code (AP CustomsServiceImpl, booking
#: InitiateBookingEventsWorkflow "…eventName :X", EventRouterService…).
FLOWS = (
    ("booking → AP", "two-way",
     r"ActivityPlan temporal workflow initialized/signaled|Sending signal with Start Workflow",
     r"ACTIVITYPLAN_FEEDBACK"),
    ("booking → TMS", "two-way",
     r"TMS message published|eventName\W{0,3}SEND_TO_TMS|signal from booking : SEND_TO_TMS",
     r"SAP_TMS_ACK_FEEDBACK"),
    ("TMS execution status → booking", "inbound", r"SAP_TMS_EXECUTION_STATUS", ""),
    ("AP → customs (UNITED)", "two-way",
     r"Customs message request successfully published|Started sending events to customs",
     r"CUSTOMS_FEEDBACK|Processing customs feedback|CIP_GOT|CIP_CHASSIS"),
    ("manual customs update", "inbound", r"CUSTOMS_UPDATE|MANUAL_CUSTOMS_STATUS_UPDATE", ""),
    ("booking → IOM", "outbound", r"Sending booking data to IOM", ""),
    ("→ event history", "outbound",
     r"Event History Data|send event history|EventHistory published|Data saved successfully to database", ""),
    ("→ email / documents", "outbound",
     r"Received ActivityPlan via temporal|Building Pdf is completed|SEND_DOCUMENTS", ""),
)
_BILLING_FLOWS = (
    ("AP → billing", "outbound", r"BillingWorkflow Version|billing object created", ""),
    ("billing feedback → AP", "inbound",
     r"started Feedback to AP|Completed running feedBackToActivityPlan for event", ""),
)


def _seen(records: list[dict], pattern: str) -> tuple[float, int]:
    if not pattern:
        return 0.0, 0
    rx = re.compile(pattern)
    hits = [r["timestamp"] for r in records if rx.search(r.get("line") or "")]
    return (min(hits), len(hits)) if hits else (0.0, 0)


def flows(records: list[dict], billing: bool = False) -> str:
    """One line per flow: sent or received (when, how often), and its ack."""
    import time as _t

    def at(ts: float, n: int) -> str:
        return _t.strftime("%d %b %H:%M:%S", _t.localtime(ts)) + (f" ×{n}" if n > 1 else "")

    out = []
    for name, way, there, back in FLOWS + (_BILLING_FLOWS if billing else ()):
        t1, n1 = _seen(records, there)
        verb = "received" if way == "inbound" else "sent"
        line = f"- {name} ({way}): " + (f"{verb} {at(t1, n1)}" if n1 else f"not seen ({verb})")
        if way == "two-way":
            t2, n2 = _seen(records, back)
            line += f" · ack {at(t2, n2)}" if n2 else " · ack not seen"
        out.append(line)
    return "\n".join(out)


# --- milestones and the event log: what the UI shows ------------------------------
#
# Arun, 3 Oct: "always focus on event log as well as milestone … here all tracked by
# milestone, as u see in the UI". booking-service logs each milestone as a
# structured line (UpdateFeedbackActivityImpl.logTime: MILESTONE, WORK_PROCESS_NAME,
# WORK_PROCESS_STATUS, TIME_TAKEN_MILLIS); event-log entries are published by AP
# ("At send event history … eventName X"), email ("Published event history … event
# name X") and as EventHistory payloads ("eventType": "X"), and persisted by
# event-history-service.

_EH_NAME = (re.compile(r"At send event history for bookingId \S+ and orderId \S+ and eventName ([^\"\\]+)"),
            re.compile(r"Published event history for orderId \S+ booking id \S+ and event name ([^\"\\]+)"),
            re.compile(r"(?:EventHistory|eventDetails).{0,400}?\\?\"eventType\\?\": ?\\?\"([^\"\\]+)"))


def _short(service: str) -> str:
    return service.replace("telikos-", "").replace("-service", "")


def milestones(records: list[dict]) -> str:
    """Each milestone booking-service recorded: name, work process, status, when."""
    import json as _json
    import time as _t
    seen: dict[tuple, list] = {}
    for r in records:
        line = r.get("line") or ""
        if '"MILESTONE"' not in line:
            continue
        try:
            body = _json.loads(line[line.find("{"):])
        except ValueError:
            continue
        key = (body.get("MILESTONE", "?"), body.get("WORK_PROCESS_NAME", "?"),
               body.get("WORK_PROCESS_STATUS", "?"))
        seen.setdefault(key, []).append((r["timestamp"], body.get("TIME_TAKEN_MILLIS")))
    if not seen:
        return "Milestones: none logged in this window."
    out = ["Milestones (booking-service — what the UI tracks):"]
    for (ms, wp, st), hits in sorted(seen.items(), key=lambda kv: kv[1][0][0]):
        when = _t.strftime("%d %b %H:%M:%S", _t.localtime(hits[0][0]))
        took = f", took {int(hits[0][1]) / 1000:.1f}s" if str(hits[0][1] or "").isdigit() else ""
        out.append(f"- {ms} · {wp} · {st} · {when}{took}" + (f" ×{len(hits)}" if len(hits) > 1 else ""))
    return "\n".join(out)


def event_log(records: list[dict], billing: bool = False) -> str:
    """The booking's event log as published: entry, who published it, when; and
    whether event-history-service saved it."""
    import time as _t
    entries: dict[tuple, list] = {}
    saved = []
    for r in records:
        line, svc = r.get("line") or "", r.get("service", "")
        if "event-history" in svc and "saved successfully" in line:
            saved.append(r["timestamp"])
            continue
        if "billing" in svc and not billing:
            continue
        for rx in _EH_NAME:
            m = rx.search(line)
            if m:
                entries.setdefault((m.group(1).strip(), _short(svc)), []).append(r["timestamp"])
                break
    if not entries and not saved:
        return "Event log: nothing published in this window."
    out = ["Event log (entries published, source; as the UI's event log shows):"]
    for (name, src), hits in sorted(entries.items(), key=lambda kv: kv[1][0])[:15]:
        when = _t.strftime("%d %b %H:%M:%S", _t.localtime(min(hits)))
        out.append(f"- {when} {name} ({src})" + (f" ×{len(hits)}" if len(hits) > 1 else ""))
    if saved:
        out.append(f"- saved by event-history-service ×{len(saved)}, last "
                   + _t.strftime("%d %b %H:%M:%S", _t.localtime(max(saved))))
    return "\n".join(out)


_CASE = re.compile(r"This is about a specific case — ([A-Z0-9, ]+?), in (?:the (\w+) environment|"
                   r"the lower environments|an environment nobody named)")


async def evidence(prompt: str, minutes: int = 4320) -> str:
    """What the logs say about the booking a brief is about — per flow, and its
    trail — fetched before the investigation starts. '' when the brief is not
    about one booking, or the logs cannot be read (the brief already says how to
    report that)."""
    import asyncio
    from . import grafana
    m = _CASE.search(prompt or "")
    if not m or not grafana.enabled():
        return ""
    found = [i.strip() for i in m.group(1).split(",") if i.strip()][:2]
    env = m.group(2) or ""
    envs = [env] if env else (["sit", "uat", "preprod"] if "lower" in m.group(0)
                              else grafana.envs())
    billing = bool(_ABOUT_BILLING.search(prompt.split("This is about a specific case")[0]))
    parts = []
    for booking in found:
        async def one(e: str) -> tuple[str, list[dict]]:
            try:
                return e, await grafana.records_for(booking, e, minutes)
            except Exception:                                  # noqa: BLE001
                return e, []
        got = await asyncio.gather(*(one(e) for e in envs))
        hit = [(e, r) for e, r in got if r]
        if not hit:
            parts.append(f"{booking}: no log lines in {', '.join(envs)} over the last "
                         f"{minutes // 1440} days.")
            continue
        for e, recs in hit[:2]:
            parts.append(f"{booking} in {e} — {len(recs)} lines, last {minutes // 1440} days.\n"
                         f"Flows (matched in code on known log messages; 'not seen' means not "
                         f"in these logs — it may be logged differently, so check before "
                         f"saying it was not sent):\n{flows(recs, billing)}\n"
                         f"{milestones(recs)}\n{event_log(recs, billing)}\n"
                         + grafana.render_trail(grafana.trail(recs, [booking]),
                                                30 if len(hit) == 1 else 12))
    if not parts:
        return ""
    return ("\n\n[Evidence Asta read from the logs before you started — start from it, "
            "confirm what you rely on]\n" + "\n\n".join(parts))
