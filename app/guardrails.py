"""Arun's standing instructions — one file he edits, applied everywhere.

The failure this closes: an instruction he gave once ("don't investigate
incidents", "be crisp", "plain commit messages") lived wherever the code that
heard it happened to put it — a PERSONA paragraph the chat brain reads, a
CODE_OVERRIDES block only the full code pipeline gets, a memory fact a worker
may or may not recall. So a rule held on one path and not on the next, and he
had to say it again. His words: "if i gave some instructions it is not
following", and then: "create one guardrails or some info file where i can add
some common instructions for coding and other things".

So: `guardrails.md`, in the repo root, his to edit, no restart needed. Every
`## Section` is routed to the prompts it belongs in — Coding to code legs,
Communication to chat and drafts, Investigation to analyses — and a section he
invents goes to chat, where he can watch it take effect. The file is gitignored
(his rules name his colleagues and his systems; the repo is public), and
`guardrails.example.md` ships the defaults so a fresh checkout behaves as before.

Two properties matter more than the parsing:

  NO TOKEN BLEED. A section reaches a prompt once — on a fresh session, in the
  system prompt where caching makes it nearly free — and never past
  `SECTION_MAX` characters. Past the cap the tail is cut and the health check
  says so; a rule silently dropped would be exactly the failure above in a new
  costume, so the cut is loud.

  THE FILE WINS. It is appended after the built-in persona and pipeline text,
  and it says so in its own heading, so when a default and his rule disagree the
  brain has been told which one is his.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_PATH = ROOT / "guardrails.md"
EXAMPLE_PATH = ROOT / "guardrails.example.md"

#: Characters of one section that reach a prompt. Enough for fifteen short
#: bullets; a section longer than that is a document, and documents go in
#: skills, which load on demand. The health check names any section over it.
SECTION_MAX = 1500

#: Where each section is sent. `chat` is the conversational brains (in-process
#: and CLI), `code` a code task leg, `analysis` a read-only investigation,
#: `draft` a Teams-reply draft. A section not listed here goes to chat only —
#: the one place he can immediately see whether it landed.
AUDIENCES: dict[str, tuple[str, ...]] = {
    "always": ("chat", "code", "analysis", "draft"),
    "never": ("chat", "code", "analysis", "draft"),
    "coding": ("code",),
    "communication": ("chat", "draft"),
    "investigation": ("chat", "analysis"),
    "git and accounts": ("chat", "code"),
    "standing instructions": ("chat", "analysis", "draft"),
}
_DEFAULT_AUDIENCE = ("chat",)

_HEADING = re.compile(r"^##\s+(.+?)\s*$", re.M)

_cache: dict[str, tuple[float, dict[str, str]]] = {}


def path() -> Path:
    """His file if it exists, else the shipped example — never nothing."""
    override = os.environ.get("ASTA_GUARDRAILS", "").strip()
    if override:
        return Path(override).expanduser()
    return DEFAULT_PATH if DEFAULT_PATH.exists() else EXAMPLE_PATH


def parse(text: str) -> dict[str, str]:
    """`## Heading` → body, headings lower-cased so `## Coding` and `## coding`
    are the same section. Text before the first heading is the file's own
    preamble and is not sent anywhere."""
    out: dict[str, str] = {}
    marks = list(_HEADING.finditer(text))
    for i, m in enumerate(marks):
        end = marks[i + 1].start() if i + 1 < len(marks) else len(text)
        body = text[m.end():end].strip()
        if body:
            out[m.group(1).strip().lower()] = body
    return out


def sections() -> dict[str, str]:
    """The parsed file, re-read only when it changed on disk."""
    p = path()
    try:
        mtime = p.stat().st_mtime
    except OSError:
        return {}
    key = str(p)
    hit = _cache.get(key)
    if hit and hit[0] == mtime:
        return hit[1]
    try:
        parsed = parse(p.read_text())
    except OSError:
        parsed = {}
    _cache[key] = (mtime, parsed)
    return parsed


def _cut(body: str) -> str:
    """Cap a section at SECTION_MAX, on a line boundary, and say that it was cut."""
    if len(body) <= SECTION_MAX:
        return body
    head = body[:SECTION_MAX]
    nl = head.rfind("\n")
    if nl > SECTION_MAX // 2:
        head = head[:nl]
    return head.rstrip() + "\n…(cut here — the rest of this section is not sent; shorten it in guardrails.md)"


def section(name: str) -> str:
    """One section's body, capped. '' when he has not written it."""
    return _cut(sections().get(name.strip().lower(), ""))


def _audience_of(name: str) -> tuple[str, ...]:
    return AUDIENCES.get(name, _DEFAULT_AUDIENCE)


def block(audience: str) -> str:
    """Every section this kind of prompt should carry, or '' when none.

    Order is the file's order, so he controls what the brain reads first.
    """
    parts = []
    for name, body in sections().items():
        if audience in _audience_of(name):
            parts.append(f"### {name.title()}\n{_cut(body)}")
    if not parts:
        return ""
    return ("## Arun's guardrails (guardrails.md — his standing instructions; "
            "these override any default above)\n\n" + "\n\n".join(parts))


STANDING = "Standing instructions"


def append_standing(line: str) -> Path:
    """Add one dated line under `## Standing instructions` — his yes to a rule the
    instruction compiler proposed. The shipped example is never written: with no
    file of his own yet, his file is created from it first."""
    target = path()
    if target == EXAMPLE_PATH:
        target = DEFAULT_PATH
        target.write_text(EXAMPLE_PATH.read_text() if EXAMPLE_PATH.exists() else "")
    text = target.read_text() if target.exists() else ""
    bullet = f"- {' '.join((line or '').split())}"
    if bullet in text:
        return target
    marks = list(_HEADING.finditer(text))
    here = next((i for i, m in enumerate(marks)
                 if m.group(1).strip().lower() == STANDING.lower()), None)
    if here is None:
        text = text.rstrip() + f"\n\n## {STANDING}\n{bullet}\n"
    else:
        end = marks[here + 1].start() if here + 1 < len(marks) else len(text)
        head, tail = text[:end].rstrip(), text[end:]
        text = head + "\n" + bullet + "\n" + ("\n" + tail.lstrip("\n") if tail else "")
    target.write_text(text)
    return target


def problems() -> dict[str, str]:
    """What the health check should tell him: a section over the cap (its tail is
    not being sent), an override path that points at nothing, or a file with
    no `## Section` headings at all (nothing is being applied)."""
    out: dict[str, str] = {}
    p = path()
    if not p.exists():
        out["guardrails"] = f"ASTA_GUARDRAILS points at {p}, which does not exist — no rules are applied"
        return out
    secs = sections()
    if not secs:
        try:
            if p.read_text().strip():
                out["guardrails"] = (f"{p.name} has no '## Section' headings — nothing in it "
                                     "reaches a prompt; put each rule set under a heading")
        except OSError:
            out["guardrails"] = f"{p.name} could not be read"
        return out
    over = [f"{n} ({len(b)} chars)" for n, b in secs.items() if len(b) > SECTION_MAX]
    if over:
        out["guardrails"] = (f"section(s) over {SECTION_MAX} chars — only the first {SECTION_MAX} "
                             f"reach a prompt: {', '.join(over)}. Trim, or move detail into a skill")
    return out
