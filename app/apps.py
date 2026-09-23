"""Hands, layer two: his apps through THEIR OWN doors.

Layer one wrote files. This is the other half of "access to files and apps": a
reminder in Reminders, an event in Calendar, a note in Notes, a draft sitting in
Mail waiting for him — put there by ASKING THE APPLICATION, not by moving his
mouse across a window that may have scrolled since the screenshot.

Three rules, and they are the whole design:

**A named recipe, never a free-form script.** A brain picks a recipe and fills in
its arguments; it does not write AppleScript. A model that can write arbitrary
AppleScript can empty his calendar, and no review of a generated script catches
the one time it is wrong. These recipes are small, read once by a person, and
after that only their arguments vary.

**Arguments are passed, never pasted.** `osascript` takes positional arguments
and the script reads them from `argv`. Interpolating a title into a script is
the same mistake as building SQL with `+` — and his titles are full of quotes,
apostrophes and em dashes as a matter of routine.

**Every write is read back.** The act is not "the script exited 0"; `osascript`
exits 0 having done nothing useful all the time. It is "the thing is there now,
and I looked". Each writing recipe carries a `verify` that reads the app again
and must find what was just written — the same bar as `files._check`, for the
same reason: otherwise he finds out in front of someone.

Off unless ASTA_APPS=1. These doors are real and they are on his own machine.
"""

from __future__ import annotations

import asyncio
import os
import plistlib
import re
import shutil
import time
from dataclasses import dataclass
from pathlib import Path

#: The interpreters. Constants so a test can point them at nothing, exactly as
#: conftest does for the audio switcher: a suite that writes to his Calendar is
#: not a suite.
OSASCRIPT = "/usr/bin/osascript"
SHORTCUTS = "/usr/bin/shortcuts"
OPEN = "/usr/bin/open"
#: Long enough for an app that has to launch, short enough that a stuck
#: permission dialog does not hold his turn open.
TIMEOUT = 25


def enabled() -> bool:
    return os.environ.get("ASTA_APPS", "").strip() == "1"


@dataclass(frozen=True)
class Recipe:
    name: str
    app: str
    #: What it does, in his terms. This is what a brain reads when choosing.
    does: str
    #: AppleScript. Arguments arrive as `argv`; never interpolated.
    script: str
    #: The arguments it expects, in order. A name ending in "?" is optional.
    takes: tuple[str, ...] = ()
    #: True when it changes something in the app.
    writes: bool = False
    #: Reads the app back, with the same arguments. For a write, the act only
    #: counts if this output contains the argument named by `verify_has`.
    verify: str = ""
    verify_has: str = ""

    @property
    def required(self) -> tuple[str, ...]:
        return tuple(a for a in self.takes if not a.endswith("?"))

    @property
    def argument_names(self) -> tuple[str, ...]:
        return tuple(a.rstrip("?") for a in self.takes)


#: Reminders keeps one list per name; "Reminders" is the default list on a Mac
#: that has never been organised, which is his.
_REMINDER_ADD = '''
on run argv
  set theTitle to item 1 of argv
  set theNote to item 2 of argv
  set dueText to item 3 of argv
  tell application "Reminders"
    set theList to default list
    set props to {name:theTitle}
    if theNote is not "" then set props to props & {body:theNote}
    if dueText is not "" then set props to props & {remind me date:my parseStamp(dueText)}
    make new reminder at end of theList with properties props
  end tell
end run

on parseStamp(t)
  set y to (text 1 thru 4 of t) as integer
  set mo to (text 6 thru 7 of t) as integer
  set d to (text 9 thru 10 of t) as integer
  set h to (text 12 thru 13 of t) as integer
  set mi to (text 15 thru 16 of t) as integer
  set theDate to current date
  set year of theDate to y
  set month of theDate to mo
  set day of theDate to d
  set hours of theDate to h
  set minutes of theDate to mi
  set seconds of theDate to 0
  return theDate
end parseStamp
'''

_REMINDER_LIST = '''
on run argv
  tell application "Reminders"
    set out to ""
    repeat with r in (reminders of default list whose completed is false)
      set out to out & (name of r) & linefeed
    end repeat
    return out
  end tell
end run
'''

_EVENT_ADD = '''
on run argv
  set theTitle to item 1 of argv
  set startText to item 2 of argv
  set minutesLong to (item 3 of argv) as integer
  set startsAt to my parseStamp(startText)
  tell application "Calendar"
    tell calendar 1
      make new event with properties {summary:theTitle, start date:startsAt, ¬
        end date:(startsAt + minutesLong * minutes)}
    end tell
  end tell
end run

on parseStamp(t)
  set y to (text 1 thru 4 of t) as integer
  set mo to (text 6 thru 7 of t) as integer
  set d to (text 9 thru 10 of t) as integer
  set h to (text 12 thru 13 of t) as integer
  set mi to (text 15 thru 16 of t) as integer
  set theDate to current date
  set year of theDate to y
  set month of theDate to mo
  set day of theDate to d
  set hours of theDate to h
  set minutes of theDate to mi
  set seconds of theDate to 0
  return theDate
end parseStamp
'''

_EVENT_LIST = '''
on run argv
  set startText to item 2 of argv
  set dayStart to my parseStamp((text 1 thru 10 of startText) & " 00:00")
  tell application "Calendar"
    set out to ""
    repeat with c in calendars
      repeat with e in (events of c whose start date is greater than dayStart ¬
        and start date is less than (dayStart + 2 * days))
        set out to out & (summary of e) & linefeed
      end repeat
    end repeat
    return out
  end tell
end run

on parseStamp(t)
  set y to (text 1 thru 4 of t) as integer
  set mo to (text 6 thru 7 of t) as integer
  set d to (text 9 thru 10 of t) as integer
  set h to (text 12 thru 13 of t) as integer
  set mi to (text 15 thru 16 of t) as integer
  set theDate to current date
  set year of theDate to y
  set month of theDate to mo
  set day of theDate to d
  set hours of theDate to h
  set minutes of theDate to mi
  set seconds of theDate to 0
  return theDate
end parseStamp
'''

_NOTE_ADD = '''
on run argv
  set theTitle to item 1 of argv
  set theBody to item 2 of argv
  tell application "Notes"
    make new note at folder 1 of account 1 with properties ¬
      {name:theTitle, body:("<div><b>" & theTitle & "</b></div><div>" & theBody & "</div>")}
  end tell
end run
'''

_NOTE_LIST = '''
on run argv
  tell application "Notes"
    set out to ""
    repeat with n in notes of folder 1 of account 1
      set out to out & (name of n) & linefeed
    end repeat
    return out
  end tell
end run
'''

#: A DRAFT, in the mail client he ACTUALLY uses. Apple Mail is on his Mac and
#: empty; his work mail is Outlook, which is why the Apple Mail recipe was the
#: wrong door — right mechanism, wrong application, and he would have found out
#: by opening Mail and seeing nothing.
#:
#: `send` is deliberately absent from both: an outward act has a gate of its own
#: (`prepare_to_send`), and a recipe that could send would put a second, ungated
#: route to the same act on his machine.
_OUTLOOK_DRAFT = '''
on run argv
  set theTo to item 1 of argv
  set theSubject to item 2 of argv
  set theBody to item 3 of argv
  tell application "Microsoft Outlook"
    set msg to make new outgoing message with properties ¬
      {subject:theSubject, content:theBody}
    make new recipient at msg with properties {email address:{address:theTo}}
    open msg
  end tell
end run
'''

_OUTLOOK_DRAFTS = '''
on run argv
  tell application "Microsoft Outlook"
    set out to ""
    repeat with m in (messages of drafts folder)
      set out to out & (subject of m) & linefeed
    end repeat
    return out
  end tell
end run
'''

_MAIL_DRAFT = '''
on run argv
  set theTo to item 1 of argv
  set theSubject to item 2 of argv
  set theBody to item 3 of argv
  tell application "Mail"
    set msg to make new outgoing message with properties ¬
      {subject:theSubject, content:theBody, visible:true}
    tell msg to make new to recipient at end of to recipients ¬
      with properties {address:theTo}
  end tell
end run
'''

_MAIL_DRAFTS = '''
on run argv
  tell application "Mail"
    set out to ""
    repeat with m in (messages of drafts mailbox)
      set out to out & (subject of m) & linefeed
    end repeat
    return out
  end tell
end run
'''

_REVEAL = '''
on run argv
  set thePath to item 1 of argv
  tell application "Finder"
    reveal POSIX file thePath as alias
    activate
  end tell
end run
'''

_SELECTION = '''
on run argv
  tell application "Finder"
    set out to ""
    repeat with i in (get selection)
      set out to out & (name of i as text) & linefeed
    end repeat
    return out
  end tell
end run
'''

RECIPES: dict[str, Recipe] = {r.name: r for r in (
    Recipe("reminder_add", "Reminders",
           "Put a reminder in his default Reminders list "
           "(due as 'YYYY-MM-DD HH:MM' — set it whenever he named a time)",
           _REMINDER_ADD, takes=("title", "note?", "due?"), writes=True,
           verify=_REMINDER_LIST, verify_has="title"),
    Recipe("reminders_open", "Reminders",
           "What is on his Reminders list right now (reads nothing else)",
           _REMINDER_LIST),
    Recipe("calendar_event_add", "Calendar",
           "Create an event in his default calendar "
           "(start as 'YYYY-MM-DD HH:MM', length in minutes)",
           _EVENT_ADD, takes=("title", "start", "minutes"), writes=True,
           verify=_EVENT_LIST, verify_has="title"),
    Recipe("note_add", "Notes",
           "Write a note into Notes",
           _NOTE_ADD, takes=("title", "body"), writes=True,
           verify=_NOTE_LIST, verify_has="title"),
    Recipe("outlook_draft", "Microsoft Outlook",
           "Leave a DRAFT email open in Outlook — his work mail, never sent from here",
           _OUTLOOK_DRAFT, takes=("to", "subject", "body"), writes=True,
           verify=_OUTLOOK_DRAFTS, verify_has="subject"),
    Recipe("mail_draft", "Mail",
           "Leave a DRAFT in Apple Mail — only for personal mail; work mail is Outlook",
           _MAIL_DRAFT, takes=("to", "subject", "body"), writes=True,
           verify=_MAIL_DRAFTS, verify_has="subject"),
    Recipe("reveal_file", "Finder",
           "Show a file in Finder, selected and in front",
           _REVEAL, takes=("path",), writes=True,
           verify=_SELECTION, verify_has="path"),
)}


class AppError(RuntimeError):
    """The door did not do what it was asked, in words he can act on."""


async def _osascript(script: str, args: list[str]) -> str:
    if not shutil.which(OSASCRIPT) and not os.path.exists(OSASCRIPT):
        raise AppError(f"no {OSASCRIPT} on this machine — app doors are macOS only")
    proc = await asyncio.create_subprocess_exec(
        OSASCRIPT, "-", *args,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE)
    try:
        out, err = await asyncio.wait_for(
            proc.communicate(script.encode()), timeout=TIMEOUT)
    except asyncio.TimeoutError:
        proc.kill()
        raise AppError(f"the app did not answer within {TIMEOUT}s — it may be "
                       "showing a permission dialog on his screen")
    if proc.returncode != 0:
        why = err.decode(errors="replace").strip().splitlines()
        tail = why[-1] if why else f"exit {proc.returncode}"
        # Two different switches, and naming the wrong one leaves the real one
        # off. -1719 ("not allowed assistive access") is ACCESSIBILITY — needed
        # to click and type through System Events. -1743 ("not authorised to send
        # Apple events") is AUTOMATION — needed to ask an app to do something.
        # Both were once reported as Automation; the first live screen call sent
        # him to the wrong page.
        if "-1719" in tail or "assistive access" in tail.lower():
            raise AppError(
                "macOS has not let Asta click and type yet — System Settings → "
                "Privacy & Security → Accessibility, and switch on the Python that "
                f"runs Asta ({tail})")
        if "-1743" in tail or "not allowed" in tail.lower() or "not authori" in tail.lower():
            raise AppError(
                f"macOS has not granted Asta access to that app yet — "
                f"System Settings → Privacy & Security → Automation ({tail})")
        raise AppError(tail[:300])
    return out.decode(errors="replace").strip()


async def run(name: str, **args) -> dict:
    """Do one recipe, and for a write, read the app back before saying it is done."""
    if not enabled():
        raise AppError("app doors are off — set ASTA_APPS=1 to let Asta use them")
    recipe = RECIPES.get(name)
    if recipe is None:
        raise AppError(f"no such recipe: {name}. Have: {', '.join(sorted(RECIPES))}")
    missing = [a for a in recipe.required if not str(args.get(a, "")).strip()]
    if missing:
        raise AppError(f"{name} needs {', '.join(missing)}")
    argv = [str(args.get(a, "")) for a in recipe.argument_names]

    from . import policy
    verdict = policy.check("app", recipe.app.lower(), asked=bool(args.get("asked")))
    if not verdict.ok:
        raise AppError(f"a rule of his stops that: {verdict.why}")

    said = await _osascript(recipe.script, argv)
    if not (recipe.writes and recipe.verify):
        # What the app ANSWERED. A reading recipe that returns only "ok" tells a
        # brain nothing — "what is on my reminders list" would come back as the
        # fact that it was asked, which is how a tool ends up being described
        # rather than used.
        return {"ok": True, "app": recipe.app, "did": recipe.does,
                "said": said[:4000]}

    # Read it back through the same door. `reveal_file` is checked on the file's
    # NAME rather than its path, because that is what Finder reports.
    want = str(args.get(recipe.verify_has, ""))
    if recipe.name == "reveal_file":
        want = os.path.basename(want)
    seen = await _osascript(recipe.verify, argv)
    if want and want not in seen:
        raise AppError(
            f"{recipe.app} did not come back with {want!r} after the change — "
            f"treating that as not done rather than reporting a success")
    return {"ok": True, "app": recipe.app, "did": recipe.does, "verified": want}


async def run_shortcut(name: str, text: str = "") -> str:
    """Run one of HIS Shortcuts by name — the door he can extend without me.

    A shortcut's effect cannot be read back generically (it might have posted to
    an app Asta has never heard of), so its own output IS the verification, and
    a shortcut that returns nothing is reported as run-but-silent rather than as
    a confirmed success. That distinction is the point of this whole layer.
    """
    if not enabled():
        raise AppError("app doors are off — set ASTA_APPS=1 to let Asta use them")
    if not (shutil.which(SHORTCUTS) or os.path.exists(SHORTCUTS)):
        raise AppError("the shortcuts command is missing — macOS 12 or newer has it")
    args = [SHORTCUTS, "run", name]
    if text:
        args += ["--input-path", "-"]
    proc = await asyncio.create_subprocess_exec(
        *args, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE)
    try:
        out, err = await asyncio.wait_for(
            proc.communicate(text.encode() if text else None), timeout=TIMEOUT)
    except asyncio.TimeoutError:
        proc.kill()
        raise AppError(f"shortcut {name!r} did not finish within {TIMEOUT}s")
    if proc.returncode != 0:
        tail = err.decode(errors="replace").strip().splitlines()
        raise AppError((tail[-1] if tail else f"exit {proc.returncode}")[:300])
    return out.decode(errors="replace").strip()


async def probe() -> dict:
    """Which doors actually answer on this machine, without changing anything."""
    out: dict[str, str] = {}
    for name in ("Reminders", "Calendar", "Notes", "Mail", "Finder"):
        script = ('on run argv\n  tell application "System Events" to return '
                  f'(exists application process "{name}") as text\nend run')
        try:
            out[name] = await _osascript(script, [])
        except AppError as exc:
            out[name] = f"error: {exc}"
    return out


def catalogue() -> str:
    """The recipes, as a brain reads them when choosing one."""
    lines = []
    for r in RECIPES.values():
        takes = ", ".join(r.takes) or "—"
        lines.append(f"{r.name} ({r.app}): {r.does}. takes: {takes}"
                     f"{' [writes, read back after]' if r.writes else ''}")
    return "\n".join(lines)


# --- opening anything he names ---------------------------------------------------------
#
# "i ask to open intelij, open chrome, open youtube it has to open". The recipes
# above are for putting something INTO an app; this is just "put it in front of
# me", which needs no script at all — `open -a` does it, and the honest part is
# afterwards: an app that did not start must be reported as not started.

#: Where macOS keeps applications. Utilities is listed because it is where a
#: fair number of the things he asks for by name actually live.
APP_FOLDERS = ("/Applications", "/Applications/Utilities",
               "/System/Applications", "/System/Applications/Utilities",
               "~/Applications")

#: Sites by the name he says rather than the address he would type. Deliberately
#: short and public: anything with a dot in it is opened as typed, so his own
#: systems need no entry here (and this repo carries no internal hostnames).
SITES = {
    "youtube": "https://www.youtube.com",
    "google": "https://www.google.com",
    "gmail": "https://mail.google.com",
    "github": "https://github.com",
    "google drive": "https://drive.google.com",
    "google maps": "https://maps.google.com",
    "stack overflow": "https://stackoverflow.com",
}

#: Browsers, by the name he would use for them.
BROWSERS = {"chrome": "Google Chrome", "google chrome": "Google Chrome",
            "safari": "Safari", "firefox": "Firefox", "edge": "Microsoft Edge",
            "brave": "Brave Browser", "arc": "Arc"}

_apps_cache: tuple[float, list[Path]] = (0.0, [])
APPS_TTL = 300


def installed_apps(refresh: bool = False) -> list[Path]:
    """Every application on this machine, cached — a scan per turn is wasteful
    and the list changes about once a month."""
    global _apps_cache
    when, found = _apps_cache
    if found and not refresh and time.time() - when < APPS_TTL:
        return found
    out: list[Path] = []
    for folder in APP_FOLDERS:
        base = Path(os.path.expanduser(folder))
        try:
            out += [p for p in base.iterdir() if p.suffix == ".app"]
        except OSError:
            continue
    out.sort(key=lambda p: p.name.lower())
    _apps_cache = (time.time(), out)
    return out


def _norm(text: str) -> str:
    return re.sub(r"[^a-z0-9 ]+", "", (text or "").lower()).strip()


def find_app(name: str) -> tuple[Path | None, list[str]]:
    """The app he means, and the other candidates.

    He types "intellij" and this machine has both IntelliJ IDEA and IntelliJ
    IDEA CE. The shortest name that contains what he wrote is the plain one,
    which is the one people mean — but the alternatives are handed back so the
    answer can say which was chosen and what else was there.
    """
    want = _norm(name)
    if not want:
        return None, []
    apps = installed_apps()
    exact = [p for p in apps if _norm(p.stem) == want]
    if exact:
        return exact[0], [p.stem for p in exact[1:]]
    starts = [p for p in apps if _norm(p.stem).startswith(want)]
    holds = [p for p in apps if want in _norm(p.stem) and p not in starts]
    # Every word he wrote, in any order: "teams microsoft" is Microsoft Teams.
    words = want.split()
    loose = [p for p in apps
             if p not in starts and p not in holds
             and all(w in _norm(p.stem) for w in words)]
    ranked = sorted(starts, key=lambda p: len(p.stem)) + \
        sorted(holds, key=lambda p: len(p.stem)) + sorted(loose, key=lambda p: len(p.stem))
    if ranked:
        return ranked[0], [p.stem for p in ranked[1:4]]
    import difflib
    near = difflib.get_close_matches(want, [_norm(p.stem) for p in apps], n=3, cutoff=0.6)
    return None, [p.stem for p in apps if _norm(p.stem) in near]


def bundle_id(app: Path) -> str:
    """An app's identifier, read from its own Info.plist — no subprocess."""
    try:
        with open(app / "Contents" / "Info.plist", "rb") as fh:
            return str(plistlib.load(fh).get("CFBundleIdentifier") or "")
    except (OSError, ValueError):
        return ""


async def _run(*argv: str) -> tuple[int, str]:
    proc = await asyncio.create_subprocess_exec(
        *argv, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=TIMEOUT)
    except asyncio.TimeoutError:
        proc.kill()
        return 124, f"{argv[0]} did not finish within {TIMEOUT}s"
    return proc.returncode, out.decode(errors="replace").strip()


async def running_bundles() -> set[str] | None:
    """The identifiers of everything running right now, or None when macOS will
    not say.

    Read from System Events rather than by asking each app whether it is
    running — that question LAUNCHES an app that is not, which would make the
    check its own answer.

    None is not an empty set. Until this Mac allows Asta to ask System Events
    (Privacy & Security → Automation), the honest answer is "I cannot tell",
    and reporting that as "it did not open" would call every successful launch
    a failure.
    """
    script = ('on run argv\n  tell application "System Events" to return '
              '(bundle identifier of every process) as text\nend run')
    try:
        said = await _osascript(script, [])
    except AppError:
        return None
    return {b.strip() for b in said.split(",") if b.strip()}


#: How long to wait for an app to appear after asking macOS to open it. A cold
#: IntelliJ takes several seconds; a test sets this to a fraction.
START_WAIT = 12.0


async def _became_running(bid: str, seconds: float | None = None) -> bool | None:
    """Wait for it to actually appear: True, False, or None when we cannot see."""
    if not bid:
        return None
    deadline = time.time() + (START_WAIT if seconds is None else seconds)
    seen = None
    while time.time() < deadline:
        seen = await running_bundles()
        if seen is None:
            return None                  # macOS will not answer; do not guess
        if bid.lower() in {b.lower() for b in seen}:
            return True
        await asyncio.sleep(0.5)
    return False


#: Said once, where it can be acted on: the check needs this permission.
BLIND = ("opened, but I can't confirm it is running — this Mac has not allowed "
         "Asta to ask System Events (Privacy & Security → Automation → python3.13)")


async def open_app(name: str, asked: bool = True) -> dict:
    """Bring an application on his Mac to the front, and check it really came."""
    if not enabled():
        raise AppError("app doors are off — set ASTA_APPS=1 to let Asta use them")
    app, others = find_app(name)
    if app is None:
        near = f" Closest I can see: {', '.join(others)}." if others else ""
        raise AppError(f"no app called {name!r} on this Mac.{near}")
    from . import policy
    verdict = policy.check("app", app.stem.lower(), asked=asked)
    if not verdict.ok:
        raise AppError(f"a rule of his stops that: {verdict.why}")
    code, said = await _run(OPEN, "-a", str(app))
    if code != 0:
        raise AppError(f"could not open {app.stem}: {said[:200] or f'open exited {code}'}")
    seen = await _became_running(bundle_id(app))
    said = {True: f"{app.stem} is open.",
            None: f"{app.stem} {BLIND}.",
            False: f"Asked macOS to open {app.stem}, but it is not running — "
                   f"reporting that rather than assuming."}[seen]
    return {"ok": seen is not False, "verified": seen, "app": app.stem,
            "others": others, "said": said}


async def open_url(where: str, browser: str = "") -> dict:
    """Open a site. `browser` names one, otherwise his default takes it."""
    if not enabled():
        raise AppError("app doors are off — set ASTA_APPS=1 to let Asta use them")
    url = site_url(where) or where.strip()
    if not re.match(r"^[a-z][a-z0-9+.-]*://", url):
        url = "https://" + url.lstrip("/")
    if not re.match(r"^https?://[^\s/]+\.[^\s/]+", url):
        raise AppError(f"{where!r} is not a site I can open")
    argv = [OPEN, url]
    app = None
    if browser:
        app, _ = find_app(BROWSERS.get(_norm(browser), browser))
        if app is None:
            raise AppError(f"no browser called {browser!r} on this Mac")
        argv = [OPEN, "-a", str(app), url]
    code, said = await _run(*argv)
    if code != 0:
        raise AppError(f"could not open {url}: {said[:200] or f'open exited {code}'}")
    # With no browser named, the honest check is that SOME browser is now up:
    # which one is his default is LaunchServices' business, not Asta's.
    if app is not None:
        seen = await _became_running(bundle_id(app))
        where_at = f" in {app.stem}"
    else:
        running = await running_bundles()
        where_at = ""
        seen = None if running is None else any(
            b.lower().startswith(("com.google.chrome", "com.apple.safari", "org.mozilla",
                                  "com.microsoft.edgemac", "com.brave.browser",
                                  "company.thebrowser")) for b in running)
    said = {True: f"Opened {url}{where_at}.",
            None: f"{url}{where_at} {BLIND}.",
            False: f"Asked macOS to open {url}, but I cannot see a browser running — "
                   f"reporting that rather than assuming."}[seen]
    return {"ok": seen is not False, "verified": seen, "url": url,
            "app": app.stem if app else "", "said": said}


def site_url(name: str) -> str:
    """The address for a site he names ("youtube"), or '' when it is not one."""
    key = _norm(name)
    if key in SITES:
        return SITES[key]
    # A bare domain he typed: "youtube.com", "docs.python.org/3".
    if re.match(r"^(?:https?://)?[\w.-]+\.[a-z]{2,}(?:[/?#]\S*)?$", name.strip(), re.I):
        return name.strip()
    return ""


#: Searching a site he names. Only the explicit forms: "search youtube for X"
#: and "search for X on youtube". Never a bare "google the error" — that is
#: usually him asking Asta to find out, not to open a browser window.
SEARCHES = {
    "youtube": "https://www.youtube.com/results?search_query={q}",
    "google": "https://www.google.com/search?q={q}",
    "github": "https://github.com/search?q={q}",
    "stack overflow": "https://stackoverflow.com/search?q={q}",
    "stackoverflow": "https://stackoverflow.com/search?q={q}",
}
_SITES_RE = "|".join(sorted((re.escape(k) for k in SEARCHES), key=len, reverse=True))
_SEARCH_ASK = (
    re.compile(rf"^\W*(?:can you\s+|could you\s+|please\s+)?(?:search|look up|find)\s+"
               rf"(?:on\s+|in\s+)?(?P<site>{_SITES_RE})\s+for\s+(?P<q>.{{1,120}}?)"
               rf"(?:\s+(?:in|on|with)\s+(?P<browser>chrome|safari|firefox|edge|brave|arc))?"
               r"\W*$", re.I),
    re.compile(rf"^\W*(?:can you\s+|could you\s+|please\s+)?(?:search|look up|find)\s+"
               rf"(?:for\s+)?(?P<q>.{{1,120}}?)\s+(?:on|in)\s+(?P<site>{_SITES_RE})"
               rf"(?:\s+(?:in|on|with)\s+(?P<browser>chrome|safari|firefox|edge|brave|arc))?"
               r"\W*$", re.I),
)


def search_url(text: str) -> tuple[str, str] | None:
    """(url, browser) when he is asking for a search on a site he named."""
    from urllib.parse import quote_plus
    t = " ".join((text or "").split())
    for pattern in _SEARCH_ASK:
        m = pattern.match(t)
        if m:
            q = m.group("q").strip(" .!?,")
            if not q:
                return None
            return (SEARCHES[_norm(m.group("site"))].format(q=quote_plus(q)),
                    (m.group("browser") or ""))
    return None


#: "open X", "launch X", "fire up X" — and nothing else. A verb list rather
#: than a brain, because this has to be instant and it has to be predictable.
_OPEN_ASK = re.compile(
    r"^\W*(?:hey\s+|asta[,\s]+)?(?:can you\s+|could you\s+|please\s+|pls\s+)?"
    r"(?:open|launch|start|fire up|bring up|show me)\s+"
    r"(?:the\s+|my\s+)?(?P<what>.{1,60}?)"
    r"(?:\s+(?:in|on|with)\s+(?P<browser>chrome|safari|firefox|edge|brave|arc))?"
    r"(?:\s+(?:for me|please|now))?\W*$", re.I)

#: Words that say he wants what is INSIDE an app, not the app in front of him.
#: "open my reminders" is a question about his list; the recipes answer those.
#: "open reminders" is not — that one is just a window.
_ABOUT_CONTENT = re.compile(r"\b(my|list|todo|inbox|unread|pr|ticket|task|log|doc|"
                            r"file|folder|draft)\b", re.I)


def open_ask(text: str) -> tuple[str, str, str] | None:
    """("app"|"url", what, browser) when he is asking for something to be opened.

    Deliberately narrow. Anything with a path in it, anything that reads as a
    question about content, and anything longer than a few words is left to a
    brain — "open the PR" is not a launch.
    """
    t = " ".join((text or "").split())
    if not t:
        return None
    found = search_url(t)
    if found:
        return ("url", found[0], found[1])
    if t.endswith("?"):
        return None                      # a question about what is open is not an ask
    m = _OPEN_ASK.match(t)
    if not m:
        return None
    what = m.group("what").strip(" .!,")
    browser = (m.group("browser") or "").strip()
    if not what or len(what.split()) > 4:
        return None
    if site_url(what):
        return ("url", what, browser)
    if _ABOUT_CONTENT.search(t):
        return None                      # about content, not about a window
    if find_app(what)[0] is None:
        return None                      # not installed: let a brain say something useful
    return ("app", what, browser)


async def open_it(kind: str, what: str, browser: str = "") -> str:
    """One line for him: what was opened, or what happened instead."""
    try:
        out = await (open_url(what, browser) if kind == "url" else open_app(what))
    except AppError as exc:
        return f"⚠️ {exc}"
    line = out["said"]
    if out.get("others"):
        line += f" (also installed: {', '.join(out['others'])})"
    return ("🖥 " if out["verified"] is not False else "⚠️ ") + line
