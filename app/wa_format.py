"""Text shaped for a phone: WhatsApp's own markup, and lines that fit its width.

Asta's pushes are read on WhatsApp, on a phone, standing up. Two separate things
made them hard to read there, and both are fixed here rather than at each of the
thirty-odd call sites that push text.

*Markup.* The brains write GitHub markdown. WhatsApp is not markdown, and the
overlap is smaller than it looks: bold is one asterisk, not two, so ``**no
changes**`` arrives as four literal asterisks around the words; single backticks
are not code, they are backticks; ``### Heading`` keeps its hashes. Every plan
Arun has been sent carried that noise.

*Width.* A plan's STRUCTURE block is written column-aligned — path, padding,
then the note — which is exactly right in a terminal and collapses into a
paragraph-shaped blob the moment a bubble wraps it, because the padding that
separated the two columns is now sitting in the middle of a line. The alignment
that made it scannable is precisely what destroys it. So an entry becomes one
line: name, then a short note of what happens in it.
"""

from __future__ import annotations

import re

#: What a WhatsApp bubble fits on one line before wrapping.
#:
#: First set to 38 from an over-cautious guess at a phone in portrait, which was
#: wrong in the expensive direction: at that width a Java path filled the line by
#: itself, so every note was dropped as not fitting and the block became a bare
#: list of filenames. Measured off his own screenshots instead — a 72-character
#: title sits on one line there — and 64 leaves real room for the note beside
#: even a long `common/dto/...` path.
WIDTH = 64

_BOLD = re.compile(r"\*\*(.+?)\*\*", re.S)
_UNDER = re.compile(r"__(.+?)__", re.S)
_TICKS = re.compile(r"`([^`\n]+)`")
_HEADING = re.compile(r"^\s{0,3}#{1,6}\s+(.*?)\s*#*$")
_BULLET = re.compile(r"^(\s*)[-*+][ \t]+")
#: Two or more spaces — the gap between a name and its note in an aligned tree.
_GAP = re.compile(r"[ \t]{2,}")
_TRAILER = re.compile(r"^(.{4,}?)\s+\((.+)\)$")
#: A branch name, not a human aside: slashed or hyphenated, and no spaces.
_BRANCHY = re.compile(r"^[\w./-]+$")


def for_whatsapp(text: str) -> str:
    """GitHub markdown → WhatsApp's markup.

    Fenced blocks pass through untouched: monospace is the one piece of markdown
    WhatsApp does share, and a fence is the only place where a literal asterisk
    or backtick is meant.
    """
    out: list[str] = []
    in_fence = False
    for line in (text or "").splitlines():
        if line.lstrip().startswith("```"):
            in_fence = not in_fence
            out.append(line)
            continue
        if in_fence:
            out.append(line)
            continue
        head = _HEADING.match(line)
        if head:
            out.append(f"*{head.group(1)}*" if head.group(1).strip() else "")
            continue
        line = _BOLD.sub(r"*\1*", line)
        line = _UNDER.sub(r"_\1_", line)
        line = _TICKS.sub(r"\1", line)
        line = _BULLET.sub(r"\1• ", line)
        out.append(line)
    return "\n".join(out)


def clip(text: str, width: int) -> str:
    """Shorten to `width` on a word boundary, so a clipped note never ends
    mid-identifier — the half-word is what makes a truncation look like a bug."""
    text = _GAP.sub(" ", (text or "").strip())
    width = max(width, 14)
    if len(text) <= width:
        return text
    cut = text[:width].rsplit(" ", 1)[0]
    return (cut or text[:width]).rstrip(" ,;:·—-") + "…"


def _mark(note: str, head: str) -> str:
    """The one glyph that says what happens to this file, hoisted out of the note
    and onto the name — so the column of changes scans vertically."""
    low = note.lower()
    if low.startswith(("unchanged", "no change", "untouched", "=")):
        return "⚪"
    if "test" in head.lower():
        return "🧪"
    if low.startswith("new") or " new" in low[:12]:
        return "🆕"
    return "✏️"


def _split_note(line: str) -> tuple[str, str]:
    """(name, note) for one aligned tree line. No aligned gap means no note."""
    body = line.strip()
    parts = _GAP.split(body, maxsplit=1)
    if len(parts) == 2 and parts[1].strip():
        return parts[0].strip(), _GAP.sub(" ", parts[1].strip())
    return body, ""


def _shared_prefix(names: list[str]) -> str:
    """The leading path segments every entry shares. `common/dto/X.java` and
    `common/models/Y.java` cost eight characters each to say `common/` — on a
    38-column bubble that is a fifth of the line, spent on nothing."""
    paths = [re.sub(r"^[└├│─↳\s]+", "", n) for n in names]
    paths = [n for n in paths if "/" in n]
    if len(paths) < 2:
        return ""
    parts = [p.split("/")[:-1] for p in paths]
    pre: list[str] = []
    for seg in zip(*parts):
        if len(set(seg)) != 1:
            break
        pre.append(seg[0])
    return "/".join(pre) + "/" if pre else ""


def _fit_path(name: str, budget: int) -> str:
    """Shorten a path from the FRONT. `common/models/Foo.java` is over budget by
    exactly the segment carrying no information, and the filename at the other
    end is the whole point — ordinary truncation removes the only part worth
    reading.

    The last two segments always stay, even when that overruns: `common/dto/X`
    and `common/models/X` collapse to the same line without them, and one line
    that wraps by three characters beats two entries he cannot tell apart.
    """
    segs = name.split("/")
    if len(name) <= budget or len(segs) < 3:
        return name
    while len(segs) > 2 and len("…/" + "/".join(segs[1:])) > budget:
        segs.pop(0)
    return "…/" + "/".join(segs[1:]) if len(segs) > 2 else "…/" + "/".join(segs)


def _clean(name: str) -> str:
    """Drop the brain's own inline markers — `<new>` before a path says what the
    glyph now says, twice."""
    return re.sub(r"^<[a-z ]{1,10}>\s*", "", name.strip(), flags=re.I)


#: A note that only restates its own glyph. "🆕 Foo.java  NEW" spends a third of
#: the line saying the same thing twice, and a long Java path has no third to
#: spare — dropping these is what lets the notes that DO say something survive.
_REDUNDANT = re.compile(
    r"^(new(\s*(file|class|record))?|\+\d+\s*(field|line|case|test)s?"
    r"|unchanged|no\s*change|untouched|edit(ed)?|modified|test)\b[\s:·—-]*$", re.I)


def _redundant(note: str, mark: str) -> bool:
    return bool(_REDUNDANT.match(_denote(note, mark).strip() or note.strip()))


LEGEND = "🆕 new · ✏️ edit · ⚪ untouched · 🧪 test"


def _denote(note: str, mark: str) -> str:
    """Strip the word the glyph already carries, so the note is only new
    information: "🆕 X.java / ↳ NEW · mirrors …" says NEW twice in nine chars."""
    if mark in ("🆕", "🧪"):
        return re.sub(r"^new(\s+(file|class|record|test))?\b\s*[·:—-]?\s*", "",
                      note, flags=re.I).strip() or note
    return note


def reflow_tree(lines: list[str], width: int = WIDTH) -> list[str]:
    """Column-aligned structure block → ONE LINE PER ENTRY.

    The first version of this wrapped each note onto continuation lines under its
    name. Every line then fit, and the block was still unreadable: six files
    became twenty stacked fragments, and a plan you have to assemble in your head
    is not a plan you can approve standing up. Arun's word for it was "clumsy".

    So an entry is one line and the note is whatever fits after the name. That
    genuinely loses text, and it is the right trade: this is a decision aid, not
    a specification. He is answering "is this the change I meant?", and the full
    plan is one tap away in the app for when the answer is no.
    """
    split = [_split_note(ln) for ln in lines]
    strip = _shared_prefix([_clean(n) for n, note in split if note])
    out: list[str] = []
    for raw, (name, note) in zip(lines, split):
        if not raw.strip():
            out.append("")
            continue
        pad = " " * min(len(raw) - len(raw.lstrip()), 4)
        name = _clean(name)
        if not note:
            trailer = _TRAILER.match(name)
            if trailer and _BRANCHY.match(trailer.group(2)):
                # "telikos-email-service (feature/asta-94-transportassetpriority-
                # carry-the-field-t)" — a whole wrapped line spent on a name Asta
                # generated, that he never types and does not decide anything by.
                # The DONE push carries it, where checking it out is the point.
                out.append(f"{pad}📁 {trailer.group(1)}")
                continue
            # A note-less line with spaces in it is prose the brain wrote around
            # the tree ("booking-service: no changes — their legs are already
            # done"), not a name. Clipping a sentence to the column width
            # deletes the sentence; WhatsApp wraps prose perfectly well.
            out.append(raw.rstrip() if " " in name.strip()
                       else f"{pad}{clip(name, width - len(pad))}")
            continue
        # The box characters go and the indent stays. Both said "this hangs off
        # the one above"; keeping both cost four columns of a forty-six column
        # line and pushed the glyph out of the column it is scanned in.
        name = re.sub(r"^[└├│─↳\s]+", "", name)
        mark = _mark(note, name)
        short = name[len(strip):] if strip and name.startswith(strip) else name
        # Reserve the note's room BEFORE fitting the path, or a long path eats
        # the whole line and the note it was supposed to sit beside gets clipped
        # to nothing. The path is the half that can be shortened without loss.
        short = _fit_path(short, width - len(pad) - 3 - min(len(note) + 2, 22))
        lead = f"{pad}{mark} {short}"
        room = width - len(lead) - 2
        if _redundant(note, mark):
            out.append(lead)
            continue
        note = _denote(note, mark)
        out.append(f"{lead}  {clip(note, room)}" if room >= 14 else lead)
    return out


#: How the brain marks the step a change actually lands in.
_HERE = re.compile(r"\s*(?:<-+|←|⬅)\s*(change|here|this)?\s*$", re.I)


def reflow_flow(lines: list[str], width: int = WIDTH) -> list[str]:
    """An aligned FLOW block → a numbered sequence he can follow in one pass.

    The tree says WHAT changes; this says WHERE it sits in the run of events —
    which service writes the field, which one only carries it, which one reads
    it. Arun asked for both: the shape alone cannot show that a service in the
    middle needs no change at all, which is the single fact a reviewer of this
    plan most needs and the easiest one to get wrong.
    """
    out: list[str] = []
    step = 0
    for raw in lines:
        if not raw.strip():
            continue
        actor, note = _split_note(raw)
        actor = re.sub(r"^[\d.)\s*+-]+", "", actor).strip()
        if not actor:
            continue
        step += 1
        here = bool(_HERE.search(note))
        note = _HERE.sub("", note).strip()
        lead = f"{step} {actor}"
        room = width - len(lead) - 3 - (8 if here else 0)
        if note and room >= 10:
            lead = f"{lead} · {clip(note, room)}"
        out.append(f"{lead}  ⬅ HERE" if here else lead)
    return out
