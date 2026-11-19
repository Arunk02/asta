"""A plan he can read on his phone: plan-<id>.pdf, rendered from the plan's Markdown.

His words, 18 Nov: "the .md file it is sharing, I don't feel it easy to read and
understand clearly". WhatsApp opens a .md as raw text — `**`, backticks, a JSON
``outcome`` block, a revision hash. A PDF opens in the phone's own viewer with
real headings, bullets, tables and code blocks.

Narrow pages (phone width), the noise taken out (the machine-readable outcome
block, revision and receipt lines, "PLAN READY"), bare section names
("STRUCTURE", "RISK: …") made headings. Rendered with fpdf2, already a
dependency — no browser to launch. Any failure returns None and the caller
sends the .md as before, so a rendering bug can never cost him the plan.
"""
from __future__ import annotations

import re
from pathlib import Path

#: Phone-shaped page, in mm: read without zooming on a phone held upright.
PAGE = (110, 196)
MARGIN = 7
_FONTS = (Path("/System/Library/Fonts/Supplemental/Arial Unicode.ttf"),
          Path("/Library/Fonts/Arial Unicode.ttf"))

_OUTCOME = re.compile(r"```outcome\s*\n.*?```\s*", re.S)
_NOISE = re.compile(r"(?im)^\s*(?:Revision [0-9a-f]{6,}|Plan receipt:.*|PLAN READY|---)\s*$\n?")
#: "STRUCTURE" alone on a line, or "RISK: text" / "- ROLLBACK: text".
_SECTION = re.compile(r"(?m)^(?:[-*]\s+)?([A-Z][A-Z ]{2,30})\s*$")
_LABEL = re.compile(r"(?m)^([-*]\s+)?([A-Z][A-Z ]{2,30}):\s+(.+)$")


def clean(text: str) -> str:
    """The plan as he should read it: no machine blocks, sections as headings."""
    text = _OUTCOME.sub("", text or "")
    text = _NOISE.sub("", text)
    text = _SECTION.sub(lambda m: f"### {m.group(1).strip().capitalize()}", text)
    text = _LABEL.sub(lambda m: f"{m.group(1) or ''}**{m.group(2).strip().capitalize()}:** {m.group(3)}"
                      + ("" if m.group(1) else "\n"), text)
    return re.sub(r"\n{3,}", "\n\n", text).strip() + "\n"


def _html(markdown: str) -> str:
    from markdown_it import MarkdownIt
    body = MarkdownIt("commonmark").enable("table").render(markdown)
    # fpdf's HTML is a subset: keep code readable and tables bordered.
    body = body.replace("<table>", '<table border="1" width="100%">')
    body = re.sub(r"<pre><code[^>]*>", '<pre><font face="Courier" size="8">', body)
    body = body.replace("</code></pre>", "</font></pre>")
    body = re.sub(r"<code>(.*?)</code>", r'<font face="Courier" color="#8a2be2">\1</font>', body, flags=re.S)
    return body


def _font(pdf) -> str:
    for p in _FONTS:
        if p.exists():
            pdf.add_font("Plan", "", str(p))
            pdf.add_font("Plan", "B", str(p))
            pdf.add_font("Plan", "I", str(p))
            pdf.add_font("Plan", "BI", str(p))
            return "Plan"
    return ""


def render(md_path: Path, title: str = "") -> Path | None:
    """plan-<id>.pdf beside plan-<id>.md, or None when it could not be made."""
    try:
        from fpdf import FPDF
        source = md_path.read_text()
        # The file's own first line is the "# Plan for task #N — title" heading.
        first, _, rest = source.partition("\n")
        heading = first.lstrip("# ").strip() if first.startswith("#") else (title or md_path.stem)
        pdf = FPDF(format=PAGE)
        pdf.set_margins(MARGIN, MARGIN, MARGIN)
        pdf.set_auto_page_break(auto=True, margin=MARGIN)
        family = _font(pdf)
        if not family:
            from .files import _latin1
            heading, rest = _latin1(heading), _latin1(rest)
            family = "Helvetica"
        pdf.add_page()
        pdf.set_font(family, "B", 13)
        pdf.multi_cell(0, 6.5, heading)
        pdf.ln(2)
        pdf.set_font(family, size=10)
        pdf.write_html(_html(clean(rest)), font_family=family)
        out = md_path.with_suffix(".pdf")
        pdf.output(str(out))
        return out if out.stat().st_size > 0 else None
    except Exception:                                          # noqa: BLE001
        return None

