"""What is this, and does it actually need Arun? — one policy, every channel.

Two separate bugs live here, and they have the same root: the watchers treated a
rendered *string* as if it were an *identity*, and treated "a human sent it" as if
it were "it needs you".

**Stable identity.** The Teams activity feed renders each item with a relative
timestamp ("2m", "1h", "Yesterday"). The old dedup key was the first 150 characters
of that rendering — so the same message produced a different key on every poll as
the clock moved, looked brand new, and got pushed again. And again. `stable_key`
strips everything that changes on its own, so a message keys the same at 09:00 and
at 17:00.

**Precise verdict.** "From a human" is not the same as "needs you". Someone's
passing thought and someone blocking on your approval both arrive as mail from a
person, and the old filter pushed both identically, each with a 180-character
preview. So every item now gets a `Verdict`: one line saying *what it is*, and an
honest `action` flag. FYI items are stated once, quietly, and ask nothing of him;
only genuine asks interrupt and say what is wanted.

Rules settle the clear cases for free. The local model is consulted ONLY for the
genuinely ambiguous middle, so triage costs no paid tokens and still degrades to
pure rules when the local model is not running.
"""

from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass(frozen=True)
class Verdict:
    """What an inbound item is, and whether it wants something from Arun."""

    action: bool          # True → he has to do something; False → pure FYI
    why: str              # the short reason, so a wrong call is debuggable
    one_line: str         # the precise single line he reads on his phone

    def render(self) -> str:
        """One line, with a marker that makes the ask/no-ask split scannable."""
        return f"{'🔴' if self.action else '·'} {self.one_line}"


# --- stable identity --------------------------------------------------------

# Everything that changes by itself while the message does not. Relative ages
# ("2m", "3 hours ago", "Yesterday"), clock times, and dates are all rendering,
# not identity — keying on them is what caused the endless re-notifications.
_VOLATILE = re.compile(
    r"\b\d+\s*(?:s|m|h|d|w|sec|min|mins|minute|minutes|hour|hours|day|days|week|weeks)\b"
    r"|\b(?:just now|now|yesterday|today|tomorrow|last week|this week)\b"
    r"|\b\d{1,2}:\d{2}\s*(?:[ap]\.?m\.?)?\b"
    r"|\b\d{1,2}[/-]\d{1,2}(?:[/-]\d{2,4})?\b"
    r"|\b(?:mon|tue|wed|thu|fri|sat|sun)(?:day|s|nes|rs|ur)?day?\b"
    r"|\b(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?\s*\d{1,2}\b"
    r"|\bago\b",
    re.I)

_UNREAD_MARK = re.compile(r"^\s*(?:\d+\s+)?(?:unread|new)\b[:\s—-]*", re.I)


def stable_key(text: str, limit: int = 160) -> str:
    """An identity for an inbound item that does NOT drift as time passes.

    The same message must key identically an hour later, or it re-notifies. Strips
    volatile time text, the unread badge, punctuation and case, then collapses
    whitespace — what is left is who said what.
    """
    t = _UNREAD_MARK.sub("", text or "")
    t = _VOLATILE.sub(" ", t)
    t = re.sub(r"[^\w\s]+", " ", t)
    return re.sub(r"\s+", " ", t).strip().lower()[:limit]


# --- does it actually need him? ---------------------------------------------

# A real ask: someone is waiting on Arun and is blocked until he moves.
_ASK = re.compile(
    r"\b(can|could|would|will)\s+(you|u)\b"
    r"|\bplease\s+(review|check|confirm|approve|share|send|update|look|advise|fix|join)\b"
    r"|\b(need|needs|needed|require[sd]?|waiting|blocked|blocker)\b.{0,24}\b(your|you|from you|arun)\b"
    r"|\byour\s+(input|review|approval|sign[- ]?off|confirmation|thoughts|help|action|attention)\b"
    r"|\b(assigned to you|over to you|action required|response required|awaiting your)\b"
    r"|\b(approve|sign[- ]?off|authorise|authorize)\b"
    r"|\bany\s+update\b|\bfollow(ing)?[- ]up\b"
    r"|\b(eod|asap|urgent|by (today|tomorrow|monday|tuesday|wednesday|thursday|friday))\b",
    re.I)

# Explicitly NOT an ask — the sender is telling, not requesting.
_FYI = re.compile(
    r"\bf\.?y\.?i\.?\b"
    r"|\bno action (is )?(required|needed)\b"
    r"|\bfor (your )?(information|awareness|reference|visibility)\b"
    r"|\b(just|simply) (sharing|a thought|thinking|an idea|wanted to (share|note))\b"
    r"|\b(heads[- ]up|sharing|shared|circulating|posting|published|announcement|newsletter)\b"
    r"|\b(auto|automated|do not reply|no[- ]reply|noreply)\b"
    r"|\b(minutes|notes|recap|summary) (of|from|for)\b",
    re.I)

# A direct 1:1 / @mention outranks wording: someone chose to address him.
_ADDRESSED = re.compile(
    r"\bmentioned you\b|\bmissed call\b|\breplied to you\b|\bin chat with you\b"
    r"|\bassigned (it |this )?to you\b|\bdm\b|\bdirect message\b",
    re.I)


def _first_sentence(s: str, limit: int = 110) -> str:
    """The gist, trimmed on a word boundary — enough for clarity, not a wall."""
    s = re.sub(r"\s+", " ", (s or "").strip())
    cut = re.split(r"(?<=[.!?])\s", s, maxsplit=1)[0] if s else ""
    if len(cut) > limit:
        cut = cut[:limit].rsplit(" ", 1)[0] + "…"
    return cut


# Openers that carry no information. Quoting these back was most of what made the
# old previews feel like padding — "Hi Arun, hope you're doing well" is not what
# the mail is about, it is what every mail starts with.
_PLEASANTRY = re.compile(
    r"^\s*(hi|hello|hey|dear|good (morning|afternoon|evening))\b[^.!?]*[.!?,]?\s*"
    r"|^\s*hope (you|this|all)[^.!?]*[.!?]\s*", re.I)


def _ask_sentence(text: str, limit: int = 110) -> str:
    """The sentence that actually asks for something — not the first sentence.

    The first sentence of a mail is a greeting far more often than it is the
    point, so quoting it added length without adding information. This finds the
    line the ask is IN and quotes that one, verbatim, which is the only part he
    needs to decide whether to open it.

    Returns "" when nothing in the body asks — in which case the subject already
    said everything, and appending anything else would be padding.
    """
    body = _PLEASANTRY.sub("", re.sub(r"\s+", " ", (text or "").strip()))
    for sentence in re.split(r"(?<=[.!?])\s+", body):
        s = sentence.strip()
        if s and _ASK.search(s):
            return s[:limit].rsplit(" ", 1)[0] + "…" if len(s) > limit else s
    return ""


def classify(who: str, subject: str, preview: str = "",
             addressed: bool | None = None) -> Verdict:
    """Judge one inbound item. Rules only — free, instant, and deterministic.

    `addressed` lets a caller assert the channel already proved this was aimed at
    him (a 1:1 DM, an @mention). That outranks wording: a bare "ok?" in a DM is
    still directed at him.
    """
    who = (who or "").strip() or "Someone"
    subject = (subject or "").strip()
    preview = (preview or "").strip()
    blob = f"{subject} {preview}"

    if addressed is None:
        addressed = bool(_ADDRESSED.search(blob))

    gist = _first_sentence(subject or preview) or "(no subject)"

    if _ASK.search(blob):
        why = "asks you directly" if addressed else "asks for something from you"
        line = f"{who}: {gist}"
        # Quote the sentence that does the asking, and only when the subject did
        # not already contain it. Anything else is detail he did not ask for and
        # has to read past to find the one thing that matters.
        if not _ASK.search(subject):
            asked = _ask_sentence(preview)
            if asked and asked.lower() not in gist.lower():
                line += f" — “{asked}”"
        return Verdict(True, why, line)

    if _FYI.search(blob):
        return Verdict(False, "explicitly FYI", f"{who}: {gist}")

    if addressed:
        # Aimed at him but with no clear ask — worth knowing, not worth stopping
        # for. Told once, quietly, and it does not pretend to need a reply.
        return Verdict(False, "addressed to you, no ask", f"{who}: {gist}")

    return Verdict(False, "no ask detected", f"{who}: {gist}")


def ambiguous(v: Verdict) -> bool:
    """Cases worth spending a FREE local-model call on to double-check."""
    return v.why in ("addressed to you, no ask", "no ask detected")


_JUDGE = (
    "Does this message require the recipient (Arun) to DO something — reply, "
    "decide, approve, review, or act? Answer with exactly one word: ACT or FYI.\n"
    "ACT = someone is waiting on him. FYI = information, updates, or someone's "
    "thoughts that need no response.\n\nMessage:\n{blob}\n\nAnswer:"
)


async def refine(v: Verdict, who: str, subject: str, preview: str = "") -> Verdict:
    """Upgrade an ambiguous verdict using the FREE local model; never paid.

    Only ever flips FYI → ACT. The rules already catch explicit asks, so the risk
    worth insuring against is a real request phrased in a way no regex predicted;
    a false ACT costs one glance, a missed one costs a dropped ball. Any failure
    (local model down, odd output) keeps the rule verdict.
    """
    if not ambiguous(v):
        return v
    try:
        from . import memory
        raw = await _complete(memory, _JUDGE.format(blob=f"{who}: {subject}\n{preview}"[:1200]))
    except Exception:
        return v
    if raw and raw.strip().upper().startswith("ACT"):
        return Verdict(True, "local model says it needs you", v.one_line)
    return v


async def _complete(memory, prompt: str) -> str:
    """memory.local_llm_complete may be sync or async depending on the backend."""
    import inspect
    out = memory.local_llm_complete(prompt)
    return await out if inspect.isawaitable(out) else out


# --- rendering a batch ------------------------------------------------------

def summarize(verdicts: list[Verdict], source: str) -> tuple[str, bool]:
    """Render a poll's findings as ONE message, and say whether it needs him.

    The asks come first and in full, because those are the reason to look at the
    phone. The FYI tail is one line each under a header that states plainly that
    nothing is wanted — so a glance is enough and he can put the phone down.
    """
    acts = [v for v in verdicts if v.action]
    fyis = [v for v in verdicts if not v.action]
    parts: list[str] = []
    if acts:
        parts.append(f"🔴 {source} — needs you ({len(acts)}):\n"
                     + "\n".join("• " + v.one_line for v in acts[:6]))
    if fyis:
        parts.append(f"· {source} — FYI, nothing needed from you ({len(fyis)}):\n"
                     + "\n".join("• " + v.one_line for v in fyis[:6]))
    return "\n\n".join(parts), bool(acts)
