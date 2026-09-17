"""Arun, simulated — so scenarios are not only passed in a developer's English.

Every routing decision Asta makes reads his actual words: "new task have to
create new topic for service plan", "Ship follow the same Pr head like my last
PR", "are u working on new topic creations or not ..?". A scenario written as
"please start a new task to create the topics" tests a sentence he would never
send, and the classifier that fails on his phrasing passes on the developer's.

So every scenario runs again in his voice. The transformations are LEXICAL only
— spelling, spacing, contractions, the abbreviations he actually uses, measured
from his own messages. Nothing that could change meaning: no dropped negation,
no reordered clauses, no removed task references. A twin that changes what he
asked for would fail scenarios for the wrong reason and teach us the opposite of
what the run says.

The profile lives in `data/workworld/twin.json` (gitignored — it is derived from
his messages) and is rebuilt with `python -m app.workworld twin`. Without it the
defaults below apply, which are themselves taken from his September messages.
"""

from __future__ import annotations

import json
import random
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
PROFILE = ROOT / "data" / "workworld" / "twin.json"

#: Substitutions seen in his own messages, most characteristic first. Each is
#: whole-word and meaning-preserving.
DEFAULT_SUBS: dict[str, str] = {
    "you": "u", "your": "ur", "are": "r", "about": "abt", "regarding": "reg",
    "please": "pls", "because": "bcoz", "tomorrow": "tmr", "message": "msg",
    "okay": "ok", "and": "and", "between": "btw", "information": "info",
    "should": "shud", "would": "wud", "number": "no",
}

#: Habits, as rates. Measured by `build_profile`; these defaults match his
#: September WhatsApp messages closely enough to be worth shipping.
DEFAULT_HABITS = {
    "lowercase": 0.75,        # he rarely capitalises
    "drop_apostrophe": 0.8,   # dont, cant, im, wont
    "space_before_punct": 0.35,   # "push , and then"
    "trailing_dots": 0.45,    # "..?" / ".." at the end
    "drop_final_period": 0.9,
    "typo": 0.12,             # one doubled or swapped letter
    "sub_rate": 0.6,          # how often a known substitution is taken
}

_WORD = re.compile(r"[A-Za-z']+")
#: Never touched: changing any of these changes what he asked for.
_KEEP = {"not", "no", "never", "new", "task", "stop", "cancel", "approve", "plan",
         "ship", "pr", "ci", "don't", "dont", "yes", "send"}


def profile() -> dict:
    try:
        data = json.loads(PROFILE.read_text())
    except (OSError, ValueError):
        return {"subs": DEFAULT_SUBS, "habits": DEFAULT_HABITS, "source": "defaults"}
    data.setdefault("subs", DEFAULT_SUBS)
    data.setdefault("habits", DEFAULT_HABITS)
    return data


def build_profile(limit: int = 800) -> dict:
    """Measure his habits from his own messages and save them.

    Reads whatever database `store` currently points at, so running this against
    the live one is deliberate and explicit (`python -m app.workworld twin`); a
    scenario run never calls it.
    """
    from app import scorecard, store
    rows = store._connect().execute(
        "SELECT content FROM ui_messages WHERE role='user' ORDER BY id DESC LIMIT ?",
        (limit,)).fetchall()
    his = [w for w in (scorecard.his_words(r[0]) for r in rows) if w]
    if not his:
        return profile()
    n = len(his)
    words = [w.lower() for m in his for w in _WORD.findall(m)]
    habits = {
        "lowercase": round(sum(1 for m in his if m == m.lower()) / n, 2),
        "drop_apostrophe": round(
            sum(1 for m in his if re.search(r"\b(dont|cant|im|wont|didnt|isnt)\b", m, re.I)) /
            max(1, sum(1 for m in his if re.search(r"\b(don'?t|can'?t|i'?m|won'?t|didn'?t|isn'?t)\b", m, re.I))), 2),
        "space_before_punct": round(sum(1 for m in his if re.search(r"\s+[,.?]", m)) / n, 2),
        "trailing_dots": round(sum(1 for m in his if re.search(r"\.\.+\??\s*$", m)) / n, 2),
        "drop_final_period": round(sum(1 for m in his if not m.rstrip().endswith(".")) / n, 2),
        "typo": DEFAULT_HABITS["typo"],
        "sub_rate": DEFAULT_HABITS["sub_rate"],
    }
    counts = {short: words.count(short) for short in set(DEFAULT_SUBS.values())}
    subs = {long: short for long, short in DEFAULT_SUBS.items()
            if counts.get(short, 0) > 0 or long in ("you", "your")}
    out = {"subs": subs or DEFAULT_SUBS, "habits": habits, "messages": n,
           "source": "measured"}
    PROFILE.parent.mkdir(parents=True, exist_ok=True)
    PROFILE.write_text(json.dumps(out, indent=1))
    return out


#: Each variant is a NAMED style, not a dice roll, so "pass^4" means four
#: specific ways he writes rather than four samples that might all come back
#: unchanged — which is what happened when this was probabilistic: scenario text
#: already written in his voice sailed through every variant untouched.
STYLES = (
    {},                                                     # 0: as written
    {"lowercase": 1.0, "trailing_dots": 1.0, "sub_rate": 0.9, "typo": 0.0,
     "drop_apostrophe": 1.0, "space_before_punct": 0.0, "drop_final_period": 1.0},
    {"lowercase": 0.0, "trailing_dots": 0.0, "sub_rate": 1.0, "typo": 1.0,
     "drop_apostrophe": 1.0, "space_before_punct": 1.0, "drop_final_period": 1.0},
    {"lowercase": 1.0, "trailing_dots": 1.0, "sub_rate": 1.0, "typo": 1.0,
     "drop_apostrophe": 1.0, "space_before_punct": 1.0, "drop_final_period": 1.0,
     "filler": 1.0},
)

#: Openers he actually uses. They carry no instruction of their own, so they
#: cannot change what a message asks for.
FILLERS = ("see ", "ok ", "pls ", "hey ")


def restyle(text: str, seed: int) -> str:
    """His phrasing of the same sentence — deterministic for a given seed."""
    if not text or seed == 0:
        return text
    p = profile()
    rng = random.Random(f"{seed}:{text}")
    habits = dict(p["habits"])
    habits.update(STYLES[seed % len(STYLES)])
    subs = p["subs"]
    # A short message is a COMMAND — "stop 1", "new chat", "ignore telegram",
    # "yes" — and he types those exactly. Fillers and typos belong to the long
    # instructions, which is where his style actually shows. Without this split
    # the twin was testing whether Asta can read "ignore teleegram", which is
    # not a question anybody asked.
    long_enough = len(text.split()) >= 5
    if habits.get("filler") and long_enough:
        text = rng.choice(FILLERS) + text
    if not long_enough:
        habits["typo"] = 0.0

    def word(m: re.Match) -> str:
        w = m.group(0)
        low = w.lower()
        if low in _KEEP:
            return w
        if low in subs and rng.random() < habits.get("sub_rate", 0.6):
            return subs[low]
        if "'" in w and rng.random() < habits.get("drop_apostrophe", 0.8):
            w = w.replace("'", "")
        return w

    out = _WORD.sub(word, text)
    if rng.random() < habits.get("lowercase", 0.75):
        out = out.lower()
    if rng.random() < habits.get("space_before_punct", 0.35):
        out = re.sub(r"([,?])", r" \1", out, count=1)
    if rng.random() < habits.get("typo", 0.12):
        out = _one_typo(out, rng)
    if out.rstrip().endswith(".") and rng.random() < habits.get("drop_final_period", 0.9):
        out = out.rstrip().rstrip(".")
    if rng.random() < habits.get("trailing_dots", 0.45):
        out = out.rstrip() + (" ..?" if out.rstrip().endswith("?") is False and _asks(text) else " ..")
    return out


def _asks(text: str) -> bool:
    return bool(re.search(r"\?|^(can|did|is|are|do|does|why|what|when|will|shall)\b", text.strip(), re.I))


def _one_typo(text: str, rng: random.Random) -> str:
    """One doubled letter in one long word — his most common slip."""
    words = [(i, w) for i, w in enumerate(text.split()) if len(w) > 5 and w.isalpha()
             and w.lower() not in _KEEP]
    if not words:
        return text
    i, w = rng.choice(words)
    at = rng.randrange(1, len(w) - 1)
    parts = text.split()
    parts[i] = w[:at] + w[at] + w[at:]
    return " ".join(parts)
