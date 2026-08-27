"""Making a drafted message read like Arun wrote it, and links that work.

Two faults in one real message he shared, sent to Vinish:

    Raised the fix for BEPTELIKOS-10159 — PR #1371: https://github.com/…/pull/1371.
    CANCELLED bookings (regardless of timeout flag) and bookings with
    SERVICE_DELIVERY_EXECUTION already EXECUTED now short-circuit before the
    vessel/rail date update and SEND_TO_TMS trigger. 17/17 tests pass. Please
    review when you get a chance.

**The link.** There is a full stop welded to the end of the URL. Teams either
swallows it into the href — giving a 404 — or gives up and renders the whole
thing as plain text. Both are the same failure to the person receiving it: they
cannot click the thing they are being asked to look at. This is mechanical, so
it is fixed mechanically here rather than asked of a model that will forget.

**The voice.** That is three hundred characters of release note. Arun does not
write release notes. His own last hundred-odd messages in that same chat have a
median length of TWENTY-EIGHT characters, and read "review the current code once
bro", "go ahead and create new release", "all merged bro". Nobody who has met him
would believe "Please review when you get a chance."

So the style is not invented here, and it is not my impression of him — it is
measured from his own sent messages, which Asta now keeps. If he changes how he
writes, this changes with him.
"""

from __future__ import annotations

import os
import re
import statistics
from collections import Counter

from . import store

_URL = re.compile(r"https?://[^\s<>\"']+")

#: Punctuation that must never be left welded to the end of a URL. A closing
#: bracket is excluded — it can be a genuine part of one.
_TRAILING = ".,;:!?…"


def tidy_links(text: str) -> str:
    """Make every URL in a message clickable.

    Two rules, both learned from the message above:
      - strip trailing sentence punctuation off the URL itself;
      - give the URL its own line, so nothing can end up adjacent to it.
    """
    if not text:
        return text
    out = []
    for line in text.splitlines():
        urls = _URL.findall(line)
        if not urls:
            out.append(line)
            continue
        for raw in urls:
            clean = raw.rstrip(_TRAILING)
            # A URL alone on its line is already safe; only the punctuation
            # needs removing.
            if line.strip() == raw:
                line = line.replace(raw, clean)
                continue
            before, _, after = line.partition(raw)
            after = after.lstrip(_TRAILING).strip()
            pieces = [before.strip().rstrip("—-:"), clean, after]
            line = "\n".join(p for p in pieces if p)
        out.append(line)
    return "\n".join(out)


# --- his voice, measured rather than guessed ---------------------------------

#: How he refers to himself in the stored history. Teams labels his own messages
#: with his display name, and 'me' when it renders no author at all.
_HIS_NAMES = ("arun", "me")

#: Below this there is not enough of his writing to describe a style, and a
#: confident description built on four messages would be fiction.
MIN_SAMPLE = 20

#: Tics worth naming explicitly, because a model told only "write short" still
#: writes short CORPORATE English. These are habits of his prose and travel with
#: him to anyone — deliberately NOT including any term of address.
_TICS = ("na", "u", "ur", "once", "pls", "wont", "ok")

#: Words that address the person being written to. These do NOT travel.
#:
#: "bro" appears 35 times in his history and every one of them is in one chat,
#: with Vinish. Pooling his messages and calling it "his voice" would put that
#: word in front of every colleague he has — and the fix is not to guess which
#: ones it suits. Guessing that from a name means inferring someone's gender
#: from their name, which is both wrong often and not something to automate.
#:
#: So the rule is evidence, not inference: a term of address is used with a
#: person only if he has used it with that person. No history, no term.
_ADDRESS = frozenset("""
bro bra brother dude mate buddy man boss
da machan macha anna akka bhai yaar
sir madam maam ma'am
""".split())

#: The subset it is safe to REMOVE automatically. Narrower than _ADDRESS on
#: purpose: learning what he calls someone can afford a false positive, but
#: editing his words cannot.
#:
#: Left out deliberately —
#:   "all", "team", "everyone", "guys": ordinary words far more often than
#:     vocatives. "all" was being learned as one of his terms for Vinish purely
#:     because he writes "all merged" and "all sorted", and stripping it turned
#:     "all merged bro" into "merged";
#:   "man", "boss": "the man page", "boss of the queue";
#:   "anna", "akka", "da": these are names. Cutting a leading "Anna" out of
#:     "Anna will check it" is worse than leaving a term of address in.
_STRIP = frozenset("""
bro bra brother dude mate buddy machan macha bhai yaar
sir madam maam ma'am
""".split())

#: Openers a term of address commonly follows. Used to catch "hey dude …" without
#: resorting to "second word of the line", which would eat "my brother is here".
_GREETING = "|".join(("hey", "hi", "hello", "yo", "ok", "okay", "thanks", "thx",
                      "morning", "good morning"))


def _his_messages(limit: int = 400, chat: str = "") -> list[str]:
    rows = store.teams_messages(chat, limit=limit) if chat \
        else store.teams_messages(limit=limit)
    out = []
    for r in rows:
        sender = (r.get("sender") or "").strip().lower()
        if not sender.startswith(_HIS_NAMES):
            continue
        text = (r.get("text") or "").strip()
        # Reaction rows and quoted blocks are Teams chrome, not his prose.
        if not text or "reaction" in text.lower() or "\n" in text[:60]:
            continue
        out.append(text)
    return out


def address_terms(chat: str, limit: int = 400) -> list[str]:
    """Terms of address he has ACTUALLY used with this person, commonest first.

    Empty for anyone he has no history with — which is the safe answer, not a
    gap to fill with the term he happens to use most often elsewhere.
    """
    if not chat:
        return []
    found = Counter()
    for m in _his_messages(limit, chat):
        for w in re.findall(r"[a-z']+", m.lower()):
            if w in _ADDRESS:
                found[w] += 1
    return [w for w, _ in found.most_common(4)]


def _mask_urls(text: str) -> tuple[str, list[str]]:
    """Hide URLs so word-level edits cannot corrupt a link (github.com/bro/x)."""
    held: list[str] = []

    def keep(m):
        held.append(m.group(0))
        return f"\x00{len(held) - 1}\x00"

    return _URL.sub(keep, text), held


def _unmask_urls(text: str, held: list[str]) -> str:
    for i, url in enumerate(held):
        text = text.replace(f"\x00{i}\x00", url)
    return text


def fit_address(text: str, chat: str) -> str:
    """Drop terms of address this person has never been addressed with.

    The backstop behind the prompt guidance. A model that has been told the rule
    still slips, and the cost of slipping is a message to a colleague that calls
    them something he never has — which is exactly the sort of thing he would
    have to apologise for rather than simply correct.

    Whole words only: "brother", "broadcast" and a URL containing "bro" are all
    left alone.
    """
    if not text:
        return text
    allowed = set(address_terms(chat))
    masked, held = _mask_urls(text)
    out_lines = []
    for line in masked.splitlines():
        # Strip a term only where it is genuinely an address: at either end of
        # the line, or fenced by commas. Mid-sentence it is usually a real word
        # ("the man page", "boss of the queue"), and rewriting that is worse.
        for term in sorted(_STRIP - allowed, key=len, reverse=True):
            t = re.escape(term)
            line = re.sub(rf"^\s*{t}\b[\s,]*", "", line, flags=re.I)
            line = re.sub(rf"[\s,]*\b{t}\s*(?=[.!?]|$)", "", line, flags=re.I)
            line = re.sub(rf",\s*{t}\s*,", ", ", line, flags=re.I)
            # "hey dude", "ok bro" — vocative, but not at either end. Anchored to
            # an actual greeting rather than to position: "my brother is
            # visiting" has the term in the same slot and must survive.
            line = re.sub(rf"^(\s*(?:{_GREETING})\b[\s,]*){t}\b[\s,]*", r"\1",
                          line, flags=re.I)
        out_lines.append(re.sub(r"[ \t]{2,}", " ", line).rstrip())
    fitted = _unmask_urls("\n".join(out_lines), held).strip()
    # A draft whose entire content was the term of address strips to nothing,
    # and sending an empty message is a worse failure than sending the wrong
    # word. Hand back the original — Arun sees every draft before it goes.
    return fitted or text


def profile(limit: int = 400, chat: str = "") -> dict:
    """Measured facts about how he writes. {} when there is not enough to say.

    Length and casing are habits of his and pooled across everyone. Terms of
    address are a property of the RELATIONSHIP, so they come only from `chat`
    and are empty when he has no history with that person.
    """
    msgs = _his_messages(limit)
    if len(msgs) < MIN_SAMPLE:
        return {}
    lengths = sorted(len(m) for m in msgs)
    words = Counter()
    for m in msgs:
        words.update(re.findall(r"[a-z]+", m.lower()))
    tics = [t for t in _TICS if words.get(t, 0) >= max(2, len(msgs) // 40)]
    lower_starts = sum(1 for m in msgs if m[:1].islower())
    return {
        "samples": len(msgs),
        "median_chars": int(statistics.median(lengths)),
        "p90_chars": lengths[int(len(lengths) * 0.9)],
        "lowercase_start_pct": round(100 * lower_starts / len(msgs)),
        "tics": tics,
        "chat": chat,
        "address": address_terms(chat, limit),
        # Short, recent, representative — shown to the model as evidence rather
        # than as a description it has to take on trust.
        "examples": [m for m in msgs[-40:] if 8 <= len(m) <= 70][-8:],
    }


#: How far past his own p90 a draft may run before it stops reading as him. Not a
#: hard cap — 1.5× leaves room for a genuinely longer message that has a reason.
LENGTH_SLACK = float(os.environ.get("ASTA_DRAFT_LENGTH_SLACK", "1.5"))


def too_long(text: str, chat: str = "") -> str:
    """Why this draft does not read like him, or "" when it is fine.

    The gap the capability bench found on its first run: `profile()` MEASURES how
    short he writes and `guidance()` hands that to the model — and then nothing
    checks. Terms of address have `fit_address` as a backstop precisely because a
    model told the rule still slips; length had the rule and no backstop, so a
    model that ignored it put a paragraph out in his name and nothing noticed.

    It reports rather than truncates, deliberately. Cutting a message to length
    can remove the ask, and a polite fragment that no longer says what it wanted
    is worse than one that runs long. He sees every draft before it goes, so the
    useful thing is that the overrun is VISIBLE at that moment.
    """
    if not (text or "").strip():
        return ""
    p = profile(chat=chat)
    if not p:
        return ""           # too little history to describe a style, so no claim
    ceiling = int(p["p90_chars"] * LENGTH_SLACK)
    if len(text) <= ceiling:
        return ""
    return (f"{len(text)} characters — he writes {p['median_chars']} typically and "
            f"{p['p90_chars']} at his longest ({p['samples']} messages). This reads "
            f"like a bot wrote it.")


def guidance(chat: str = "") -> str:
    """The block to hand a model that is about to draft a message AS Arun.

    Empty string when his history is too thin to describe — better to write
    plainly than to impersonate him from a guess.
    """
    p = profile(chat=chat)
    if not p:
        return ""
    tics = ", ".join(f'"{t}"' for t in p["tics"]) or "none pronounced"
    examples = "\n".join(f"    {e}" for e in p["examples"])
    if p["address"]:
        known = ", ".join(f'"{a}"' for a in p["address"])
        address_rule = (
            f"  - He addresses {chat} as {known} — that is attested in their own "
            f"chat, so it is safe here.\n"
            f"    Do not carry it to anyone else.")
    else:
        who = chat or "this person"
        address_rule = (
            f"  - NO term of address for {who}. Not \"bro\", not \"dude\", not "
            f"\"sir\", nothing.\n"
            f"    He has no history of addressing them that way, and terms like "
            f"these belong to a\n"
            f"    specific relationship — using one because it is common in his "
            f"OTHER chats would\n"
            f"    put a word in front of a colleague he has never used with them. "
            f"Just say the thing.")
    return f"""
## Arun's voice on Teams/WhatsApp
Measured from {p['samples']} of his own sent messages — not a guess. A draft for a
PERSON on chat must pass as his. (A Jira comment, PR body or email is different:
those are written for a team and stay plain and professional. This is chat only.)

  - LENGTH: median {p['median_chars']} characters, 90% under {p['p90_chars']}. One or two short lines.
    If your draft is a paragraph, it is wrong. Rewrite it shorter.
  - {p['lowercase_start_pct']}% of his messages start lowercase. Do not tidy that up.
  - His words: {tics}.
{address_rule}
  - Never: a greeting, a sign-off, "Please review when you get a chance", "Let me
    know if you need anything else", em dashes, bullet lists, bold, or release-note
    phrasing. He states the thing and stops.
  - Detail is what the PR or ticket is for. The message says what it is and what
    he wants back — not what changed, not the test count, not the class names.
  - A URL goes on its own line with NOTHING after it, or it stops being clickable.

His actual messages, for tone:
{examples}
""".rstrip()
