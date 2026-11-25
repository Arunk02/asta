"""A plan he understands by looking at it: plan-<id>.pdf with drawn flow diagrams.

His words, 25 Nov: "better plan readability, a bit diagrammatic in the PDF ...
proper flow diagrams, easy to read and understand just by watching", and
"remove sending the plan.md in the message". plan_pdf.py made the Markdown
readable on a phone but drew nothing: a ```mermaid block arrived as code.

Here the plan's Markdown becomes an HTML page whose Mermaid blocks are drawn by
Mermaid itself in headless Chrome, then printed to a phone-shaped PDF. Two
passes: Chrome renders the diagrams and hands back the finished DOM, then that
static page (no scripts left) is printed — printing straight from the live page
caught the diagrams half-drawn.

A plan with no diagram of its own still gets one: its STRUCTURE lines, drawn as
the files and classes the change touches.

Mermaid is fetched once from a pinned version and checked against a pinned
SHA-256 before it is ever used; no copy of it lives in the repo. Any failure
returns None and the caller falls back to plan_pdf (text) — a rendering problem
can never cost him the plan.
"""
from __future__ import annotations

import asyncio
import hashlib
import html
import os
import re
import tempfile
import urllib.request
from pathlib import Path

from . import store

MERMAID_URL = "https://cdn.jsdelivr.net/npm/mermaid@10.9.1/dist/mermaid.min.js"
MERMAID_SHA256 = "61b335a46df05a7ce1c98378f60e5f3e77a7fb608a1056997e8a649304a936d6"
_CHROMES = ("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
            "/Applications/Chromium.app/Contents/MacOS/Chromium",
            "/usr/bin/google-chrome", "/usr/bin/chromium")
TIMEOUT = 90

_FENCE = re.compile(r"```mermaid\s*\n(.*?)```", re.S)


def enabled() -> bool:
    return os.environ.get("ASTA_PLAN_VISUAL", "1").strip().lower() not in ("0", "false", "no", "off")


def chrome() -> str:
    pinned = os.environ.get("ASTA_CHROME", "").strip()
    for path in ((pinned,) if pinned else ()) + _CHROMES:
        if path and Path(path).is_file():
            return path
    return ""


def _vendor_dir() -> Path:
    return store.DB_PATH.parent / "vendor"


def mermaid_js() -> Path | None:
    """The pinned Mermaid build, fetched once and verified; None if it cannot be."""
    path = _vendor_dir() / "mermaid-10.9.1.min.js"
    try:
        if path.is_file() and hashlib.sha256(path.read_bytes()).hexdigest() == MERMAID_SHA256:
            return path
        with urllib.request.urlopen(MERMAID_URL, timeout=30) as resp:   # noqa: S310 — pinned https
            body = resp.read()
        if hashlib.sha256(body).hexdigest() != MERMAID_SHA256:
            return None
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(body)
        return path
    except OSError:
        return None


# --- the page ---------------------------------------------------------------------

def _label(text: str) -> str:
    """A string Mermaid accepts inside ["..."]."""
    return re.sub(r'["\[\]{}<>|#;]', " ", text).strip()[:70] or "step"


def structure_diagram(plan: str) -> str:
    """Mermaid for a plan that drew nothing: what the change touches, in order."""
    from . import routing
    rows = []
    for line in routing._structure_lines(plan):
        s = line.strip().strip("-*• ")
        if not s:
            continue
        parts = re.split(r"\s{2,}|\s+[—–-]\s+|:\s+", s, maxsplit=1)
        what = parts[0].strip("`*")
        why = parts[1].strip() if len(parts) > 1 else ""
        rows.append((what, why))
    if not rows:
        return ""
    lines = ["flowchart TD"]
    for i, (what, why) in enumerate(rows[:12]):
        label = _label(what) + (f"<br/><small>{_label(why)}</small>" if why else "")
        lines.append(f'  n{i}["{label}"]')
        if i:
            lines.append(f"  n{i - 1} --> n{i}")
    return "\n".join(lines)


def page(markdown_text: str, title: str) -> str:
    """The plan as HTML; Mermaid blocks become drawn diagrams."""
    from markdown_it import MarkdownIt
    diagrams: list[str] = []

    def keep(m: re.Match) -> str:
        diagrams.append(m.group(1).strip())
        return f"\n\nMERMAIDBLOCK{len(diagrams) - 1}\n\n"

    body_md = _FENCE.sub(keep, markdown_text)
    # Plans indent their entries ("  RailEtaValidator   new check"): one per line,
    # as bullets — Markdown would run them together into one paragraph.
    body_md = re.sub(r"(?m)^ {2,4}(?=[^\s|>-])", "- ", body_md)
    if not diagrams:
        drawn = structure_diagram(markdown_text)
        if drawn:
            diagrams.append(drawn)
            body_md = f"### What changes\n\nMERMAIDBLOCK0\n\n{body_md}"
    body = MarkdownIt("commonmark").enable("table").render(body_md)
    for i, d in enumerate(diagrams):
        block = f'<div class="diagram"><pre class="mermaid">{html.escape(d)}</pre></div>'
        body = body.replace(f"<p>MERMAIDBLOCK{i}</p>", block)
    return f"""<!doctype html><html><head><meta charset="utf-8"><title>{html.escape(title)}</title>
<style>
@page {{ size: 130mm 230mm; margin: 8mm; }}
body {{ font-family: -apple-system, "Helvetica Neue", Arial, sans-serif; color:#1d2433; background:#fff;
       font-size: 11px; line-height: 1.45; margin:0; }}
h1 {{ font-size: 17px; margin: 0 0 6px; }} h2 {{ font-size: 14px; margin: 12px 0 4px; }}
h3 {{ font-size: 12.5px; margin: 10px 0 3px; color:#0b6bcb; }}
table {{ border-collapse: collapse; width:100%; font-size:10px; }}
th, td {{ border:1px solid #d9dee7; padding:3px 5px; text-align:left; vertical-align:top; }}
th {{ background:#f4f6fa; }}
code {{ font-family: Menlo, monospace; font-size: 9.5px; background:#eef1f6; padding:0 2px; border-radius:2px; }}
pre {{ white-space: pre-wrap; font-size: 9.5px; background:#f4f6fa; padding:5px; border-radius:4px; }}
.diagram {{ border:1px solid #d9dee7; border-radius:6px; padding:6px; margin:6px 0; break-inside: avoid; }}
.diagram pre.mermaid {{ background:#fff; padding:0; text-align:center; }}
.diagram svg {{ max-width:100% !important; height:auto; max-height:170mm; }}
</style></head><body><h1>{html.escape(title)}</h1>{body}</body></html>"""


def _scripts(js: Path) -> str:
    return (f'<script src="{js.as_uri()}"></script><script>mermaid.initialize({{startOnLoad:true,'
            f"theme:'base',securityLevel:'strict',flowchart:{{htmlLabels:true,useMaxWidth:true}},"
            f"themeVariables:{{fontSize:'13px',primaryColor:'#eaf2fc',primaryBorderColor:'#0b6bcb',"
            f"lineColor:'#5b6475'}}}});</script>")


async def _run(*args: str) -> tuple[int, bytes]:
    proc = await asyncio.create_subprocess_exec(*args, stdout=asyncio.subprocess.PIPE,
                                                stderr=asyncio.subprocess.DEVNULL)
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=TIMEOUT)
    except asyncio.TimeoutError:
        proc.kill()
        return -1, b""
    return proc.returncode or 0, out


async def render(md_path: Path, title: str = "") -> Path | None:
    """plan-<id>.pdf with drawn diagrams beside plan-<id>.md, or None."""
    if not enabled():
        return None
    exe = chrome()
    js = await asyncio.to_thread(mermaid_js)
    if not exe or not js:
        return None
    try:
        from .plan_pdf import clean
        source = md_path.read_text()
        first, _, rest = source.partition("\n")
        heading = first.lstrip("# ").strip() if first.startswith("#") else (title or md_path.stem)
        doc = page(clean(rest), heading).replace("</body>", _scripts(js) + "</body>")
        out = md_path.with_suffix(".pdf")
        with tempfile.TemporaryDirectory() as tmp:
            live = Path(tmp) / "plan.html"
            live.write_text(doc)
            flags = ("--headless=new", "--disable-gpu", "--no-first-run",
                     f"--user-data-dir={Path(tmp) / 'profile'}", "--allow-file-access-from-files")
            rc, dom = await _run(exe, *flags, "--virtual-time-budget=15000", "--dump-dom",
                                 live.as_uri())
            text = dom.decode(errors="replace")
            drawn = len(re.findall(r'<svg[^>]*aria-roledescription="(?!error)', text))
            if rc != 0 or "<svg" not in text or 'aria-roledescription="error"' in text \
                    or drawn < doc.count('class="mermaid"'):
                return None
            static = Path(tmp) / "static.html"
            static.write_text(re.sub(r"<script\b.*?</script>", "", text, flags=re.S))
            rc, _ = await _run(exe, *flags, "--no-pdf-header-footer",
                               f"--print-to-pdf={out}", static.as_uri())
        return out if rc == 0 and out.is_file() and out.stat().st_size > 2000 else None
    except Exception:                                            # noqa: BLE001
        return None
