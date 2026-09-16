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
import shutil
from dataclasses import dataclass

#: The interpreters. Constants so a test can point them at nothing, exactly as
#: conftest does for the audio switcher: a suite that writes to his Calendar is
#: not a suite.
OSASCRIPT = "/usr/bin/osascript"
SHORTCUTS = "/usr/bin/shortcuts"
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
        if "-1743" in tail or "not allowed" in tail.lower():
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
