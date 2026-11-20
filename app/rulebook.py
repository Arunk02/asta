"""His rules, said any way he says them, held by code everywhere.

20 Nov: "Hereafter handle all Priya chats one to one, going forward, without my
approval" and "Same goes to Dev as well" were both answered "Noted —
auto-handled" by the chat brain, and nothing was stored: the only path that
recorded a permission wanted "for today" and her full Teams name. Twenty
minutes later "Don't send hedwig alert" met a gate with no kind of rule that
could hold it, so a brain guessed it meant the CI-failure draft waiting and
dropped that instead. Every new phrasing was a new bug, because each kind of
rule had its own regex in its own file and anything else fell to a brain.

Now one reader runs on his message before any brain:

  1. rules first: the shapes he actually uses parse with no model;
  2. the long tail goes to one cheap completion with his live rules as context,
     and code validates the answer: a person must be one of his 1:1 chats AND
     named in his words, a muted topic must be words he used (a typo is
     corrected only to a word his own notifications contain);
  3. the result is APPLIED — his words are the approval — and the reply shows
     the stored rule, its id, how to end it, and what it would have caught.

What it holds:

  allow     answer one 1:1 contact without asking     authority grant (capped, revocable)
  mute      don't push me alerts about X              policy rule mute/push, checked in notify
  withdraw  any live rule or permission, by name      "stop handling Priya", "send hedwig alerts again"

Quiet time, defaults, "never message X" and notes keep their front-desk path
(app/instructions.py); withdrawing them by name works here too.
"""

from __future__ import annotations

import asyncio
import contextlib
import difflib
import json
import re
import time
from dataclasses import dataclass, field

from . import store

# --- the words -------------------------------------------------------------------

_STANDING = re.compile(
    r"\b(?:going\s+forward|from\s+now(?:\s+on)?|here\s*after|henceforth|onwards?|always|"
    r"any\s*more|in\s+future|every\s*time|whenever|till\s+further\s+notice|"
    r"until\s+i\s+say|permanently|for\s+good)\b", re.I)
_TODAY = re.compile(r"\b(?:(?:just\s+|only\s+)?for\s+today|today\s+only|for\s+the\s+day|"
                    r"(?:till|until)\s+(?:evening|tonight|eod|end\s+of\s+(?:the\s+)?day))\b", re.I)
_TOMORROW = re.compile(r"\b(?:till|until|through)\s+(?:tomorrow|tmrw?|tmr)\b", re.I)
_WEEK = re.compile(r"\b(?:for\s+(?:this|the)\s+week|this\s+week|till\s+(?:the\s+)?weekend)\b", re.I)
_WAIVER = re.compile(
    r"\b(?:without\s+(?:my\s+)?(?:approval|asking(?:\s+me)?|my\s+yes|checking\s+with\s+me)|"
    r"(?:don'?t|do\s+not|dont|no)\s+(?:need|require[ds]?|required)?\s*(?:of\s+)?(?:me\s+|my\s+)?approval|"
    r"no\s+need\s+(?:of|for)\s+(?:my\s+)?approval|no\s+approval|"
    r"(?:talk|respond|reply|answer|speak|chat)\s+(?:(?:with|to)\s+(?:her|him|them|\w+)\s+)?directly)\b",
    re.I)
#: Taking a person's chats over. Plain "reply"/"respond" only count beside a waiver:
#: "always reply to Priya in English" is about style, not permission.
_HANDLE = re.compile(
    r"\b(?:handl\w*|take\s+care\s+of|take\s+over|manag\w*|deal\s+with|"
    r"auto[- ]?(?:repl|respond|answer|handl)\w*)\b", re.I)
#: "whatever Pat asks", "all Priya chats": a standing scope, not one message.
_ALL = re.compile(r"\b(?:whatever|all+|any|every)\b.{0,40}\b(?:asks?|chats?|messages?|quer(?:y|ies)|"
                  r"questions?|interactions?|conversations?|pings?)\b", re.I)
#: "send the same to Dev as well" is a one-off send, never "same goes" for a rule.
_ONE_OFF_SEND = re.compile(r"\b(?:send|share|forward|post|tell|ping|message|reply|notify)\b", re.I)
_NEG_SEND = re.compile(r"\b(?:don'?t|dont|do\s+not|never)\s+(?:reply|respond|send|message|talk|answer)\b", re.I)
_SAME = re.compile(r"\b(?:same(?:\s+goes)?|as\s+well|too|also|likewise|similarly)\b", re.I)

_NEG = r"(?:don'?t|dont|do\s+not|never|stop|no\s+more|no\s+need\s+to|won'?t|wont|will\s+not|shouldn'?t|not\s+to|avoid)"
_PUSH_VERB = r"(?:send|push|notify|forward|alert|ping|tell|show|disturb|bother|bug|escalat)\w*"
_NEG_PUSH = re.compile(_NEG + r"\s+(?:\w+\s+){0,3}?" + _PUSH_VERB + r"\b", re.I)
_IGNORE = re.compile(r"\b(?:ignore|mute|silence|skip|suppress|drop)\b", re.I)
_NOUN = re.compile(r"\b(?:alerts?|alarms?|notifications?|notifs?|pings?|mails?|emails?|"
                   r"updates?|reminders?|pushes)\b", re.I)

#: Ending a rule. Each list only ever ends a rule that is live AND named.
_WD_ANY = re.compile(r"\b(?:withdraw|revoke|take\s+back|take\s+away|lift)\b", re.I)
#: Everyday verbs ("drop the draft to Priya") end a rule only beside a rule word or an id.
_WD_WEAK = re.compile(r"\b(?:cancel|remove|end|drop|undo|delete)\b", re.I)
_RULE_WORD = re.compile(r"\b(?:permissions?|stand[- ]?ins?|access|rules?|mutes?|handling|auto[- ]?repl\w*)\b"
                        r"|\b[PR]\s*#?\d{1,4}\b", re.I)
_WD_GRANT = re.compile(
    r"\b(?:stop|quit|no\s+longer)\s+(?:\w+\s+)?(?:handl|answer|repl|respond|auto|talk|chat)\w*|"
    r"\bask\s+me\s+(?:first|before)|\bback\s+to\s+asking|\b(?:need|get|wait\s+for)\s+my\s+(?:approval|yes)", re.I)
_WD_GRANT_DONT = re.compile(r"\b(?:don'?t|dont|do\s+not)\s+(?:\w+\s+)?(?:handl|answer|repl|respond|talk)\w*", re.I)
_WD_MUTE = re.compile(
    r"\bunmute\b|\b(?:resume|start|restart)\s+(?:send|push|notify)\w*|"
    r"\b(?:send|push|notify|show|forward)\w*\b.*\bagain\b|\bstop\s+(?:muting|ignoring|blocking)\b", re.I)

#: Words that never name what a rule is about.
_FILLER = set("""
a an the me my your you u i it its this that these those them they he she him her his
any anything all every some about on of for from to in at by with as and or but so also
still just only please pls hey hi said say told have has had is are was were be been will
would should can could do does did don't dont won't wont not no never more anymore again
going forward hereafter henceforth now from today tomorrow tmr tmrw till until
alerts alert alarm alarms notification notifications notif notifs ping pings mail mails email emails
update updates reminder reminders pushes push send sending sent notify forward ignore mute silence
skip suppress drop stop avoid need tell show disturb bother escalate related regarding
there here then than what which who when one ones kind type sort like whatever
don won shouldn didn doesn isn wasn aren can't cant ll re ve hereafter
""".split())

ACTS_ALLOW = ("send",)
PER_DAY = 50


@dataclass
class Intent:
    op: str                                  # add | withdraw | ask
    effect: str = ""                         # allow | mute
    people: tuple[str, ...] = ()
    match: str = ""
    until: float | None = None
    refs: tuple[str, ...] = ()               # withdraw: "P2", "R11"
    question: str = ""
    words: str = ""
    by: str = "rules"


@dataclass
class Applied:
    line: str
    intent: Intent
    people: tuple[str, ...] = field(default_factory=tuple)


def _norm(text: str) -> str:
    return " ".join((text or "").replace("’", "'").replace("‘", "'").split())


def _tokens(text: str) -> list[str]:
    return re.findall(r"[a-z0-9]+", (text or "").lower())


# --- when it ends -----------------------------------------------------------------

def _end_of(day_offset: int, now: float | None = None) -> float:
    d = time.localtime((time.time() if now is None else now) + day_offset * 86400)
    return time.mktime((d.tm_year, d.tm_mon, d.tm_mday, 23, 59, 0, 0, 0, -1))


def until_from(text: str, now: float | None = None) -> float | None:
    """None = until he withdraws it. Only what he said sets an end."""
    if _TODAY.search(text):
        return _end_of(0, now)
    if _TOMORROW.search(text):
        return _end_of(1, now)
    if _WEEK.search(text):
        wd = time.localtime(time.time() if now is None else now).tm_wday
        return _end_of(6 - wd, now)
    return None


def _until_word(value: str, now: float | None = None) -> float | None:
    v = (value or "").strip().lower()
    if v in ("today",):
        return _end_of(0, now)
    if v in ("tomorrow",):
        return _end_of(1, now)
    m = re.fullmatch(r"(\d{4})-(\d{2})-(\d{2})", v)
    if m:
        return time.mktime((int(m[1]), int(m[2]), int(m[3]), 23, 59, 0, 0, 0, -1))
    return None


# --- who he named -------------------------------------------------------------------

def contacts() -> list[str]:
    """His 1:1 chats: a person's chat is one where that person wrote."""
    from . import chat_watch
    with store._connect() as conn:
        people = [r[0] for r in conn.execute(
            "SELECT DISTINCT chat FROM teams_messages WHERE chat <> '' AND sender = chat").fetchall()]
    return [c for c in people if not any(ch in c for ch in (",", "+")) and not chat_watch.is_from_him(c)]


def _name_words(name: str) -> set[str]:
    from . import answers
    return {t for t in answers.name_tokens(name) if len(t) >= 3}


def people_in(text: str) -> tuple[list[str], str]:
    """(the contacts his words name, a question when a name fits several)."""
    said = [t for t in _tokens(text) if len(t) >= 3 and t not in _FILLER]
    pool = contacts()
    found: list[str] = []
    for tok in dict.fromkeys(said):
        hits = [c for c in pool if tok in _name_words(c)]
        if len(hits) > 1:                     # "Kumar": does another word narrow it?
            narrowed = [c for c in hits if len(_name_words(c) & set(said)) > 1]
            if len(narrowed) == 1:
                hits = narrowed
            elif not any(h in found for h in hits):
                return found, "Which one — " + " or ".join(sorted(hits)[:4]) + "?"
            else:
                continue
        for h in hits:
            if h not in found:
                found.append(h)
    return found, ""


def names_person(words: str, person: str) -> bool:
    """His words name this contact: in full, or by a name that fits only them.

    "handle Other Person going forward" shares "person" with Test Person and
    names someone else; a partial name counts only when it resolves to them.
    """
    full = " ".join(_tokens(person))
    if full and re.search(r"(?<![a-z0-9])" + re.escape(full) + r"(?![a-z0-9])", " ".join(_tokens(words))):
        return True
    found, _ = people_in(words)
    return person in found


# --- what a mute is about -----------------------------------------------------------

def _recent_words(days: int = 7) -> set[str]:
    with store._connect() as conn:
        rows = conn.execute("SELECT text FROM notifications WHERE created_at >= ?",
                            (time.time() - days * 86400,)).fetchall()
    out: set[str] = set()
    for r in rows:
        out.update(t for t in _tokens(r[0]) if len(t) >= 3)
    return out


def _fix_typo(word: str, known: set[str]) -> str:
    """'headwig' → 'hedwig', only toward a word his own notifications contain."""
    if word in known or not known:
        return word
    close = difflib.get_close_matches(word, sorted(known), n=1, cutoff=0.8)
    return close[0] if close else word


def topic_of(text: str, people: list[str]) -> str:
    """The words a mute is about, with everything that is not a subject removed."""
    person_words = set().union(*(_name_words(p) for p in people)) if people else set()
    kept = [t for t in _tokens(text)
            if len(t) >= 2 and t not in _FILLER and t not in person_words and not t.isdigit()]
    kept = list(dict.fromkeys(kept))
    if not kept or len(kept) > 4:
        return ""
    known = _recent_words()
    fixed = list(dict.fromkeys(_fix_typo(t, known) for t in kept))
    # A word no alert of his has ever used cannot help pick alerts out, and
    # requiring it would mute nothing ("ignore ouhsing hedwig alerts").
    seen = [t for t in fixed if t in known]
    return " ".join(seen or fixed)


def would_have_caught(target: str, days: int = 7) -> list[str]:
    """What a mute on `target` would have held in the last week: the proof he reads."""
    from . import policy
    with store._connect() as conn:
        rows = conn.execute("SELECT text, level FROM notifications WHERE created_at >= ? ORDER BY id DESC",
                            (time.time() - days * 86400,)).fetchall()
    return [r[0] for r in rows if _was_alert(r[0], r[1]) and policy._words_in(target, f"{r[1]} {r[0]}")]


#: How app/outlook.alert_message opens: the bell keeps no kind, so the preview
#: recognises an alert push by the words it always starts with.
_ALERT_HEAD = re.compile(r"^\s*(?:⚠️ Still broken|🚨 Broken now|🟢 Recovered)")


def _was_alert(text: str, level: str) -> bool:
    from . import policy
    return policy.is_alert(level) or (level == "outlook" and bool(_ALERT_HEAD.match(text or "")))


# --- live rules, as one list ------------------------------------------------------

def live() -> list[dict]:
    """Every rule and permission in force, each with a ref (P2, R11) and its names."""
    from . import authority, policy
    out = [{"ref": f"P{g.id}", "kind": "allow", "who": g.target, "text": g.render()}
           for g in authority.grants()]
    out += [{"ref": f"R{r.id}", "kind": f"{r.kind}/{r.act}" if r.act else r.kind,
             "who": r.target, "text": r.render()} for r in policy.rules() if r.kind != "quiet"]
    return out


def summary() -> str:
    from . import policy
    rows = live()
    quiet = [r for r in policy.rules("quiet")]
    if not rows and not quiet:
        return "No standing rules or permissions yet."
    lines = [f"  {r['ref']}. {r['text']}" for r in rows]
    lines += [f"  R{q.id}. {q.render()}" for q in quiet]
    return ("Your standing rules:\n" + "\n".join(lines)
            + "\nSay “drop R11” / “revoke P2”, or just “stop handling <name>”, to end one.")


def _withdraw_targets(text: str) -> list[dict]:
    """Live rules his words name — a person by any name, a topic by all its words."""
    from . import policy
    hits = []
    toks = set(_tokens(text))
    for r in live():
        if r["kind"] == "allow":
            if _name_words(r["who"]) & toks:
                hits.append(r)
        elif r["who"] and policy._words_in(r["who"], text):
            hits.append(r)
    for ref in re.findall(r"\b([PR])\s*#?(\d{1,4})\b", text, re.I):
        want = f"{ref[0].upper()}{ref[1]}"
        hits += [r for r in live() if r["ref"] == want and r not in hits]
    return hits


# --- rules first --------------------------------------------------------------------

def _last() -> dict:
    with contextlib.suppress(Exception):
        d = json.loads(store.kv_get("rulebook:last") or "{}")
        if time.time() - float(d.get("at") or 0) < 3600:
            return d
    return {}


def parse(text: str, now: float | None = None) -> Intent | None:
    """The rule his words state, by the shapes he uses — no model. None: not one of them."""
    t = _norm(text)
    if not t or len(t) > 400:
        return None
    if t.endswith("?") and not _WAIVER.search(t):
        return None
    until = until_from(t, now)

    # Ending a rule he has: only ever a rule that is live AND named.
    wd_any = bool(_WD_ANY.search(t) or (_WD_WEAK.search(t) and _RULE_WORD.search(t)))
    wd_grant = bool(_WD_GRANT.search(t)) or bool(
        _WD_GRANT_DONT.search(t) and (_STANDING.search(t) or _WAIVER.search(t)))
    wd_mute = bool(_WD_MUTE.search(t))
    if wd_any or wd_grant or wd_mute:
        targets = [r for r in _withdraw_targets(t)
                   if wd_any or (r["kind"] == "allow" and wd_grant)
                   or (r["kind"] != "allow" and wd_mute)]
        if targets:
            return Intent("withdraw", refs=tuple(r["ref"] for r in targets), words=t)

    # Muting what reaches his phone: a negated push or "ignore", about alerts.
    if (_NEG_PUSH.search(t) or _IGNORE.search(t)) and _NOUN.search(t):
        people, _ = people_in(t)
        topic = topic_of(t, people)
        if topic and not people:
            return Intent("add", "mute", match=topic, until=until, words=t)

    # Standing in for him with a person.
    people, ask = people_in(t)
    lasting = (_STANDING.search(t) or _TODAY.search(t) or _TOMORROW.search(t) or _WEEK.search(t)
               or _ALL.search(t))
    allowish = lasting and (_WAIVER.search(t) or _HANDLE.search(t))
    if allowish and not _NEG_SEND.search(t):
        if ask:
            return Intent("ask", "allow", question=ask, words=t)
        if people:
            return Intent("add", "allow", people=tuple(people), until=until, words=t)
    # "Same goes to Dev": the rule he just gave, for someone else.
    last = _last()
    if _SAME.search(t) and last and people and last.get("effect") == "allow" \
            and not _NEG_SEND.search(t) and not _ONE_OFF_SEND.search(t):
        return Intent("add", "allow", people=tuple(people),
                      until=until if until is not None else last.get("until"), words=t)
    return None


def might_be_rule(text: str) -> bool:
    """Cheap gate before the model: only these ever cost a completion."""
    t = _norm(text)
    if not t or len(t) > 400:
        return False
    return bool(_STANDING.search(t) or _WAIVER.search(t) or _TODAY.search(t)
                or ((_NEG_PUSH.search(t) or _IGNORE.search(t))
                    and (_NOUN.search(t) or re.search(r"\b(?:ci|failures?|incidents?|builds?)\b", t, re.I)))
                or re.search(r"\bunmute\b", t, re.I)
                or ((_WD_ANY.search(t) or _WD_WEAK.search(t) or _WD_GRANT.search(t)
                     or _WD_MUTE.search(t) or _SAME.search(t)
                     or re.search(r"\b(?:i\s+said|not\s+the)\b", t, re.I)) and _withdraw_targets(t)))


# --- the long tail: one cheap completion, checked by code ---------------------------

_PROMPT = """You read ONE message Arun sent his assistant and decide whether it sets, \
changes or withdraws a STANDING rule about what the assistant does on its own. Reply \
with one JSON object and nothing else.

Rules in force:
{rules}

His previous messages (oldest first):
{recent}

Message: {text}

JSON keys:
  "op": "none" | "add" | "withdraw" | "amend" | "ask"
  "effect": "allow" (answer a person's 1:1 Teams chats without his approval) | "mute" (don't push him alerts/notifications about a topic or source)
  "people": [names exactly as he wrote them]
  "match": "1-3 words naming the alerts to mute, as he wrote them, e.g. hedwig"
  "until": "today" | "tomorrow" | "forever" | "YYYY-MM-DD"
  "ids": [refs like "P2", "R11" this ends or replaces]
  "question": "one short question, only when it is a rule but unclear"
"none" when the message is a one-off (about a draft, a task, one message) or not a rule.
"amend" when he corrects a rule: the ids it replaces AND the new rule."""

#: The one completion. A module attribute so the bench and tests can stand in.
_complete = None


async def _model(text: str, cid: str = "") -> dict | None:
    from . import memory
    complete = _complete or memory.cheap_complete
    recent = ""
    if cid:
        with contextlib.suppress(Exception):
            rows = [r for r in store.list_ui_messages(cid)[-8:] if r["role"] == "user"][-3:]
            recent = "\n".join(f"- {(r.get('content') or '')[:200]}" for r in rows)
    rules = "\n".join(f"{r['ref']} {r['kind']}: {r['text']}" for r in live()) or "(none)"
    prompt = _PROMPT.format(rules=rules, recent=recent or "(none)", text=json.dumps(text))
    try:
        out = await asyncio.wait_for(complete(prompt, max_tokens=300, paid_ok=True), timeout=20)
    except Exception:                                            # noqa: BLE001
        return None
    m = re.search(r"\{.*\}", out or "", re.S)
    if not m:
        return None
    with contextlib.suppress(ValueError):
        return json.loads(m.group(0))
    return None


def from_model(d: dict, text: str, now: float | None = None) -> Intent | None:
    """Keep only what code can stand behind: names he used, topics he wrote."""
    if not isinstance(d, dict):
        return None
    op = str(d.get("op") or "none").lower()
    if op == "none" or op not in ("add", "withdraw", "amend", "ask"):
        return None
    if op == "ask":
        q = str(d.get("question") or "").strip()
        return Intent("ask", question=q[:200], words=text, by="model") if q else None
    have = {r["ref"] for r in live()}
    refs = tuple(str(x).upper() for x in (d.get("ids") or []) if str(x).upper() in have)
    if op == "withdraw":
        return Intent("withdraw", refs=refs, words=text, by="model") if refs else None
    effect = str(d.get("effect") or "").lower()
    until = _until_word(str(d.get("until") or ""), now) if d.get("until") not in (None, "forever") \
        else None
    if until is None:
        until = until_from(text, now)
    intent: Intent | None = None
    if effect == "allow":
        named = [p for p in (d.get("people") or []) if names_person(text, str(p))]
        people: list[str] = []
        for p in named:
            found, ask = people_in(str(p))
            if ask:
                return Intent("ask", "allow", question=ask, words=text, by="model")
            people += [f for f in found if f not in people]
        if people:
            intent = Intent("add", "allow", people=tuple(people), until=until, words=text, by="model")
    elif effect == "mute":
        said = set(_tokens(text))
        known = _recent_words()
        words = []
        for w in _tokens(str(d.get("match") or "")):
            if w in _FILLER:
                continue
            if w in said:
                words.append(w)
            elif any(difflib.SequenceMatcher(None, w, s).ratio() >= 0.8 for s in said) and w in known:
                words.append(w)                 # his typo, corrected to a word his alerts use
        if words and len(words) <= 4:
            intent = Intent("add", "mute", match=" ".join(words), until=until, words=text, by="model")
    if intent is None:
        return None
    if op == "amend":
        intent.refs = refs
    return intent


async def read(text: str, cid: str = "", now: float | None = None) -> Intent | None:
    """Rules first; the model only for what passed the cheap gate and parsed as nothing."""
    intent = parse(text, now)
    if intent is not None or not might_be_rule(text):
        return intent
    d = await _model(_norm(text), cid)
    return from_model(d, _norm(text), now) if d else None


# --- applying it ------------------------------------------------------------------

def _until_text(end: float | None) -> str:
    if not end:
        return "until you withdraw it"
    if time.strftime("%Y-%m-%d", time.localtime(end)) == time.strftime("%Y-%m-%d"):
        return f"until {time.strftime('%H:%M', time.localtime(end))} today"
    return f"until {time.strftime('%a %d %b %H:%M', time.localtime(end))}"


def _first(name: str, said: str = "") -> str:
    """The name he calls them by: the one his words used, else their first name."""
    words = sorted(_name_words(name), key=lambda w: name.lower().find(w))
    his = [w for w in words if w in set(_tokens(said))]
    pick = (his or words or [name])[0]
    return pick.capitalize()


def _withdraw(refs: tuple[str, ...]) -> list[str]:
    from . import authority, policy
    before = {r["ref"]: r["text"] for r in live()}
    ended = []
    for ref in refs:
        n = int(ref[1:])
        ok = authority.revoke(n) if ref.startswith("P") else policy.drop(n)
        if ok:
            ended.append(f"{ref} — {before.get(ref, '')}")
    return ended


def apply(intent: Intent) -> Applied:
    """Do what he said, and say exactly what is now stored — never just "noted"."""
    from . import authority, policy
    if intent.op == "ask":
        return Applied("❓ " + intent.question, intent)
    lines: list[str] = []
    if intent.refs:
        ended = _withdraw(intent.refs)
        if ended:
            lines.append("↩️ Ended:\n" + "\n".join(f"  {e}" for e in ended)
                         + ("\nThat goes back to asking you first." if any(
                             e.startswith("P") for e in ended) else ""))
        if intent.op == "withdraw":
            store.record_outcome("rulebook", "withdrew", detail=", ".join(intent.refs)[:200])
            return Applied("\n".join(lines) or "Nothing live matched that — say “my rules”.", intent)
    people: tuple[str, ...] = ()
    if intent.effect == "allow":
        for person in intent.people:
            g = authority.grant("send", person, per_day=PER_DAY, words=intent.words,
                                expires_at=intent.until)
            lines.append(f"✅ P{g.id}: I'll answer *{person}* 1:1 directly — no approval needed, "
                         f"{_until_text(g.expires_at)}. I'll tell you what went. "
                         f"“stop handling {_first(person, intent.words)}” ends it.")
        people = intent.people
    elif intent.effect == "mute":
        r = policy.add("mute", "push", intent.match, words=intent.words, expires_at=intent.until)
        caught = would_have_caught(r.target)
        proof = (f"Last 7 days it would have held {len(caught)}, e.g. “{caught[0].splitlines()[0][:90]}”."
                 if caught else f"Nothing in the last 7 days mentions “{r.target}” — check the word.")
        lines.append(f"🔕 R{r.id}: {r.render()} — {_until_text(r.expires_at)}. {proof} "
                     f"Everything else still comes. “send {r.target} alerts again” ends it.")
    store.kv_set("rulebook:last", json.dumps({"effect": intent.effect, "until": intent.until,
                                              "at": time.time()}))
    store.record_outcome("rulebook", f"{intent.op} {intent.effect}", subject=intent.by,
                         detail=(", ".join(intent.people) or intent.match)[:200])
    return Applied("\n".join(lines), intent, people)


async def handle(text: str, cid: str = "") -> Applied | None:
    """His message, if it is a rule: read, applied, answered. None for anything else."""
    intent = await read(text, cid)
    if intent is None:
        return None
    return apply(intent)
