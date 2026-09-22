"""The instruction compiler: say it once, and it holds everywhere.

When he gives a STANDING instruction — "don't check on any incidents going
forward unless I ask", "my favourite workspace is booking", "never message the
release channel" — three things used to be possible, and only the weakest
happened: the chat brain nodded, maybe wrote a memory note, and the next path
that needed the rule never saw it.

Now the message is compiled into a typed rule (app/policy.py) and proposed to
him once, as a one-tap offer. His yes does all of it at once:

  * the rule goes into the policy gate, which every doer checks;
  * the line goes into guardrails.md "Standing instructions", so every brain
    reads it in his words;
  * a replay scenario is written from the moment he said it, so a regression
    is caught by the bench, not by him (data/workworld/replays — his real words
    stay on his machine; the repo is public).

Compilation is rules first: the shapes he actually uses map to typed rules with
no model. What doesn't fit a typed kind becomes a `note` — still recorded, still
routed to every brain, just not machine-checkable.

Only STANDING instructions are caught. "don't throw 400 in the callback" is an
instruction for one task and belongs to that task; the markers below — going
forward, from now on, never, always, unless I ask, my favourite — are what make
an instruction outlive the conversation it was given in.
"""

from __future__ import annotations

import datetime as dt
import re
import time
from dataclasses import asdict, dataclass

from . import store

_STANDING = re.compile(
    r"\b(going forward|from now on|henceforth|any ?more|ever again|in future|"
    r"unless i (?:ask|say|tell)|always|never|every ?time|whenever|by default|"
    r"blindly|without (?:asking|checking with) me|remember (?:that|my)|"
    r"my (?:favou?rite|default|preferred))\b", re.I)
_NEGATION = r"(?:don'?t|dont|do not|never|stop|no more)"

_MUTE_KINDS = (
    (re.compile(r"\bincidents?\b|\bsev ?\d|\bservice ?now\b|\bINC\d", re.I), "incident"),
    (re.compile(r"\bpr reviews?\b|\breview requests?\b", re.I), "pr_review"),
    (re.compile(r"\bdebug", re.I), "debug"),
)
_MUTE = re.compile(_NEGATION + r"\s+(?:\w+\s+){0,2}?(?:check|look|investigat\w*|analy[sz]\w*|"
                   r"monitor\w*|respond\w*|jump)\w*\b", re.I)
_NEVER_PERSON = re.compile(
    _NEGATION + r"\s+(?:ever\s+)?(message|ping|text|dm|call|ring|contact|send (?:anything |messages? )?to|"
    r"reply to)\s+(?P<who>(?:the\s+)?[A-Za-z][\w.&-]*(?:\s+(?!unless|without|again|going|from|any|ever)"
    r"[A-Za-z][\w.&-]*){0,3})", re.I)
_NEVER_PUSH = re.compile(_NEGATION + r"\s+(?:\w+\s+){0,1}?(push|merge|ship)\b", re.I)
_PREFER = re.compile(
    r"\bmy\s+(?:favou?rite|default|preferred)\s+(workspace|repo|brain|model)\s+is\s+"
    r"(?P<value>[\w-]+(?:\.[\w-]+)*)", re.I)
_UNLESS = re.compile(r"\bunless i\b|\bwithout (?:asking|checking with) me\b|\bblindly\b", re.I)


@dataclass(frozen=True)
class Candidate:
    kind: str
    act: str = ""
    target: str = ""
    value: str = ""
    unless_asked: bool = False
    words: str = ""

    def render(self) -> str:
        from .policy import Rule
        return Rule(0, **asdict(self)).render()


def is_standing(text: str) -> bool:
    t = (text or "").strip()
    return 8 <= len(t) <= 400 and bool(_STANDING.search(t))


def states_a_default(text: str) -> bool:
    return bool(_PREFER.search((text or "").replace("’", "'")))


# --- quiet time, as he says it -------------------------------------------------------
# "sat and Sunday be on silent … don't have to notify me anything … summarise all
# on Monday mrng", "go silent for a week", "don't ping me until tomorrow 11:30".

#: Asking for quiet — for HIS phone. "Don't message Sam on Sunday" is a rule
#: about Sam, not quiet time, so a verb needs "me" after it; "be on silent",
#: "go quiet", "do not disturb" and "no notifications" need nothing more.
_SILENCE = re.compile(
    r"\b(?:be|go|stay|keep(?: it)?|put (?:me|it|everything) on)\s+(?:on\s+)?(?:silent|quiet|mute)\b"
    r"|\bsilent mode\b|\bon (?:silent|mute)\b|\bdo not disturb\b|\bdnd\b"
    r"|\bmute (?:everything|all|notifications?|pings?|alerts?)\b"
    r"|\b(?:don'?t|dont|do not|no need to|never)\s+(?:\w+\s+){0,3}?"
    r"(?:notify|ping|message|disturb|push|buzz|text|alert)\w*\s+(?:me|arun)\b"
    r"|\bno (?:notifications?|pings?|messages?|alerts?|pushes)\b", re.I)
_DAY_WORDS = {"mon": 0, "monday": 0, "tue": 1, "tuesday": 1, "wed": 2, "wednesday": 2,
              "thu": 3, "thursday": 3, "fri": 4, "friday": 4, "sat": 5, "saturday": 5,
              "sun": 6, "sunday": 6}
_FULL_DAYS = {k: v for k, v in _DAY_WORDS.items() if k.endswith("day")}
_SHORT_DAYS = {"mon": 0, "tue": 1, "tues": 1, "wed": 2, "thu": 3, "thur": 3, "thurs": 3,
               "fri": 4, "sat": 5, "sun": 6}
#: Words around a short day name that make it a day — "sat and Sunday" is one,
#: "I sat at my desk" and "the sun" are not.
_DAY_NEIGHBOURS = {"on", "every", "this", "next", "and", "&", ",", "/", "till", "until", "or"}
_WEEKDAY = (r"mon(?:day)?|tue(?:sday)?|wed(?:nesday)?|thu(?:rsday)?|fri(?:day)?|"
            r"sat(?:urday)?|sun(?:day)?")


def _days_in(text: str) -> list[int]:
    """The weekdays a sentence names, 0 = Monday."""
    words = re.findall(r"[a-z]+|[,&/]", (text or "").lower())
    out: set[int] = set()
    for i, w in enumerate(words):
        if w in ("weekend", "weekends"):
            out |= {5, 6}
        elif w in _FULL_DAYS or (w.endswith("s") and w[:-1] in _FULL_DAYS):
            out.add(_FULL_DAYS[w if w in _FULL_DAYS else w[:-1]])
        elif w in _SHORT_DAYS:
            prev = words[i - 1] if i else ""
            nxt = words[i + 1] if i + 1 < len(words) else ""
            if prev in _DAY_NEIGHBOURS or nxt in _DAY_NEIGHBOURS:
                out.add(_SHORT_DAYS[w])
    return sorted(out)


_RELEASE = re.compile(
    r"\b(?:summari[sz]e|summary|update|tell|brief|catch me up)\b.{0,40}?"
    rf"\b({_WEEKDAY}|tomorrow)\b(?:\s+(morning|mrng|mrg|mng|am|evening|night))?", re.I)
#: A day named once is that day; these make it every week.
_EVERY = re.compile(r"\b(?:every|each|always|going forward|from now on|weekly)\b"
                    r"|\b(?:weekends|(?:mon|tues|wednes|thurs|fri|satur|sun)days)\b", re.I)
_THIS = re.compile(rf"\b(?:this|next|coming)\s+(weekend|{_WEEKDAY})\b", re.I)
_FOR = re.compile(r"\bfor (?:the )?(?:(a|an|one|1|two|2|three|3|\d+) )?(week|day|hour|hr|minute|min)s?\b",
                  re.I)
_UNTIL = re.compile(
    rf"\b(?:until|till|til|upto|up to)\s+(?:(tomorrow|today|tonight|{_WEEKDAY})\s*)?"
    r"(?:(morning|mrng|noon|evening|night)|(\d{1,2})(?::(\d{2}))?\s*(am|pm)?)?", re.I)
_TODAY = re.compile(r"\b(?:today|tonight|rest of the day|for the day)\b", re.I)
_TOMORROW = re.compile(r"\btomorrow\b", re.I)
_NUM = {"a": 1, "an": 1, "one": 1, "two": 2, "three": 3}
_UNIT = {"week": 7 * 86400, "day": 86400, "hour": 3600, "hr": 3600, "minute": 60, "min": 60}


def _day_at(base: dt.datetime, days: int, hour: int = 9, minute: int = 0) -> dt.datetime:
    return (base + dt.timedelta(days=days)).replace(hour=hour, minute=minute,
                                                    second=0, microsecond=0)


def _window(start: dt.datetime, until: dt.datetime) -> str:
    return f"from={int(start.timestamp())};until={int(until.timestamp())}"


def quiet_spec(text: str, now: float | None = None) -> str:
    """The quiet rule a message asks for, as policy data — or '' when it asks none.

    Weekly ("Saturdays and Sundays … going forward") becomes days + a release
    time; anything else is one window, from/until, which ends by itself.
    """
    t = text or ""
    if not _SILENCE.search(t):
        return ""
    now = time.time() if now is None else now
    at = dt.datetime.fromtimestamp(now)
    midnight = at.replace(hour=0, minute=0, second=0, microsecond=0)
    release = _RELEASE.search(t)
    # The day he wants the summary on is not a quiet day: "…summarise all on
    # Monday mrng" named Monday, and read naively made it silent too.
    quiet_part = t[:release.start()] + t[release.end():] if release else t
    until_m = _UNTIL.search(quiet_part)
    if until_m and not any(until_m.groups()):
        until_m = None                    # "until" with nothing after it
    if until_m:
        quiet_part = quiet_part[:until_m.start()] + quiet_part[until_m.end():]
    days = _days_in(quiet_part)
    if days and _EVERY.search(t) and not _THIS.search(t):
        rel = ""
        if release and release.group(1).lower() != "tomorrow":
            hour = "18:00" if (release.group(2) or "").lower() in ("evening", "night") else "09:00"
            rel = f"release={release.group(1).lower()[:3]} {hour}"
        if not rel:
            # The morning after the last quiet day, unless he named a time.
            after = (days[-1] + 1) % 7
            rel = f"release={('mon', 'tue', 'wed', 'thu', 'fri', 'sat', 'sun')[after]} 09:00"
        names = ",".join(("mon", "tue", "wed", "thu", "fri", "sat", "sun")[d] for d in days)
        return f"days={names};{rel}"
    if days:
        # Those days, once: from the next of them (now, when today is one) over
        # the run of days that follows it, to the morning after. "This weekend"
        # said on a Sunday is today, not a week on Saturday.
        ahead = sorted((d - at.weekday()) % 7 for d in days)
        first = last = ahead[0]
        while last + 1 in ahead:
            last += 1
        start = max(at, _day_at(midnight, first, 0))
        return _window(start, _day_at(midnight, last + 1))
    m = _FOR.search(t)
    if m:
        n = _NUM.get((m.group(1) or "a").lower()) or int(m.group(1))
        span = n * _UNIT[m.group(2).lower()]
        until = at + dt.timedelta(seconds=span)
        if span >= 86400:
            until = until.replace(hour=9, minute=0, second=0, microsecond=0)
        return _window(at, until)
    if until_m:
        day, part, hh, mm, ampm = until_m.groups()
        day = (day or "").lower()
        if day == "tomorrow":
            offset = 1
        elif day in ("", "today", "tonight"):
            offset = 0
        else:
            offset = (_DAY_WORDS[day[:3]] - at.weekday()) % 7 or 7
        hour, minute = 9, 0
        if part:
            hour = {"noon": 12, "evening": 18, "night": 21}.get(part.lower(), 9)
            if part.lower() in ("morning", "mrng") and offset == 0:
                offset = 1                  # "till morning" said today is tomorrow's
        elif hh:
            hour, minute = int(hh), int(mm or 0)
            if (ampm or "").lower() == "pm" and hour < 12:
                hour += 12
        until = _day_at(midnight, offset, hour, minute)
        if until <= at:
            until += dt.timedelta(days=1)   # "until 9" said at ten means tomorrow
        return _window(at, until)
    if _TOMORROW.search(quiet_part):
        return _window(max(at, _day_at(midnight, 1, 0)), _day_at(midnight, 2))
    if _TODAY.search(quiet_part):
        return _window(at, _day_at(midnight, 1))
    return ""


def compile(text: str) -> Candidate | None:  # noqa: A001 — the word for what it does
    """The rule a standing instruction states, or None when it isn't one."""
    t = " ".join((text or "").replace("’", "'").split())
    # Quiet time is a rule whether or not it says "going forward": "go silent
    # until tomorrow 11:30" is as binding for its window as a weekly one.
    if t and not t.rstrip().endswith("?"):
        spec = quiet_spec(t)
        if spec:
            return Candidate("quiet", "push", value=spec, words=t)
    if not is_standing(t):
        return None
    unless = bool(_UNLESS.search(t))
    m = _PREFER.search(t)
    if m:
        return Candidate("prefer", m.group(1).lower(), value=m.group("value"), words=t)
    if t.rstrip().endswith("?"):
        return None              # a question about a habit is not an instruction
    if _MUTE.search(t):
        for pat, kind in _MUTE_KINDS:
            if pat.search(t):
                return Candidate("mute", "investigate", kind, unless_asked=unless, words=t)
    m = _NEVER_PERSON.search(t)
    if m:
        verb = m.group(1).lower()
        act = "call" if verb in ("call", "ring") else "send"
        who = re.sub(r"^the\s+", "", m.group("who").strip(" .,!?"), flags=re.I)
        return Candidate("never", act, who, unless_asked=unless, words=t)
    m = _NEVER_PUSH.search(t)
    if m:
        return Candidate("never", m.group(1).lower(), unless_asked=True, words=t)
    if re.search(r"\b(never|always|going forward|from now on|don'?t|dont|do not)\b", t, re.I):
        return Candidate("note", words=t)
    return None


# --- proposing, and what his yes does ---------------------------------------------------

def propose(text: str, cand: Candidate) -> str:
    """Stage the rule as a one-tap offer; returns the line to show him."""
    from . import offers
    q = f"Make this a standing rule? “{cand.render()}”"
    offers.staged_write("rule_add", {**asdict(cand), "said_on": dt.date.today().isoformat()},
                        subject="a standing rule", context=f"You said: “{text.strip()[:200]}”",
                        question=q)
    store.record_outcome("rule", "proposed", detail=cand.render()[:200])
    return f"📌 {q}\nReply yes to keep it everywhere, or no."


def go_quiet(cand: Candidate) -> str:
    """A quiet window he asked for, in so many words: it starts now, no offer.

    "Don't notify me until 11:30" is the act itself, not a habit to agree to —
    asking him "make this a standing rule?" first is the delay he asked to be
    spared. It ends by itself; a weekly rule still goes through `propose`.
    """
    from . import policy
    rule = policy.add("quiet", "push", value=cand.value, words=cand.words)
    store.record_outcome("rule", "quiet window", detail=rule.render()[:200])
    return (f"🔕 {rule.render()}. Reminders you set still ring. "
            "Say “I'm back” to end it early.")


def is_one_off_quiet(cand: Candidate | None) -> bool:
    return bool(cand) and cand.kind == "quiet" and "until=" in (cand.value or "")


def adopt(args: dict) -> str:
    """His yes: the gate, the guardrails file, a replay — all three."""
    from . import guardrails, policy
    rule = policy.add(args["kind"], args.get("act", ""), args.get("target", ""),
                      args.get("value", ""), bool(args.get("unless_asked")),
                      args.get("words", ""))
    said_on = args.get("said_on") or dt.date.today().isoformat()
    guardrails.append_standing(f"{said_on}: {rule.render() if rule.kind != 'note' else rule.words}")
    path = write_replay(rule, said_on)
    tail = " A replay now guards it." if path else ""
    return f"📌 Standing rule {rule.id}: {rule.render()}.{tail}"


# --- the replay that keeps it true --------------------------------------------------------

def replay_scenario(rule, said_on: str = "") -> dict | None:
    """A WorkWorld scenario that fails if this rule stops holding."""
    from .policy import Rule
    r: Rule = rule
    base = {"id": f"rule-{r.id}-{r.kind}-{re.sub(r'[^a-z0-9]+', '-', (r.target or r.act or 'note').lower())[:30]}",
            "title": f"Holds: {r.render()}",
            "why": f"Said on {said_on or 'an earlier day'}: “{r.words[:200]}”",
            "twin": False,
            "setup": {"rules": [{"kind": r.kind, "act": r.act, "target": r.target,
                                 "value": r.value, "unless_asked": r.unless_asked,
                                 "words": r.words}]}}
    if r.kind == "mute" and r.target == "incident":
        base["steps"] = [{"colleague": {"who": "A colleague", "source": "teams-chat",
                                        "text": "INC0012345 is open for the booking consumer, can you check?"}}]
        base["checks"] = [{"tasks": {"count": 0}}]
    elif r.kind == "mute":
        base["steps"] = [{"colleague": {"who": "A colleague", "source": "teams-chat",
                                        "text": f"can you take a look at this {r.target.replace('_', ' ')}?"}}]
        base["checks"] = [{"tasks": {"count": 0}}]
    elif r.kind == "never" and r.act in ("send", "call"):
        tool = "prepare_to_send" if r.act == "send" else "teams_call"
        args = ({"what": "quick update: the fix is merged", "to": r.target, "channel": "teams"}
                if r.act == "send" else {"who": r.target})
        base["may_send"] = False
        base["brains"] = {"chat": [{"calls": [{"tool": tool, "args": args}], "text": "Staged."}]}
        base["steps"] = [{"say": f"tell {r.target} the fix is merged"}, {"say": "yes send it"}]
        base["checks"] = [{"no_send": True}]
    elif r.kind == "never":
        base["steps"] = [{"spawn": {"title": "small fix", "prompt": "fix the log line",
                                    "kind": "code", "workspace": "booking", "as": "job"}},
                         {"approve": "job"}]
        base["checks"] = [{"sent": {"door": "git", "text": "push", "count": 0}}]
    elif r.kind == "quiet":
        base["real_notify"] = True
        base["setup"]["clock"] = _a_quiet_moment(r.value)
        base["steps"] = [{"inbound": {"who": "A colleague", "source": "teams-chat",
                                      "text": "can you check why the build is red?", "priority": 1}}]
        base["checks"] = [{"push_lacks": "build is red"}]
    elif r.kind == "prefer" and r.act == "workspace":
        base["setup"]["workspace"] = None
        base["steps"] = [{"say": "fix the null check in the booking mapper"}]
        base["checks"] = [{"kv": {"key": "task_pipeline:1", "set": True}}]
    else:
        base["brains"] = {"chat": [{"text": "Noted."}]}
        base["steps"] = [{"say": "what's next on my list?"}]
        base["checks"] = [{"brain_prompt_contains": re.escape(r.words[:60])}]
    return base


def _a_quiet_moment(value: str) -> str:
    """A time the rule holds, for its replay: noon on its first quiet day next
    week, or just after it starts."""
    from . import policy
    q = policy.parse_quiet(value)
    now = dt.datetime.now().replace(hour=12, minute=0, second=0, microsecond=0)
    if q.get("days"):
        ahead = (q["days"][0] - now.weekday()) % 7 or 7
        return (now + dt.timedelta(days=ahead)).strftime("%Y-%m-%d %H:%M")
    start = dt.datetime.fromtimestamp(q.get("from", now.timestamp()) + 60)
    return start.strftime("%Y-%m-%d %H:%M")


def replay_dir():
    """Beside whichever database is live — data/ on his machine (gitignored,
    because a replay quotes what he actually said), a temp folder in a test or
    in the WorkWorld sandbox."""
    from pathlib import Path
    return Path(store.DB_PATH).parent / "workworld" / "replays"


def write_replay(rule, said_on: str = "") -> str:
    import yaml
    sc = replay_scenario(rule, said_on)
    if not sc:
        return ""
    folder = replay_dir()
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{sc['id']}.yaml"
    path.write_text(yaml.safe_dump({"scenarios": [sc]}, sort_keys=False, allow_unicode=True))
    return str(path)
