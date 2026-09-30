"""A code review he can sit through: the change, step by step, in IntelliJ.

His ask, 30 Sep: "once code is done, open IntelliJ, explain the changes to me
one by one, routing the exact flow in code — a new API from the controller
till the end — and take my corrections in parallel, to work on later. An
interactive review session."

    walk me through task 126          → the session starts; step 1 opens
    next / back / again               → move; IntelliJ follows, at the line
    why is this null-checked here?    → answered about THIS step's code
    this should use the enum instead  → noted against this step (nothing changes)
    done                              → the notes, and "apply them?"
    apply                             → the SAME task continues with the notes
                                        (tasks.refine: same session, same branch)

The order is the request's path through the code, not the diff's file order:
the change is read once by a model that returns the steps from entry point to
the end (controller → service → repository / client / Kafka / Temporal), each
with its file, line and a short explanation that says how it hands on to the
next. Nothing here edits code; applying notes is the task's own code path,
behind its own rules.

State is one kv row per conversation, so a restart does not lose his place.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path

from . import store

_KEY = "walkthrough:"
IDEA = os.environ.get("ASTA_IDEA_BIN", "") or "/Applications/IntelliJ IDEA.app/Contents/MacOS/idea"
DIFF_CHARS = 60_000

_START = re.compile(
    r"\b(?:walk\s+(?:me\s+)?through|review\s+session|code\s+walk\s*through|"
    r"explain\s+(?:me\s+)?(?:the\s+)?(?:code\s+)?changes?(?:\s+one\s+by\s+one)?|"
    r"take\s+me\s+through)\b", re.I)
_TASK = re.compile(r"\btask\s*#?\s*(\d{1,5})\b|(?<![\w/])#(\d{2,5})\b", re.I)
_PR = re.compile(r"https://github\.com/[\w.-]+/[\w.-]+/pull/\d+")

_NEXT = re.compile(r"^\W*(?:next|n|ok(?:ay)?\s*next|go\s+on|continue|got\s+it|ok(?:ay)?|"
                   r"fine|yes|yep|done\s+with\s+this|move\s+on)\W*$", re.I)
_BACK = re.compile(r"^\W*(?:back|previous|prev|go\s+back)\W*$", re.I)
_AGAIN = re.compile(r"^\W*(?:again|repeat|say\s+(?:it\s+)?again|show\s+again)\W*$", re.I)
_DONE = re.compile(r"^\W*(?:done|stop|finish(?:ed)?|end(?:\s+(?:review|session))?|"
                   r"that'?s\s+(?:all|it)|close\s+(?:the\s+)?(?:review|session))\W*$", re.I)
_APPLY = re.compile(r"^\W*(?:apply(?:\s+(?:them|it|the\s+notes|all))?|yes,?\s+apply|"
                    r"go\s+ahead(?:\s+and\s+apply)?|fix\s+them)\W*$", re.I)
_LATER = re.compile(r"^\W*(?:later|not\s+now|keep\s+(?:them|it)|save\s+(?:them|it)|no)\W*$", re.I)
_QUESTION = re.compile(r"\?\s*$|^\W*(?:why|what|how|where|which|who|when|is|are|does|do|"
                       r"can|could|explain|tell\s+me)\b", re.I)
#: Words that make a sentence a change he wants — a note, not a question.
_CORRECTION = re.compile(
    r"\b(?:should|shouldn'?t|must|instead|rename|remove|delete|drop|add|use|move|"
    r"extract|replace|change|don'?t|do\s+not|avoid|make\s+(?:it|this)|needs?\s+to|"
    r"missing|wrong|null[- ]?safe|validate|handle|log|test|refactor)\b", re.I)


_SPEAK_ASK = re.compile(r"\b(?:speak|voice|aloud|out\s+loud|verbally|say\s+it|talk\s+me)\b", re.I)
_VOICE_ON = re.compile(r"^\W*(?:voice\s+on|speak(?:\s+it)?|unmute|talk|read\s+it\s+out)\W*$", re.I)
_VOICE_OFF = re.compile(r"^\W*(?:voice\s+off|mute|stop\s+talking|silent|quiet|no\s+voice)\W*$", re.I)


def wants_voice(text: str) -> bool:
    """"walk me through task 126 and speak" — or ASTA_WALKTHROUGH_VOICE=1."""
    return bool(_SPEAK_ASK.search(text or "")) or \
        os.environ.get("ASTA_WALKTHROUGH_VOICE", "").strip() == "1"


#: The one clip playing, so the next step interrupts it instead of talking over it.
_PLAYING: dict = {}


async def say_aloud(text: str) -> None:
    """Speak on the Mac's speakers, in the background. His local voice first
    (Voicebox), macOS `say` when that is down. Never blocks the chat reply.
    Its own seam, so tests never make a sound."""
    import asyncio
    import tempfile
    old = _PLAYING.pop("proc", None)
    if old and old.returncode is None:
        old.kill()
    words = " ".join((text or "").split())
    if not words:
        return
    argv: list[str]
    try:
        from . import voice
        wav = await voice.speak(words, voice="assistant")
        f = tempfile.NamedTemporaryFile(prefix="asta-walk-", suffix=".wav", delete=False)
        f.write(wav)
        f.close()
        argv = ["afplay", f.name]
    except Exception:                                          # noqa: BLE001
        argv = ["say", words[:1200]]
    try:
        _PLAYING["proc"] = await asyncio.create_subprocess_exec(
            *argv, stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL)
    except Exception:                                          # noqa: BLE001
        pass


def _speak(s: dict, text: str) -> None:
    if s.get("voice"):
        import asyncio
        asyncio.get_running_loop().create_task(say_aloud(text))


class WalkError(RuntimeError):
    pass


# --- state ---------------------------------------------------------------------

#: A session nobody has touched for this long is over. 30 Sep: a walkthrough of
#: task 126 was left open, and an hour later his feedback on a colleague's draft
#: was answered as a question about the helm file still on screen.
IDLE_SECONDS = int(os.environ.get("ASTA_WALKTHROUGH_IDLE_MINUTES", "30") or 30) * 60


def get(cid: str) -> dict | None:
    import time
    try:
        s = json.loads(store.kv_get(_KEY + cid) or "null")
    except ValueError:
        return None
    if not isinstance(s, dict):
        return None
    if time.time() - float(s.get("touched") or s.get("started") or 0) > IDLE_SECONDS:
        store.kv_set(_KEY + cid, "")
        store.record_outcome("walkthrough", "expired", subject=s.get("target", ""))
        return None
    return s


def _save(cid: str, s: dict | None) -> None:
    import time
    if s:
        s["touched"] = time.time()
    store.kv_set(_KEY + cid, json.dumps(s) if s else "")


def takes(cid: str, text: str, draft_waiting: bool, affirms: bool = False) -> bool:
    """Is this message for the walkthrough? Starting one always is. Otherwise
    only while a session is open — and, while a colleague's draft is waiting,
    only the session's own words that are not also a yes/no to the draft."""
    if wants_to_start(text) or _APPLY_LATER.search(text or ""):
        return True
    if not get(cid):
        return False
    if draft_waiting:
        return is_command(text) and not affirms
    return True


def is_command(text: str) -> bool:
    """One of the session's own words — the only thing a walkthrough may take
    while a colleague's draft is waiting for him."""
    t = (text or "").strip()
    return bool(_NEXT.match(t) or _BACK.match(t) or _AGAIN.match(t) or _DONE.match(t)
                or _VOICE_ON.match(t) or _VOICE_OFF.match(t) or _APPLY.match(t) or _LATER.match(t))


def notes_for(task_id: int) -> list[dict]:
    try:
        return json.loads(store.kv_get(f"review_notes:{task_id}") or "[]")
    except ValueError:
        return []


# --- where the change is ----------------------------------------------------------

async def _change(target: str) -> dict:
    """{title, diff, root, task_id, pr, workspace} for "task 126" or a PR link."""
    from . import review, tasks
    m = _TASK.search(target or "")
    link = _PR.search(target or "")
    pr = link.group(0) if link else ""
    task = None
    if m:
        task = store.get_task(int(m.group(1) or m.group(2)))
        if not task:
            raise WalkError(f"there is no task #{m.group(1) or m.group(2)}")
        if task.get("kind") != "code":
            raise WalkError(f"task #{task['id']} made no code change to walk through")
        found = _PR.search(f"{task.get('pr_urls') or ''} {task.get('result') or ''}")
        pr = pr or (found.group(0) if found else "")
    workspace = (task or {}).get("workspace") or ""
    root = ""
    diff = ""
    title = (task or {}).get("title") or ""
    if pr:
        got = await review.gather(pr, workspace or "booking")
        diff, title = got.get("diff") or "", title or got.get("title") or ""
        repo = (got.get("target") or "").split("/")[-1]
        for ws in _workspace_roots(workspace):
            hit = review.resolve_repo(ws, repo)
            if hit:
                root = str(ws / hit)
                break
    elif task:
        cwd = Path(tasks.task_cwd(task["id"], workspace))
        diff, root = await _local_diff(cwd), str(cwd)
    if not diff.strip():
        raise WalkError("there is no diff to walk through (no PR and no local change)")
    return {"title": title, "diff": diff, "root": root, "task_id": (task or {}).get("id"),
            "pr": pr, "workspace": workspace}


def _workspace_roots(preferred: str) -> list[Path]:
    from . import workspace_tools
    ws = workspace_tools.WORKSPACES
    order = ([preferred] if preferred in ws else []) + [k for k in ws if k != preferred]
    return [Path(str(ws[k])) for k in order]


async def _local_diff(cwd: Path) -> str:
    from . import repo_ops
    for args in (("git", "diff", "origin/develop...HEAD"), ("git", "diff", "HEAD~1"),
                 ("git", "diff")):
        rc, out = await repo_ops.git(cwd, *args)
        if rc == 0 and out.strip():
            return out
    return ""


# --- the steps ---------------------------------------------------------------------

_ORDER = """You are walking Arun, the senior engineer who owns this code, through a
change in IntelliJ, one step at a time. Order the steps the way a REQUEST travels
through the code — entry point first (controller / listener / workflow start),
then service, domain, repository or client, messaging (Kafka, Temporal), config,
and tests last. Not the diff's file order.

Change: {title}

Diff (unified):
{diff}

For each step give the file path exactly as in the diff (after b/), the NEW line
number where the change starts, a title of a few words, and an explanation of at
most 60 words: what changed, why, and how it hands on to the next step. Plain
words, no markdown. Group trivial edits (imports, formatting) into the step they
serve; skip pure noise. At most 12 steps.

Reply with ONLY this JSON:
{{"overview":"two sentences: the flow end to end","steps":[{{"file":"...","line":1,"title":"...","explain":"..."}}]}}"""


async def _ask(text: str) -> str:
    """The model. Its own seam, so tests never start a CLI."""
    from . import claude_cli
    return await claude_cli.one_shot(text, model=os.environ.get("ASTA_WALKTHROUGH_MODEL", "")
                                     or "sonnet", tools_off=True, timeout=240)


def _added_lines(diff: str) -> dict[str, list[int]]:
    """Every NEW line number the diff adds, per file — where IntelliJ should land."""
    out: dict[str, list[int]] = {}
    current, n = "", 0
    for line in diff.splitlines():
        if line.startswith("+++ b/"):
            current = line[6:].strip()
            out.setdefault(current, [])
        elif line.startswith("@@"):
            m = re.search(r"\+(\d+)", line)
            n = int(m.group(1)) if m else 1
        elif current and line.startswith("+") and not line.startswith("+++"):
            out[current].append(n)
            n += 1
        elif current and not line.startswith("-") and not line.startswith("\\"):
            n += 1
    return out


def _snap(line: int, added: list[int]) -> int:
    """The model's line, moved to the nearest line the change actually added.
    Live, 30 Sep: it said qa-values.yml:86 for a change on 85."""
    return min(added, key=lambda a: abs(a - line)) if added else line


def _files_and_lines(diff: str) -> dict[str, int]:
    """First changed NEW line per file, from the diff itself."""
    out: dict[str, int] = {}
    current = ""
    for line in diff.splitlines():
        if line.startswith("+++ b/"):
            current = line[6:].strip()
        elif line.startswith("@@") and current and current not in out:
            m = re.search(r"\+(\d+)", line)
            out[current] = int(m.group(1)) if m else 1
    return out


async def plan_steps(change: dict) -> dict:
    raw = await _ask(_ORDER.format(title=change["title"] or "(untitled)",
                                   diff=change["diff"][:DIFF_CHARS]))
    m = re.search(r"\{.*\}", raw or "", re.S)
    try:
        data = json.loads(m.group(0)) if m else {}
    except ValueError:
        data = {}
    known = _files_and_lines(change["diff"])
    added = _added_lines(change["diff"])
    steps = []
    for st in data.get("steps") or []:
        f = str(st.get("file") or "").strip().removeprefix("b/")
        if f not in known:
            f = next((k for k in known if k.endswith(f) or f.endswith(k)), "")
        if not f:
            continue
        try:
            line = int(st.get("line") or known[f])
        except (TypeError, ValueError):
            line = known[f]
        line = _snap(line, added.get(f) or [])
        steps.append({"file": f, "line": max(1, line), "title": str(st.get("title") or f)[:80],
                      "explain": " ".join(str(st.get("explain") or "").split())[:500]})
    if not steps:                           # the model failed: the diff's own order
        steps = [{"file": f, "line": ln, "title": Path(f).name, "explain": ""}
                 for f, ln in known.items()]
    return {"overview": " ".join(str(data.get("overview") or "").split())[:400], "steps": steps}


# --- IntelliJ ---------------------------------------------------------------------

async def open_in_idea(root: str, file: str, line: int) -> bool:
    """Put the step in front of him. Its own seam so tests never launch an IDE."""
    import asyncio
    path = Path(root) / file if root else Path(file)
    if not root or not path.exists() or not Path(IDEA).exists():
        return False
    proc = await asyncio.create_subprocess_exec(
        IDEA, str(root), "--line", str(line), str(path),
        stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL)
    try:
        await asyncio.wait_for(proc.wait(), 20)
    except asyncio.TimeoutError:
        pass                                # the launcher hands off and may linger
    return True


async def _show(cid: str, s: dict, speak_first: str = "") -> str:
    st = s["steps"][s["cursor"]]
    _speak(s, " ".join(x for x in [speak_first, f"Step {s['cursor'] + 1}. {st['title']}.",
                                   st["explain"]] if x))
    opened = await open_in_idea(s.get("root", ""), st["file"], st["line"])
    where = f"{Path(st['file']).name}:{st['line']}"
    head = f"*{s['cursor'] + 1}/{len(s['steps'])} · {st['title']}* — {where}"
    tail = [] if opened else ["(Couldn't open it in IntelliJ — the repo isn't cloned here.)"]
    mine = [n for n in s["notes"] if n["step"] == s["cursor"]]
    if mine:
        tail.append(f"📝 {len(mine)} note(s) on this step.")
    nav = "next · back · ask anything · tell me what to change · done"
    return "\n".join([head, st["explain"] or "(see the change at that line)", *tail, nav])


# --- the session ------------------------------------------------------------------

def wants_to_start(text: str) -> str:
    """The target ("task 126" / a PR link) when he asks for a walkthrough, else ''."""
    t = text or ""
    if not _START.search(t):
        return ""
    m = _TASK.search(t)
    if m:
        return f"task {m.group(1) or m.group(2)}"
    pr = _PR.search(t)
    return pr.group(0) if pr else ""


async def start(cid: str, target: str, voice: bool = False) -> str:
    try:
        change = await _change(target)
        planned = await plan_steps(change)
    except WalkError as exc:
        return f"Can't start the walkthrough — {exc}."
    except Exception as exc:                                   # noqa: BLE001
        return f"Can't start the walkthrough — {str(exc)[:200]}"
    import time
    s = {"target": target, "title": change["title"], "root": change["root"], "started": time.time(),
         "task_id": change["task_id"], "pr": change["pr"], "workspace": change["workspace"],
         "steps": planned["steps"], "cursor": 0, "notes": [], "state": "walking",
         "voice": bool(voice)}
    _save(cid, s)
    store.record_outcome("walkthrough", "started", subject=target, detail=change["title"][:160])
    intro = f"🧭 *{change['title'] or target}* — {len(s['steps'])} steps, in the order a request runs."
    if planned["overview"]:
        intro += f"\n{planned['overview']}"
    if s["voice"]:
        intro += "\n🔊 Speaking each step — say *mute* to stop."
    shown = await _show(cid, s, speak_first=planned["overview"])
    return intro + "\n\n" + shown


async def handle(cid: str, text: str) -> str | None:
    """His message during a session, or None when it is not for the session."""
    s = get(cid)
    if not s:
        return None
    t = (text or "").strip()
    if s.get("state") == "awaiting_apply":
        return await _decide(cid, s, t)
    if _VOICE_ON.match(t) or _VOICE_OFF.match(t):
        s["voice"] = bool(_VOICE_ON.match(t))
        _save(cid, s)
        if not s["voice"]:
            old = _PLAYING.pop("proc", None)
            if old and old.returncode is None:
                old.kill()
        return "🔊 Speaking each step." if s["voice"] else "🔇 Voice off — text only."
    if _DONE.match(t):
        return _finish(cid, s)
    if _BACK.match(t):
        s["cursor"] = max(0, s["cursor"] - 1)
        _save(cid, s)
        return await _show(cid, s)
    if _AGAIN.match(t):
        return await _show(cid, s)
    if _NEXT.match(t):
        if s["cursor"] + 1 >= len(s["steps"]):
            return _finish(cid, s)
        s["cursor"] += 1
        _save(cid, s)
        return await _show(cid, s)
    if _CORRECTION.search(t) and not (_QUESTION.search(t) and not re.search(
            r"\b(?:should|instead|rename|remove|change)\b", t, re.I)):
        st = s["steps"][s["cursor"]]
        s["notes"].append({"step": s["cursor"], "file": st["file"], "line": st["line"],
                           "note": t})
        _save(cid, s)
        return (f"📝 Noted for step {s['cursor'] + 1} ({Path(st['file']).name}:{st['line']}): "
                f"{t}\nNothing changes yet — I'll ask at the end. Say *next* to go on.")
    return await _answer(s, t)


async def _answer(s: dict, question: str) -> str:
    st = s["steps"][s["cursor"]]
    hunk = _hunk_for(s, st)
    raw = await _ask(
        f"Arun is reviewing step {s['cursor'] + 1} of a change in IntelliJ: {st['title']} "
        f"({st['file']}:{st['line']}).\nThe change at this step:\n{hunk}\n\n"
        f"His question: {question}\n\nAnswer in at most 80 words, plainly, about this code. "
        f"If you are not sure, say what you would check.")
    answer = (raw or "").strip()[:900]
    _speak(s, answer)
    return answer + "\n\n(next · back · done)"


def _hunk_for(s: dict, st: dict) -> str:
    """The diff lines for this step's file — kept in the session start only as
    the steps, so re-read from where it lives when asked."""
    try:
        path = Path(s.get("root") or "") / st["file"]
        lines = path.read_text(errors="replace").splitlines()
        lo = max(0, st["line"] - 15)
        return "\n".join(f"{i + 1}: {ln}" for i, ln in enumerate(lines[lo:lo + 45], start=lo))
    except OSError:
        return "(the file is not readable here)"


def _finish(cid: str, s: dict) -> str:
    notes = s["notes"]
    if not notes:
        _save(cid, None)
        store.record_outcome("walkthrough", "finished", subject=s["target"], detail="no notes")
        return "✅ Walkthrough done — no changes noted."
    s["state"] = "awaiting_apply"
    _save(cid, s)
    if s.get("task_id"):
        store.kv_set(f"review_notes:{s['task_id']}", json.dumps(notes))
    listed = "\n".join(f"{i}. {Path(n['file']).name}:{n['line']} — {n['note']}"
                       for i, n in enumerate(notes, 1))
    who = f"task #{s['task_id']}" if s.get("task_id") else "a new code task (plan first)"
    return (f"📝 {len(notes)} note(s) from the review:\n{listed}\n\n"
            f"Apply them now? Say *apply* and {who} makes these changes. "
            f"Say *later* to keep them.")


async def _decide(cid: str, s: dict, t: str) -> str:
    if _LATER.match(t):
        _save(cid, None)
        store.record_outcome("walkthrough", "kept", subject=s["target"],
                             detail=f"{len(s['notes'])} notes")
        return (f"Kept. Say “apply review notes for task {s['task_id']}” whenever you want."
                if s.get("task_id") else "Kept for this session only — no task to attach them to.")
    if not _APPLY.match(t):
        return "Say *apply* to make the noted changes, or *later* to keep them."
    return await apply_notes(cid, s)


async def apply_notes(cid: str, s: dict) -> str:
    from . import tasks
    notes = s.get("notes") or (notes_for(s["task_id"]) if s.get("task_id") else [])
    spec = "Review notes from Arun's walkthrough — apply each, nothing else:\n" + "\n".join(
        f"{i}. {n['file']}:{n['line']} — {n['note']}" for i, n in enumerate(notes, 1))
    _save(cid, None)
    store.record_outcome("walkthrough", "applied", subject=s["target"], detail=f"{len(notes)} notes")
    if s.get("task_id"):
        store.kv_set(f"review_notes:{s['task_id']}", "[]")
        return await tasks.refine(int(s["task_id"]), spec)
    t = tasks.spawn(f"Review notes on {s.get('pr') or s['target']}", spec + (
        f"\n\nThe change under review: {s.get('pr')}" if s.get("pr") else ""),
        kind="code", workspace=s.get("workspace") or None)
    tid = t.get("id") if isinstance(t, dict) else t
    return f"Started task #{tid} for the {len(notes)} notes — its plan comes to you first."


_APPLY_LATER = re.compile(r"\bapply\s+(?:the\s+)?review\s+notes\s+(?:for|on)\s+task\s*#?\s*(\d+)", re.I)


async def apply_later(cid: str, text: str) -> str | None:
    """"apply review notes for task 126" — the notes he kept for later."""
    m = _APPLY_LATER.search(text or "")
    if not m:
        return None
    tid = int(m.group(1))
    notes = notes_for(tid)
    if not notes:
        return f"There are no kept review notes for task #{tid}."
    return await apply_notes(cid, {"task_id": tid, "notes": notes, "target": f"task {tid}"})
