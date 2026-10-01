"""Hands, layer three: the screen and the mouse — the fallback, and the last one.

Layers one and two do the work properly: code writes the file, the application is
asked in its own language. This layer exists for the app that has neither — no
API, no AppleScript dictionary, nothing but a window. It is deliberately the
least-preferred route, and it is built so that being wrong is loud.

**Elements, never coordinates.** A click at (412, 180) is a click at whatever
happens to be there after a scroll, a notification banner, or a different screen
size. Every step here names what it is clicking — `button "Send"`, `menu item
"New Note"` — and macOS's accessibility tree resolves it. If the name is not on
screen, nothing is clicked and the step says so.

**Every click states what it expects, and the expectation is checked.** A step is
`do this, and afterwards THIS must be true` — a window appears, a button is gone,
a field holds the text. A click whose expectation does not come true is a failed
step, not a step that happened to do nothing; the sequence stops there rather
than typing into whatever now has focus. This is the difference between
automation and a machine mashing keys at a screen it cannot read.

**A path that worked is saved, so it is walked once.** The first time is careful
and slow; afterwards it is a named recipe he can read, and the model is not
re-deriving anything.

Off unless ASTA_SCREEN=1, and refused when a real door exists for the same app:
falling back to the mouse while the application is sitting there answering
AppleScript is how a reliable act gets replaced by a fragile one.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field

from . import apps, store

#: Named UI paths that worked, so the second time is a replay rather than a hunt.
PATHS_KEY = "screen_paths"


def enabled() -> bool:
    return os.environ.get("ASTA_SCREEN", "1").strip() == "1"


class ScreenError(RuntimeError):
    """A step did not do what it said it would, in words he can act on."""


@dataclass
class Step:
    """One act, and what must be true afterwards.

    `expect` is not optional in spirit — a step without one is a click into the
    dark — so `follow` refuses a sequence whose last step has nothing to check.
    """
    do: str                      # click | menu | type | key
    target: str = ""             # 'button "Send"', 'File > New Note', or the text
    expect: str = ""             # 'exists: button "Reply"' | 'gone: …' | 'window: …'
    window: int = 1

    def render(self) -> str:
        return f"{self.do} {self.target}" + (f" → expect {self.expect}" if self.expect else "")


def _tree_script(process: str) -> str:
    """Everything named or holding a value on screen, as one flat list the checks
    can search.

    Two things the first live run taught. The element list must be FETCHED
    (`set els to (get entire contents of w)`) before it is walked: iterating
    `entire contents` directly hands back lazy references that System Events
    cannot resolve, every property read fails, and the tree came back holding
    nothing but window titles — so a click that worked read as a click that did
    not. And a text area keeps what was typed as its VALUE, not its name.
    """
    return f'''
on run argv
  tell application "System Events"
    if not (exists process "{process}") then return "NO-PROCESS"
    tell process "{process}"
      set out to "windows: " & (count of windows) & linefeed
      repeat with w in windows
        set out to out & "window: " & (name of w as text) & linefeed
        -- A big Swing/Electron window (IntelliJ) fails `entire contents` with
        -- -10000; its top level is still worth reading.
        try
          set els to (get entire contents of w)
        on error
          set els to (get UI elements of w)
        end try
        repeat with i from 1 to (count of els)
          set e to item i of els
          try
            set r to (role description of e) as text
            set n to ""
            try
              set n to (name of e) as text
            end try
            if n is "missing value" then set n to ""
            if n is not "" then set out to out & r & ": " & n & linefeed
            set v to ""
            try
              set v to (value of e) as text
            end try
            if v is not "" and v is not "missing value" and v is not n then
              if (length of v) > 300 then set v to text 1 thru 300 of v
              set out to out & r & " value: " & v & linefeed
            end if
          end try
        end repeat
      end repeat
      -- The menus, two levels: for an IDE they are the dependable way in.
      try
        repeat with m in (menu bar items of menu bar 1)
          set mn to (name of m) as text
          if mn is not "Apple" then
            set out to out & "menu: " & mn & linefeed
            try
              repeat with mi in (menu items of menu 1 of m)
                set itn to (name of mi) as text
                if itn is not "missing value" and itn is not "" then
                  set out to out & "  " & mn & " > " & itn & linefeed
                end if
              end repeat
            end try
          end if
        end repeat
      end try
      return out
    end tell
  end tell
end run
'''


def _shallow_script(process: str) -> str:
    """Window titles, the front window's top-level controls and the whole menu
    map — each fetched in ONE AppleEvent. Reading an IDE element by element took
    over 25s and failed; batched, IntelliJ's 110 menu items come back in ~5s."""
    return f'''
on run argv
  tell application "System Events"
    if not (exists process "{process}") then return "NO-PROCESS"
    tell process "{process}"
      set out to "windows: " & (count of windows) & linefeed
      repeat with n in (name of every window)
        try
          set out to out & "window: " & (n as text) & linefeed
        end try
      end repeat
      try
        repeat with n in (name of every UI element of front window)
          try
            set t to (n as text)
            if t is not "missing value" and t is not "" then set out to out & "element: " & t & linefeed
          end try
        end repeat
      end try
      try
        set mbn to name of every menu bar item of menu bar 1
        set mis to name of every menu item of menu 1 of every menu bar item of menu bar 1
        repeat with k from 2 to (count of mbn)
          set mn to (item k of mbn) as text
          set out to out & "menu: " & mn & linefeed
          repeat with oneItem in (item k of mis)
            set itn to ""
            try
              set itn to oneItem as text
            end try
            if itn is not "missing value" and itn is not "" then set out to out & "  " & mn & " > " & itn & linefeed
          end repeat
        end repeat
      end try
      return out
    end tell
  end tell
end run
'''


#: Apps whose accessibility tree is too big to walk element by element.
BIG_APPS = {"intellij idea", "intellij idea ce", "visual studio code", "code",
            "microsoft teams", "pycharm", "webstorm"}
_SHALLOW_KEY = "screen_shallow:"


async def look(process: str) -> str:
    """What is on screen in that app, by name. Reads nothing else."""
    if not enabled():
        raise ScreenError("the screen fallback is off — ASTA_SCREEN=1 turns it on")
    shallow = process.lower() in BIG_APPS or bool(store.kv_get(_SHALLOW_KEY + process.lower()))
    if not shallow:
        try:
            out = await apps._osascript(_tree_script(process), [])
        except apps.AppError:
            # Too big to walk (-10000, or no answer in time): read it the fast
            # way, now and from now on.
            store.kv_set(_SHALLOW_KEY + process.lower(), "1")
            shallow = True
    if shallow:
        out = await apps._osascript(_shallow_script(process), [])
    if out.strip() == "NO-PROCESS":
        raise ScreenError(f"{process} is not running, so there is nothing to click")
    return out


def _click_script(process: str, target: str, window: int) -> str:
    return f'''
on run argv
  set theTarget to item 1 of argv
  tell application "System Events" to tell process "{process}"
    set frontmost to true
    click (first UI element of window {window} whose name is theTarget)
  end tell
end run
'''


def _menu_script(process: str, path: list[str]) -> str:
    """Click a menu path of ANY depth. The names travel as arguments, never
    inside the script: JSON-quoting "Find in Files…" wrote \\u2026, which
    AppleScript cannot parse, and a two-level template dropped the middle of
    "Edit > Find > Find in Files…" (both found live, 30 Sep)."""
    ref = "menu bar item (item 1 of argv) of menu bar 1"
    for i in range(2, len(path)):
        ref = f"menu item (item {i} of argv) of menu 1 of {ref}"
    return f'''
on run argv
  tell application "System Events" to tell process "{process}"
    set frontmost to true
    click menu item (item {len(path)} of argv) of menu 1 of {ref}
  end tell
end run
'''


#: Keys by name → macOS key code. "key escape" used to send the WORD to
#: `key code`, which takes only numbers.
KEY_CODES = {"return": 36, "enter": 36, "escape": 53, "esc": 53, "tab": 48, "space": 49,
             "delete": 51, "backspace": 51, "up": 126, "down": 125, "left": 123,
             "right": 124, "home": 115, "end": 119, "pageup": 116, "pagedown": 121}


def _key_script(process: str, code: int) -> str:
    return f'''
on run argv
  tell application "System Events" to tell process "{process}"
    set frontmost to true
    key code {int(code)}
  end tell
end run
'''


def _type_script(process: str, key: bool) -> str:
    verb = "key code" if key else "keystroke"
    return f'''
on run argv
  set theText to item 1 of argv
  tell application "System Events" to tell process "{process}"
    set frontmost to true
    {verb} theText
  end tell
end run
'''


def _paste_script(process: str) -> str:
    """Type anything, by way of the clipboard — and give the clipboard back.

    `keystroke` can only produce characters on the current keyboard layout. Live
    on 17 Sep an em dash made it type NOTHING, silently; the step's own check
    caught it ("did not come true") rather than reporting a success, which is
    the design working. His text is full of em dashes, so anything that is not
    plain ASCII is pasted instead. What was on his clipboard is restored after —
    taking it and not giving it back would be the screen layer quietly costing
    him something he copied.
    """
    return f'''
on run argv
  set theText to item 1 of argv
  set hadClip to true
  try
    set oldClip to the clipboard
  on error
    set hadClip to false
  end try
  set the clipboard to theText
  tell application "System Events" to tell process "{process}"
    set frontmost to true
    keystroke "v" using command down
  end tell
  delay 0.3
  if hadClip then set the clipboard to oldClip
end run
'''


def _needs_paste(text: str) -> bool:
    return any(ord(ch) > 126 for ch in text)


def _met(expect: str, seen: str) -> bool:
    """Is the expectation true of what is on screen now?"""
    kind, _, want = expect.partition(":")
    kind, want = kind.strip().lower(), want.strip()
    if not want:
        return False
    if kind == "windows":
        # How many windows are open — the check that works for apps (Java IDEs)
        # whose popups and dialogs expose no name. Exact, not a substring.
        import re as _re
        got = _re.search(r"^windows:\s*(\d+)", seen or "", _re.M)
        return bool(got) and want.isdigit() and int(got.group(1)) == int(want)
    there = want.lower() in seen.lower()
    return not there if kind == "gone" else there


async def follow(process: str, steps: list[Step], why: str = "") -> dict:
    """Walk a path, checking after every step. Stops at the first one that lies."""
    if not enabled():
        raise ScreenError("the screen fallback is off — ASTA_SCREEN=1 turns it on")
    if not steps:
        raise ScreenError("no steps to follow")
    if not steps[-1].expect:
        raise ScreenError(
            "the last step has nothing to check, so there would be no way to know "
            "whether any of this worked — give it an expect:")

    from . import policy
    verdict = policy.check("screen", process.lower())
    if not verdict.ok:
        raise ScreenError(f"a rule of his stops that: {verdict.why}")

    # The rule that keeps this a fallback rather than a habit. An app with a
    # scripting door answers the same question reliably; clicking at it instead
    # trades a verified act for a fragile one, and the fragility only shows up
    # later, on his screen, in front of someone.
    door = [r.name for r in apps.RECIPES.values()
            if r.app.lower() == process.lower()]
    if door:
        raise ScreenError(
            f"{process} has a proper door — use {', '.join(sorted(door)[:3])} "
            f"through use_app instead of clicking at it. The mouse is for apps "
            f"that have no other way in.")

    done: list[str] = []
    for i, step in enumerate(steps, start=1):
        if step.do == "click":
            await apps._osascript(_click_script(process, step.target, step.window),
                                  [step.target])
        elif step.do == "menu":
            path = [p.strip() for p in step.target.split(">")]
            if len(path) < 2:
                raise ScreenError(f"a menu path needs 'Menu > Item', got {step.target!r}")
            await apps._osascript(_menu_script(process, path), path)
        elif step.do == "type" and _needs_paste(step.target):
            await apps._osascript(_paste_script(process), [step.target])
        elif step.do == "key" and step.target.strip().lower() in KEY_CODES:
            await apps._osascript(_key_script(process, KEY_CODES[step.target.strip().lower()]), [])
        elif step.do in ("type", "key"):
            await apps._osascript(_type_script(process, False), [step.target])
        else:
            raise ScreenError(f"step {i}: no such action {step.do!r}")

        if step.expect:
            seen = await look(process)
            if not _met(step.expect, seen):
                raise ScreenError(
                    f"step {i} ({step.render()}) did not come true. Stopping here "
                    f"rather than carrying on into a window I cannot read. "
                    f"On screen now: {_shortlist(seen)}")
        done.append(step.render())
    return {"ok": True, "process": process, "steps": done, "why": why}


def _shortlist(seen: str, limit: int = 12) -> str:
    lines = [ln for ln in seen.splitlines() if ln.strip()][:limit]
    return " | ".join(lines) if lines else "(nothing named on screen)"


# --- paths worth keeping ----------------------------------------------------

def paths() -> dict:
    return json.loads(store.kv_get(PATHS_KEY) or "{}")


def remember_path(name: str, process: str, steps: list[Step], why: str = "") -> None:
    """Keep a sequence that worked, so the next time is a replay he can read."""
    kept = paths()
    kept[name] = {"process": process, "why": why,
                  "steps": [{"do": s.do, "target": s.target,
                             "expect": s.expect, "window": s.window} for s in steps]}
    store.kv_set(PATHS_KEY, json.dumps(kept))


async def replay(name: str) -> dict:
    kept = paths().get(name)
    if not kept:
        raise ScreenError(f"no saved path called {name!r}. Have: "
                          f"{', '.join(sorted(paths())) or 'none yet'}")
    steps = [Step(**s) for s in kept["steps"]]
    return await follow(kept["process"], steps, why=kept.get("why", ""))


def describe_paths() -> str:
    kept = paths()
    if not kept:
        return "No screen paths saved yet."
    return "\n".join(
        f"{name} ({p['process']}): " + " → ".join(
            f"{s['do']} {s['target']}" for s in p["steps"])
        for name, p in sorted(kept.items()))
