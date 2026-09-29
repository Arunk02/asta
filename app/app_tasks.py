"""Do a task INSIDE an app — not just open it.

His words, 29 Sep: "dont just open youtube and play songs, it should open any
kind of app in my task and do, open excel add a column, like that it should do
task on any apps". `open_app` puts a window in front of him; `use_app` knows
seven fixed recipes; `use_screen` clicks named things. None of them can take a
goal it has never seen and carry it out.

This can, for any app that publishes an AppleScript dictionary (Numbers, Word,
PowerPoint, Keynote, Pages, Outlook, Chrome, Notes, Reminders…):

  1. read the app's own dictionary from its bundle (the .sdef — `sdef` itself
     needs full Xcode), cut down to the parts the goal is about;
  2. a model writes the script AND a check that returns proof it worked;
  3. the script is screened: never a shell, never keystrokes into other apps,
     never another app; deleting, closing without saving or quitting needs his
     explicit yes;
  4. it runs, the check runs, and he is told what the check saw — a failure is
     sent back to the model once with the error, then reported as it is.

The script and its check are recorded, so "what did you do in Numbers?" has an
exact answer.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import xml.etree.ElementTree as ET
from pathlib import Path

from . import store

#: Names he uses for apps this Mac does not have, and what to use instead.
#: Excel is not installed; Numbers reads and writes .xlsx.
STAND_INS = {"excel": ("Microsoft Excel", "Numbers"),
             "microsoft excel": ("Microsoft Excel", "Numbers")}

DICT_CHARS = 7000
#: Always kept from a dictionary, whatever the goal says.
_CORE = {"application", "document", "window", "make", "open", "save", "count",
         "table", "row", "column", "cell", "sheet", "slide", "paragraph", "text",
         "tab", "note", "reminder", "message", "range"}


class AppTaskError(RuntimeError):
    pass


def model() -> str:
    return os.environ.get("ASTA_APP_TASK_MODEL", "").strip() or "sonnet"


def resolve(name: str) -> tuple[Path, str]:
    """(app bundle, note) for the app he named — or a stand-in, said out loud."""
    from . import apps
    key = " ".join((name or "").lower().split())
    wanted = name
    note = ""
    if key in STAND_INS:
        real, alt = STAND_INS[key]
        if apps.find_app(real)[0] is None:
            wanted, note = alt, f"{real} isn't installed here, so I used {alt} (it reads .xlsx). "
    path, _ = apps.find_app(wanted)
    if path is None:
        raise AppTaskError(f"no app called {name!r} on this Mac")
    return path, note


def dictionary(app: Path, goal: str = "") -> str:
    """The app's scripting dictionary, compact, biased to what the goal names."""
    files = sorted((app / "Contents" / "Resources").glob("*.sdef"))
    if not files:
        return ""
    try:
        root = ET.fromstring(files[0].read_bytes())
    except (ET.ParseError, OSError):
        return ""
    words = {w for w in re.findall(r"[a-z]+", goal.lower()) if len(w) > 2} | _CORE

    def wanted(n: str) -> bool:
        return any(w in n.lower() or n.lower() in w for w in words)

    lines: list[str] = []
    for suite in root.iter("suite"):
        cmds = [c.get("name", "") for c in suite.findall("command")]
        cmds = [c for c in cmds if wanted(c)] or cmds[:6]
        if cmds:
            lines.append(f"commands: {', '.join(cmds)}")
        for cls in suite.findall("class") + suite.findall("class-extension"):
            name = cls.get("name") or cls.get("extends") or ""
            if not wanted(name):
                continue
            props = [p.get("name", "") for p in cls.findall("property")][:18]
            elems = [e.get("type", "") for e in cls.findall("element")][:10]
            lines.append(f"class {name}: props {', '.join(props)}"
                         + (f"; elements {', '.join(elems)}" if elems else ""))
    return "\n".join(lines)[:DICT_CHARS]


_BRIEF = """You write AppleScript that does ONE task in the macOS app "{app}" for Arun.

His ask: {goal}
{context}
The app's scripting dictionary (trimmed):
{dictionary}

Rules:
- Only `tell application "{app}"`. Never `do shell script`, never "System Events",
  never keystrokes, never another application.
- If he names no document, use the front document; if none is open, make a new
  one. Leave the result open in front of him (`activate`).
- Never delete anything, close without saving, or quit unless the ask says so;
  if the script must, set "destructive" to true.
- "check" is a second AppleScript that changes nothing and RETURNS a short
  string proving the task is done (e.g. the new column's header, the row count).
- If something essential is missing and cannot be defaulted sensibly, leave
  "script" empty and put ONE short question in "question".

Reply with ONLY this JSON:
{{"script":"...","check":"...","changes":"one line: what this changes","destructive":false,"question":""}}"""

_REPAIR = """That script failed with:
{error}

Fix it. Same rules, same JSON shape, nothing else."""

_FORBIDDEN = re.compile(r"do\s+shell\s+script|system\s+events|keystroke|key\s+code|"
                        r"run\s+script|load\s+script|store\s+script", re.I)
_DESTRUCTIVE = re.compile(r"\bdelete\b|\bquit\b|\bempty\b|saving\s+no|move\s+.*\bto\s+trash|"
                          r"\berase\b|\bremove\b", re.I)
_TELL = re.compile(r'tell\s+application\s+"([^"]+)"', re.I)


def screen(script: str, app: str) -> str:
    """'' if the script may run; otherwise why not."""
    if _FORBIDDEN.search(script or ""):
        return "it reached outside the app (shell, keystrokes or System Events)"
    others = {t for t in _TELL.findall(script or "") if t.lower() != app.lower()}
    if others:
        return f"it talks to other apps too ({', '.join(sorted(others))})"
    return ""


def destructive(plan: dict) -> bool:
    return bool(plan.get("destructive")) or bool(_DESTRUCTIVE.search(plan.get("script") or ""))


async def _ask_model(text: str) -> dict:
    """The script writer. Its own seam, so tests never start a CLI."""
    from . import claude_cli
    raw = await claude_cli.one_shot(text, model=model(), tools_off=True, timeout=120)
    m = re.search(r"\{.*\}", raw or "", re.S)
    if not m:
        raise AppTaskError("the script writer returned no plan")
    try:
        return json.loads(m.group(0))
    except ValueError as exc:
        raise AppTaskError(f"the script writer's plan was not readable: {exc}") from exc


async def osascript(script: str, timeout: float = 60) -> tuple[int, str]:
    """Run AppleScript. Its own seam, so tests never drive a real app."""
    proc = await asyncio.create_subprocess_exec(
        "osascript", "-e", script, stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout)
    except asyncio.TimeoutError:
        proc.kill()
        return 124, "timed out"
    return proc.returncode or 0, (out or b"").decode(errors="replace").strip()


def _permission_hint(out: str, app: str) -> str:
    if "-1743" in out or "not authorized" in out.lower() or "not allowed" in out.lower():
        return (f"macOS has not let Asta control {app} yet. Allow it once: System "
                f"Settings → Privacy & Security → Automation → Python → {app}.")
    return ""


async def do(app_name: str, goal: str, *, context: str = "", confirmed: bool = False) -> str:
    """Carry out `goal` inside the app. One line for him, always honest."""
    from . import apps
    if not apps.enabled():
        return "Not done — app doors are off (ASTA_APPS=1 lets Asta use them)."
    try:
        path, note = resolve(app_name)
    except AppTaskError as exc:
        return f"Not done — {exc}."
    app = path.stem
    brief = _BRIEF.format(app=app, goal=goal.strip(),
                          context=f"Context: {context.strip()}\n" if context.strip() else "",
                          dictionary=dictionary(path, goal) or "(no dictionary published)")
    try:
        plan = await _ask_model(brief)
    except Exception as exc:                                   # noqa: BLE001
        return f"Not done — I couldn't work out how to do that in {app}: {exc}"
    if (plan.get("question") or "").strip() and not (plan.get("script") or "").strip():
        return f"Before I do it in {app}: {plan['question'].strip()}"
    for attempt in (1, 2):
        script = plan.get("script") or ""
        why = screen(script, app)
        if why:
            _record(app, goal, plan, "refused", why)
            return f"Not done — the script I got for {app} was unsafe: {why}."
        if destructive(plan) and not confirmed:
            _record(app, goal, plan, "needs yes", plan.get("changes", ""))
            return (f"{note}This would {plan.get('changes') or 'change or remove things'} in "
                    f"{app} — that can't be undone. Say yes and I'll go ahead.")
        code, out = await osascript(script)
        if code == 0:
            ccode, seen = await osascript(plan.get("check") or 'return ""')
            proof = seen if ccode == 0 and seen else "(the check returned nothing)"
            _record(app, goal, plan, "done", proof)
            return f"{note}Done in {app}: {(plan.get('changes') or goal).rstrip('.')}. Checked: {proof[:200]}"
        hint = _permission_hint(out, app)
        if hint:
            _record(app, goal, plan, "no permission", out)
            return f"Not done — {hint}"
        if attempt == 2:
            _record(app, goal, plan, "failed", out)
            return f"Not done — {app} rejected the script twice. Last error: {out[:200]}"
        try:
            plan = await _ask_model(brief + "\n\n" + _REPAIR.format(error=out[:600]))
        except Exception as exc:                               # noqa: BLE001
            _record(app, goal, plan, "failed", out)
            return f"Not done — {app} rejected the script ({out[:120]}) and the fix failed: {exc}"
    return "Not done."


def _record(app: str, goal: str, plan: dict, outcome: str, detail: str) -> None:
    store.record_outcome("app_task", outcome, subject=app[:80],
                         detail=json.dumps({"goal": goal[:200], "changes": plan.get("changes", ""),
                                            "script": (plan.get("script") or "")[:1500],
                                            "check": (plan.get("check") or "")[:400],
                                            "seen": detail[:300]})[:2000])


#: Apps he refers to by a short name. Used to spot "…in numbers", "open word and…".
_APP_WORDS = ("excel", "numbers", "word", "powerpoint", "keynote", "pages", "outlook",
              "chrome", "notes", "reminders", "calendar", "safari")
_DOING = re.compile(r"\b(add|insert|create|make|write|put|fill|sort|rename|format|type|"
                    r"change|update|set|move|copy|paste|highlight|bold|new|append|draft)\b", re.I)
_APP_RE = re.compile(r"\b(" + "|".join(_APP_WORDS) + r")\b", re.I)


def asks_inside_an_app(text: str) -> bool:
    """"open excel add a column", "in keynote add a slide titled X" — an app named
    AND something to do in it. Opening alone is not this."""
    t = text or ""
    m = _APP_RE.search(t)
    if not m:
        return False
    rest = t[:m.start()] + t[m.end():]
    return bool(_DOING.search(rest))


_DIRECT = re.compile(
    r"^\W*(?:hey\s+|asta[,\s]+)?(?:can you\s+|could you\s+|please\s+|pls\s+)?"
    r"(?:open|in|on|using|go to)\s+(?:the\s+|my\s+)?(?P<app>" + "|".join(_APP_WORDS) + r")"
    r"(?:\s+app)?\s*(?:,|and|&|then)?\s+(?P<goal>.{3,400}?)\W*$", re.I | re.S)


def direct_ask(text: str) -> tuple[str, str] | None:
    """(app, goal) for "open excel and add a column Status" — said as one
    instruction, so no chat turn is needed to understand it."""
    m = _DIRECT.match(" ".join((text or "").split()))
    if not m or not _DOING.search(m.group("goal")):
        return None
    return m.group("app"), m.group("goal")
