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
#: writes short CORPORATE English.
_TICS = ("bro", "na", "u", "ur", "once", "pls")


def _his_messages(limit: int = 400) -> list[str]:
    rows = store.teams_messages(limit=limit)
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


def profile(limit: int = 400) -> dict:
    """Measured facts about how he writes. {} when there is not enough to say."""
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
        # Short, recent, representative — shown to the model as evidence rather
        # than as a description it has to take on trust.
        "examples": [m for m in msgs[-40:] if 8 <= len(m) <= 70][-8:],
    }


def guidance() -> str:
    """The block to hand a model that is about to draft a message AS Arun.

    Empty string when his history is too thin to describe — better to write
    plainly than to impersonate him from a guess.
    """
    p = profile()
    if not p:
        return ""
    tics = ", ".join(f'"{t}"' for t in p["tics"]) or "none pronounced"
    examples = "\n".join(f"    {e}" for e in p["examples"])
    return f"""
## Arun's voice on Teams/WhatsApp
Measured from {p['samples']} of his own sent messages — not a guess. A draft for a
PERSON on chat must pass as his. (A Jira comment, PR body or email is different:
those are written for a team and stay plain and professional. This is chat only.)

  - LENGTH: median {p['median_chars']} characters, 90% under {p['p90_chars']}. One or two short lines.
    If your draft is a paragraph, it is wrong. Rewrite it shorter.
  - {p['lowercase_start_pct']}% of his messages start lowercase. Do not tidy that up.
  - His words: {tics}.
  - Never: a greeting, a sign-off, "Please review when you get a chance", "Let me
    know if you need anything else", em dashes, bullet lists, bold, or release-note
    phrasing. He states the thing and stops.
  - Detail is what the PR or ticket is for. The message says what it is and what
    he wants back — not what changed, not the test count, not the class names.
  - A URL goes on its own line with NOTHING after it, or it stops being clickable.

His actual messages, for tone:
{examples}
""".rstrip()
