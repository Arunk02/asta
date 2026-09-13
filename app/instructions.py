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


def compile(text: str) -> Candidate | None:  # noqa: A001 — the word for what it does
    """The rule a standing instruction states, or None when it isn't one."""
    t = " ".join((text or "").replace("’", "'").split())
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
    elif r.kind == "prefer" and r.act == "workspace":
        base["setup"]["workspace"] = None
        base["steps"] = [{"say": "fix the null check in the booking mapper"}]
        base["checks"] = [{"kv": {"key": "task_pipeline:1", "set": True}}]
    else:
        base["brains"] = {"chat": [{"text": "Noted."}]}
        base["steps"] = [{"say": "what's next on my list?"}]
        base["checks"] = [{"brain_prompt_contains": re.escape(r.words[:60])}]
    return base


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
