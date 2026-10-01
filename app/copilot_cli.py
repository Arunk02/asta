"""GitHub Copilot CLI as a chat provider — the office-paid day-to-day workhorse.

Asta acts as the orchestrator: routine chat turns can run entirely on `copilot -p`
(zero Anthropic/OpenAI tokens), with per-conversation session continuity via
`--session-id` / `--resume`. Claude stays for verification passes and as the smarter
brain when explicitly selected; when Claude runs out of tokens mid-conversation the
turn is automatically re-routed here.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import re
import shutil
import time
import uuid
from pathlib import Path
from typing import Awaitable, Callable

from . import memory, store, turn_budget, untrusted, workspace_tools

COPILOT_SESSIONS = Path.home() / ".copilot" / "session-state"


def mcp_cli_enabled() -> bool:
    """Whether CLI brains reach Asta's capabilities as native MCP tools rather
    than by curling the API. Off by default: the curl path is proven, and this is
    the opt-in cutover (ASTA_CLI_MCP=1), flipped only after a real-turn test."""
    return os.environ.get("ASTA_CLI_MCP", "1").lower() in ("1", "true", "yes", "on")

ROOT = Path(__file__).resolve().parent.parent


#: What a chat turn may not do here, in THIS CLI's syntax. The decision lives in
#: capabilities.chat_may_write(); only the spelling is local.
#:
#: Every entry was verified against the real `copilot` binary on 2026-08-26, and
#: two things came out of that which no amount of reading would have:
#:
#:   · `--deny-tool edit` — what this code passed until today — is a NO-OP. The
#:     tool is called `write`. Asked to create a file with `edit` denied, copilot
#:     created it. The chat write-block had never once worked.
#:   · denying `write` alone is not enough either: copilot simply falls back to
#:     the shell and writes the file that way. Measured, not assumed.
#:
#: `gh` is denied only for `pr create`. Chat legitimately reads CI with
#: `gh run list` and `gh pr view`, and banning the whole command would break the
#: thing he uses most.
_CHAT_DENY = ("write", "shell(git commit)", "shell(git push)", "shell(gh pr create)")


def turn_timeout() -> int:
    """How long ONE CLI turn may run before it is abandoned.

    The number and the reasoning live in `turn_budget.ceiling_seconds` — one
    ceiling for every brain, the local model included. Kept as a name here
    because callers and `claude_cli` already reach for it.
    """
    return turn_budget.ceiling_seconds()


TURN_TIMEOUT = 10 * 60          # legacy constant; live callers use turn_timeout()


def available() -> bool:
    return bool(shutil.which("copilot")) and (Path.home() / ".copilot").is_dir()


def _session_id(conv_id: str) -> tuple[str, bool]:
    """(session_id, is_new) — one Copilot session per Asta conversation."""
    key = f"copilot_session:{conv_id}"
    sid = store.kv_get(key)
    if sid:
        return sid, False
    sid = str(uuid.uuid4())
    store.kv_set(key, sid)
    return sid, True


#: Copilot keeps a per-session event log and appends to it on every tool call.
#: `~/.copilot/session-state/<session-id>/events.jsonl` — the layout the CLI has
#: used since it gained resumable sessions.
SESSION_STATE = Path.home() / ".copilot" / "session-state"


def _current_session(conv_id: str) -> str:
    """The session id already recorded for this conversation, without minting one.

    Deliberately NOT `_session_id`, which writes a new id when it finds none —
    calling that here would make `_build_cmd` believe the session already existed
    and resume one that was never created, losing the first-turn orientation
    block on every new conversation. Read after the command is built, when the
    id is there either way.
    """
    return store.kv_get(f"copilot_session:{conv_id}") or ""


def _session_progress(sid: str) -> Callable[[], object] | None:
    """Copilot's own event log as a "still working" signal — see turn_budget.

    Its stdout carries prose and nothing else, so a turn doing tool work is
    silent on the only channel the watchdog could see. The event log is not: it
    grows on every tool call, every result, every step. Size is enough — this
    asks "did anything happen", never what.

    Measured on the turn that prompted this rather than assumed: 52 events in the
    window it was killed in, the longest gap between any two being 24.1s against
    an idle limit of 120. The signal has a wide margin; it is not marginal.

    Returns None when there is no session id to watch, which leaves the turn on
    exactly the byte-stream-only rule it had before.
    """
    if not sid:
        return None
    path = SESSION_STATE / sid / "events.jsonl"

    def probe() -> object:
        try:
            return path.stat().st_size
        except OSError:
            # Not there yet on a brand-new session, and gone if the store is
            # cleaned mid-turn. Both mean "no signal", never "stopped".
            return None

    return probe


def _cwd(conv: dict) -> str:
    ws = conv.get("workspace")
    if ws and ws in workspace_tools.WORKSPACES:
        return str(workspace_tools.WORKSPACES[ws])
    return str(ROOT)


def _switch_recap(conv: dict, via: str) -> str:
    """A recap of the conversation so far — ONLY when switching brains mid-thread.

    A fresh CLI session is not a fresh conversation. Each brain keeps its own
    session (the formats can't be shared), so switching the model picker used to
    drop the new brain in blind — it answered with no idea what the other brain
    had just discussed. This bridges that.

    The trigger is precise: recap only when the OTHER brain has a live session for
    this conversation. A real "new chat" clears BOTH sessions (rotate_sessions),
    so this correctly stays silent then — there is nothing to continue, and the
    durable bits were already digested into memory and resurface via recall.
    """
    # The second trigger: this conversation's own session was just retired for
    # SIZE (main.retire_session_for_size). Both keys are gone then, so the
    # switch check below cannot fire — but the thread is still the same thread,
    # and a fresh session dropped in blind is exactly the failure this recaps.
    flag = f"session_recap:{conv['id']}"
    rotated = (store.kv_get(flag) or "") == "1"
    other = "claude_session" if "Copilot" in via else "copilot_session"
    if not rotated and not (store.kv_get(f"{other}:{conv['id']}") or "").strip():
        return ""
    if rotated:
        store.kv_del(flag)          # consumed: the new session now carries the recap
    msgs = store.list_ui_messages(conv["id"])
    if msgs and msgs[-1].get("role") == "user":
        msgs = msgs[:-1]                        # drop the current turn (already stored)
    if not msgs:
        return ""
    lines = []
    for m in msgs[-6:]:
        who = "Arun" if m.get("role") == "user" else "You"
        text = " ".join((m.get("content") or "").split())[:400]
        if text:
            lines.append(f"{who}: {text}")
    if not lines:
        return ""
    why = ("your previous session was retired to keep its context small — this is "
           "the same conversation continuing" if rotated
           else "you're continuing it after a model switch")
    return (f"Conversation so far ({why} — pick up where it left off, don't restart):\n"
            + "\n".join(lines))


def now_line(now: float | None = None) -> str:
    """What every brain is told about when it is. One definition, four callers.

    28 Sep: *"some of the tasks are not doing thinking it is sunday"* — on a
    Monday. Nothing was broken; a brain was asked to infer policy it had no way
    to evaluate. `guardrails.block()` hands every brain his standing rule frozen
    as prose — "Quiet on Saturdays and Sundays — one summary Monday 09:00" — and
    `one_shot`, the entry point for every background call (tasks, the responder,
    digests, triage, plans, reviews), said nothing at all about the date. So the
    brain guessed the weekend and politely held back.

    Two things, therefore, and the second matters more than the first: the local
    day, and whether the quiet rule is in force RIGHT NOW. A condition a brain
    has to work out from a sentence is a condition it will get wrong; this
    answers it. Shared by claude_cli too — a rule that holds on one CLI and not
    the other is two assistants.
    """
    import datetime as _dt
    from . import policy
    at = _dt.datetime.fromtimestamp(time.time() if now is None else now)
    line = at.strftime("%Y-%m-%d %H:%M %a")
    with contextlib.suppress(Exception):
        if policy.rules("quiet"):
            holding = policy.quiet_holding(at.timestamp())
            line += (f" · his quiet rule IS in force now ({holding.render()}) — "
                     "hold anything that is not urgent" if holding else
                     " · his quiet-hours rules are NOT in force right now, so work normally")
    return f"[now: {line}]"


#: He is asking to be TOLD something, rather than asking Asta to DO something.
#: Only these turns get passages put in front of the brain: injecting documents
#: into "set a reminder" or "thanks" would spend tokens on nothing.
_SEEKING = re.compile(
    r"^\s*(?:what|whats|what's|how|why|when|where|which|who|explain|describe|"
    r"define|summari[sz]e|overview|walk\s+me\s+through|"
    r"tell\s+(?:me\s+)?(?:abt|about|more))\b|\?\s*$", re.I)


#: He is asking for something to be written to someone.
_WRITING_TO = re.compile(r"\b(reply|respond|tell|ping|message|msg|send|write|ask|draft|inform|"
                         r"let\s+\w+\s+know)\b", re.I)


def _named_in(text: str) -> str:
    """The colleague a message names, from the people he actually chats with."""
    try:
        from . import chat_watch, store
        names = json.loads(store.kv_get(chat_watch._RAIL_KEY) or "[]")
        # And every thread history knows — the rail shows only the recent ones.
        names += [n for n in store.teams_chats_known() if n not in names]
    except Exception:                                          # noqa: BLE001
        return ""
    low = (text or "").lower()
    for n in names:
        first = (n or "").split()[0].lower() if (n or "").split() else ""
        if len(first) > 2 and re.search(rf"\b{re.escape(first)}\b", low):
            return n
    return ""


_TASK_REF = re.compile(r"\btask\s*#?\s*(\d{1,5})\b|(?<![\w/])#(\d{2,5})\b", re.I)
_LINK = re.compile(r"https://github\.com/[\w.-]+/[\w.-]+/(?:pull|actions/runs)/\d+")
_PR_NUM = re.compile(r"\bPR\s*#?(\d{2,6})\b", re.I)


def _tasks_named(text: str) -> str:
    """The tasks a message names, with what they found and the links in them."""
    from . import store
    ids = []
    for m in _TASK_REF.finditer(text or ""):
        n = int(m.group(1) or m.group(2))
        if n not in ids:
            ids.append(n)
    lines = []
    for n in ids[:3]:
        t = store.get_task(n)
        if not t:
            continue
        body = f"{t.get('result') or ''} {t.get('pr_urls') or ''} {t.get('prompt') or ''}"
        links = sorted(set(_LINK.findall(body)))[:4]
        prs = sorted(set(_PR_NUM.findall(t.get("result") or "")))[:3]
        head = " ".join((t.get("result") or t.get("error") or "").split())[:280]
        lines.append(f"Task #{n} ({t.get('status')}, workspace {t.get('workspace') or '-'}): "
                     f"{t.get('title', '')}"
                     + (f"\n  links: {', '.join(links)}" if links else "")
                     + (f"\n  PRs mentioned: {', '.join('#' + p for p in prs)}" if prs else "")
                     + (f"\n  it found: {head}" if head else ""))
    return ("Tasks he refers to (from the task table — use these, do not search for them):\n"
            + "\n".join(lines)) if lines else ""


def _teams_with(who: str, n: int = 12) -> str:
    """The last messages of his 1:1 with this person, both sides, oldest first."""
    import time as _t
    from . import chat_watch, store
    rows = store.teams_messages(chat=who, since=_t.time() - 3 * 86400, limit=4000)
    rows = [r for r in rows if (r.get("chat") or "").strip().lower() == who.strip().lower()]
    lines, seen = [], set()
    for r in rows:
        ident = chat_watch._identity(r)
        if ident in seen:
            continue
        seen.add(ident)
        sender = "Arun" if chat_watch.is_from_him(r.get("sender", "")) else (r.get("sender") or "?")
        when = _t.strftime("%d %b %H:%M", _t.localtime(float(r.get("sent_at") or r.get("seen_at") or 0)))
        text = " ".join(chat_watch.as_read(r.get("text") or "").split())[:400]
        lines.append(f"{when} {sender}: {text}")
    if not lines:
        return ""
    return (f"His Teams chat with {who} — the last messages, as they were written "
            f"(this is the conversation; continue from it):\n" + "\n".join(lines[-n:]))


def _his_prs(hours: float = 96) -> str:
    """His own PRs from recent tasks, with the state the PR watcher last saw."""
    import time as _t
    from . import store, tasks
    out = []
    for t in store.list_tasks(limit=60):
        if not t.get("pr_urls") or _t.time() - float(t.get("created_at") or 0) > hours * 3600:
            continue
        state = {"merged": "MERGED", "pr_closed": "CLOSED"}.get(t["status"], "OPEN")
        branch = store.kv_get(f"task_branch:{t['id']}") or ""
        from . import prname
        for url in tasks._pr_links(t):
            out.append(f"• {prname.from_url(url)} — {state} — task #{t['id']} {t['title'][:60]} — {url}"
                       + (f" — branch {branch}" if branch else ""))
    if not out:
        return ""
    return ("His own PRs from recent tasks, as the PR watcher last saw them (checked every "
            "5 min). Never call one merged or closed unless it says so here or `gh pr "
            "view` says so now:\n" + "\n".join(out[:10]))


def open_work() -> str:
    """Every PR of his still open, and every follow-up already scheduled."""
    import datetime as _dt
    from . import prname, reminders, store
    lines = []
    mine = prname.his_open_prs()
    if mine:
        lines.append("His open PRs (\"my PR\", \"this\", \"both\" mean these — never "
                     "a colleague's PR unless he names it):\n" + mine)
    due = [r for r in store.list_reminders() if r["due_at"] < __import__("time").time() + 14 * 86400]
    if due:
        lines.append("Already scheduled — update these with cancel_reminder / "
                     "send_later; never add a second one for the same thing:\n" + "\n".join(
                         f"• #{r['id']} {_dt.datetime.fromtimestamp(r['due_at']).strftime('%a %d %b %H:%M')}"
                         f" — {reminders.describe(r)}" for r in due[:10]))
    return "\n\n".join(lines)


def turn_context(user_text: str) -> str:
    """What a chat turn should know before it starts, besides the date.

    Two things, both learned on 29 Sep, and both built so they can only ever add:

      * WHO HE MEANS. Asta pushed "Navya R: need ur help" and a minute later
        asked who "her" was, because pushes never reach the thread the brain
        reads. `referents` carries the last few people across.
      * WHAT HIS DOCUMENTS SAY. "tell abt telikos inland journey" got "no
        grounded info here" while his own index answered it outright. A tool the
        brain may decide not to call is not grounding, so when he is asking to
        be told something and the passages are actually about it, they arrive
        first, each with the document it came from.

    Never raises. A broken index or an empty register leaves the turn exactly as
    it was without them.
    """
    parts: list[str] = []
    with contextlib.suppress(Exception):
        from . import referents, threads
        people = referents.block()
        if people:
            parts.append(people)
        # …and what is going on with the first two of them, so "what did she
        # want?" or "send her the fix" continues their conversation.
        for r in referents.recent()[:2]:
            known = threads.context_for(r["who"])
            if known:
                parts.append(known)
    # The conversation ITSELF, not a summary of it. 1 Oct: "go ahead as vinish
    # asked" got "I can't see the Teams thread — only a summary", then the
    # wrong PR (1252, yesterday's other task) and "1429 is merged", which it
    # was not. The last messages with the person, and his own PRs as they
    # stand, are facts to start from — not things to remember.
    with contextlib.suppress(Exception):
        from . import referents
        who = _named_in(user_text) or next((r["who"] for r in referents.recent()[:1]), "")
        convo = _teams_with(who) if who else ""
        if convo:
            parts.append(convo)
    with contextlib.suppress(Exception):
        prs = _his_prs()
        if prs:
            parts.append(prs)
    # His open work and what is already promised, every turn, whichever brain.
    # 1 Oct: "notify Vinish on Monday to get this merged" got "is that PR 1459,
    # Komal's?" — "my PR, why would I chase others' PRs, simple things" — and a
    # follow-up said to be scheduled for 14:32 had never been stored at all.
    with contextlib.suppress(Exception):
        work = open_work()
        if work:
            parts.append(work)
    # "merge task 166's PR": what task 166 was, and every link in it. Without
    # this, 29 Sep's turn spent twelve model calls finding PR #1251 and its repo.
    with contextlib.suppress(Exception):
        facts = _tasks_named(user_text)
        if facts:
            parts.append(facts)
    # A screenshot he sent (WhatsApp) or one he is asking about.
    with contextlib.suppress(Exception):
        from . import chat_watch
        shots = chat_watch.image_paths(user_text)
        if shots:
            parts.append("He sent screenshot(s) — open each with the Read tool before you "
                         "answer; it is the subject of his message:\n"
                         + "\n".join(f"- {p}" for p in shots))
    # Writing to someone in his name: how he actually writes to them.
    if _WRITING_TO.search(user_text or ""):
        with contextlib.suppress(Exception):
            from . import referents, style
            who = _named_in(user_text) or next((r["who"] for r in referents.recent()[:1]), "")
            voice = style.rider(who) if who else ""
            if voice:
                parts.append(voice)
    if _SEEKING.search(user_text or ""):
        with contextlib.suppress(Exception):
            from . import knowledge
            hits = knowledge.relevant(user_text, limit=3, at_least=0.6)
            if hits:
                lines = [f"[{h['document']} — {h['where']}]\n{h['text'][:600]}" for h in hits]
                parts.append(
                    "From his indexed documents. Answer from these and name the "
                    "document when you do; if they do not cover the question, say "
                    "so rather than guess:\n\n" + "\n\n".join(lines))
    return "\n\n".join(parts)


def _first_turn_context(conv: dict, via: str = "Copilot CLI", user_text: str = "") -> str:
    """Orientation block for a fresh CLI session (it has no Asta memory).

    The capability list is GENERATED from the same registry that gives the chat
    agent its tools, so a new tool is taught here the moment it is added — there
    is no second description to keep in sync, and no stale copilot_session:* rows
    to clear after editing prose.

    Shared with claude_cli: same assistant, same tools, same rules — only the
    CLI underneath differs, so `via` is the one thing that changes.

    CLI brains never see agent.PERSONA, so the safety policy rides here.

    The full capability spec is ranked against this first message and narrowed to
    what the turn likely needs — the same per-turn selection the in-process agent
    already gets, which the CLI paths were throwing away and so paid for all ~34
    every session. Safe because cli_block still lists every un-expanded tool by
    name: a mis-rank costs one round-trip to ask, never a lost capability. This
    runs once per CLI session (the CLI remembers the rest), so the index tail is
    what keeps a later message in the same session reachable.
    """
    # Which expert is answering. One shared function, so the chat brain, the task
    # pipeline and the responder's investigations all reach the same answer about
    # the same sentence — see app/roles.py for why that is not a nicety.
    from . import roles
    hat = roles.brief(roles.role_for(user_text))

    from . import capabilities, consent, guardrails, skills, tool_index
    name = os.environ.get("ASSISTANT_NAME", "Asta")
    parts = [
        f"You are acting as {name}, Arun's assistant, via {via}. Be concise and direct.",
    ]
    if hat:
        parts.append(hat)
    # His standing instructions — the same block the in-process brain gets from
    # agent.build_instructions, so a rule holds whichever brain took the turn.
    rules = guardrails.block("chat")
    if rules:
        parts.append(rules)
    idx = memory.index_text().strip()
    if idx:
        parts.append("Arun's memory index (for orientation):\n" + idx[:1500])
    # The skill catalogue is progressive disclosure: only these one-liners ride in
    # the prompt; the CLI pulls a skill's full body with GET /api/skills/{name}
    # (load_skill) when its one-liner matches — parity with the in-process brain.
    sk = skills.index_block()
    if sk:
        parts.append(sk)
    port = os.environ.get("ASTA_PORT", "8321")
    if mcp_cli_enabled():
        # Tools, descriptions and rules all arrive over MCP, so the ~2k-token
        # curl catalogue is dead weight — this is the orientation's biggest line.
        parts.append(
            "Arun's capabilities are native MCP tools on the `asta` server "
            "(remember, set_reminder, teams_activity, jira_issue, delegate_task, "
            "make_file, ask_user, review_pr, …). Call them directly — do NOT curl "
            "the HTTP API or shell out for them. Each tool's own description "
            "carries its rules.")
    else:
        selected = tool_index.select(user_text) if user_text else None
        parts.append(capabilities.cli_block(port, str(ROOT), selected))
    # A file ask is the one where shelling out looks plausible: the module that
    # writes files is right there in the repo, so a brain reads app/files.py and
    # tries to run it. Live, twice on 16 Sep, that ended in a five-minute repo
    # hunt, a delegated code task, and no file — while `make_file` sat in its own
    # tool list. So when he is plainly asking to be handed a file, say which tool
    # that is. Outside the MCP branch on purpose: every brain gets the same
    # sentence, because a rule that holds on one CLI and not the other is two
    # assistants. Same definition the substitution guard uses, so the prompt and
    # the refusal cannot drift apart.
    if consent.asked_for_a_file(user_text or ""):
        parts.append(
            "THIS MESSAGE ASKS FOR A FILE. `make_file` is how that happens: you "
            "decide the rows (header first) and the title, it writes the "
            "xlsx/csv/md/docx/pptx/pdf, checks what it wrote and sends it to his "
            "phone. Call it directly. Do NOT write a script, do NOT run "
            "app/files.py yourself, and do NOT delegate a task for it — a file he "
            "asked for is one tool call, not a piece of engineering.")
    recap = _switch_recap(conv, via)
    if recap:
        parts.append(recap)
    cid = conv.get("id", "")
    parts.append(
        f'AUTONOMOUS LOOP — this conversation\'s id is "{cid}". You do not have to stop '
        "and wait for Arun between steps. When the task isn't finished and you already "
        "know the next step, make your LAST action a call to POST /api/loop/continue "
        f'{{"conv_id":"{cid}","next_step":"<one line>"}} — Asta runs it immediately, with no '
        "message from Arun, and keeps looping until the work is done. Anything you would "
        "send OUTSIDE this chat (a Teams reply, email, Jira comment, PR body, a message to a "
        "person) must NEVER be sent directly: POST /api/loop/prepare-send "
        f'{{"conv_id":"{cid}","what":"<draft>","to":"<who>","channel":"teams|email|jira|pr|chat"}} '
        "and Asta shows Arun the draft and asks before it goes out. Stop the loop only when "
        "the task is done or you genuinely need his decision.")
    parts.append(
        "CODE WORK — the flow Arun expects, with a message to him at EVERY step:\n"
        "0. DELEGATE, do not interrogate. Never ask him which repo, which service a short "
        "name means ('AP' = the activity-plan workflow service), or for a Jira key before "
        "spawning: the workspace's own context resolves repos, a ticket is NOT a "
        "precondition, and the task asks — once, before planning — only what it cannot "
        "find itself. Put his words in the brief verbatim. If he says it is a different "
        "ticket, do not carry another ticket's key into the brief: the branch is named "
        "from whatever key the brief contains.\n"
        "NAME A PR WITH ITS SERVICE: 'booking PR 1459', 'AP PR 1252', 'email PR 34' — never "
        "a bare 'PR 1459'; the same number exists in more than one repo. When he writes "
        "'booking PR 1459' that names the repo too.\n"
        "FACTS, NOT MEMORY: what a colleague said is in the Teams chat block of this "
        "turn — quote only from there, never write a quote you cannot see. A PR's state "
        "(open/merged/closed) comes from the PR list in this turn or `gh pr view`, never "
        "from memory. If a fact is not in front of you, check it before you say it.\n"
        "A lookup for yourself is an ANALYSIS task (or just read it yourself) — a "
        "teams_draft is only ever a message TO that person, in Arun's voice.\n"
        "Once a task is spawned or your answer is relayed to it, END the turn: the task "
        "reports to him itself at every gate and when it finishes. Never continue_working "
        "just to poll a task, and never send 'still running'. If he says the work belongs "
        "on an existing PR or branch, say so in the brief — the task finds and switches "
        "to it; that is never a question to bring back to him.\n"
        "1. Spawn a code task (kind 'code', workspace set). Routing is automatic: Jira-key "
        "tickets run the full staged pipeline (plan gate → Arun approves → implement); "
        "small ad-hoc asks run the micro pipeline (no gate, ~25 turns, escalates itself if "
        "bigger). Never plan the code change yourself in this chat. If an analysis task "
        'already investigated the topic, add "context_from": <that task id> so the worker '
        "reuses its evidence instead of re-discovering (big token saver).\n"
        "2. Relay Arun's answer: 'approve task N' → approve_task. Any other feedback → "
        'POST /api/tasks/N/reply with {"text":"…"} — the pipeline re-plans with it.\n'
        "3. After implementation the task finishes with the diff summary — the pipeline "
        "NEVER pushes or opens a PR. Show Arun the diff; only when he says ship, call "
        "ship_task.\n"
        "4. CI is watched for you and he is told the moment it turns red or green, with "
        "the failing test. When he asks about a red run, read the FAILED run's log before "
        "calling it noise — name the test and the assertion, and check EVERY run on the "
        "PR (a push-triggered and a pull_request-triggered build are two runs). 'rerun "
        "it', 'add that to the PR description', 'comment on the PR' → update_task_pr, done "
        "at once: it is his own PR and he asked, so it is never staged for a second yes, "
        "and a chat turn cannot run gh itself.\n"
        "5. On green, ASK whether to post the PR for review — and post it only to the person "
        "or group he names, never on your own initiative.\n"
        "COMMIT RULE (strict): plain `git commit -m \"msg\"`. Never add a Co-Authored-By "
        "trailer, an AI/assistant name, or a 'Generated with …' line — his commits and PRs "
        "must read as his own work. `gh` is already authenticated for push/PR."
    )
    if not (os.environ.get("JIRA_BASE_URL") and os.environ.get("JIRA_API_TOKEN")):
        parts.append("Jira is NOT configured — the jira_* endpoints above will fail; say so "
                     "rather than guessing ticket contents.")
    if os.environ.get("TEAMS_BRIDGE", "1").lower() not in ("1", "true", "yes"):
        parts.append("The Teams/Outlook bridge is OFF — those shell capabilities are "
                     "unavailable this session.")
    if conv.get("workspace"):
        parts.append(
            f"Active workspace: {conv['workspace']} at {_cwd(conv)} — project context lives in "
            ".asta-context/ (resolve-task.js maps questions to exact files)."
        )
    return untrusted.POLICY + "\n\n" + "\n\n".join(parts)


_ACTIVITY_ASK = re.compile(
    r"\b(any\s+(new\s+)?(teams\s+)?(messages?|mentions?|pings?)|anything\s+(new\s+)?for\s+me"
    r"|mentioned\s+me|what\s+did\s+i\s+miss|any\s?thing\s+i\s+missed|teams\s+activity"
    r"|missed\s+call)\b", re.I)


async def _teams_activity_context(user_text: str) -> str:
    """Pre-fetch the Teams activity feed in Python when the question is clearly
    about mentions/messages.

    Why not let the brain shell out: Copilot's Bash step depends on an external
    safety classifier, and when that is degraded EVERY command is refused — so
    'any messages for me?' would fail. Reading here is deterministic, works
    regardless, and saves the model a 20s round trip.
    """
    if not _ACTIVITY_ASK.search(user_text):
        return ""
    from . import teams_bridge
    if not (teams_bridge.enabled() and teams_bridge.logged_in_once()):
        return ""
    try:
        items = await teams_bridge.read_activity(20)
    except Exception:
        return ""
    if not items:
        return ""
    return ("Arun's live Teams activity feed, just read for you — use it to answer; "
            "no need to run any command.\n"
            + untrusted.wrap_lines(items, "Teams activity feed"))


_MAIL_ASK = re.compile(r"\b(mails?|e-?mails?|inbox|outlook)\b", re.I)
_MEETING_ASK = re.compile(r"\b(meetings?|calendar|schedule|calls?\s+today|free\s+at|busy)\b", re.I)


async def _outlook_context(user_text: str) -> str:
    """Same trick as _teams_activity_context, for inbox and calendar questions."""
    wants_mail = bool(_MAIL_ASK.search(user_text))
    wants_cal = bool(_MEETING_ASK.search(user_text))
    if not (wants_mail or wants_cal):
        return ""
    from . import outlook, teams_bridge
    if not (teams_bridge.enabled() and teams_bridge.logged_in_once()):
        return ""
    blocks = []
    if wants_mail:
        try:
            mails = await outlook.read_mail(20)
            if mails:
                att = outlook.needs_attention(mails)
                blocks.append("Inbox (newest first; 🔵 = unread):\n"
                              + "\n".join("• " + outlook.fmt_mail(m) for m in mails))
                blocks.append("Of those, needing a human reply: "
                              + ("; ".join(outlook.fmt_mail(m) for m in att) if att else "none"))
        except Exception:
            pass
    if wants_cal:
        try:
            mtgs = await outlook.todays_meetings()
            blocks.append("Today's meetings:\n" + ("\n".join("• " + m for m in mtgs) if mtgs else "none"))
        except Exception:
            pass
    if not blocks:
        return ""
    return ("Arun's live Outlook data, just read for you — answer from it, no command needed.\n"
            + untrusted.wrap("\n\n".join(blocks), "Outlook"))


def _build_cmd(conv: dict, user_text: str, extra_context: str = "") -> list[str]:
    sid, is_new = _session_id(conv["id"])
    # A handoff arrives AS the turn's text, and its wrapper ("was working on
    # it when it ran out of quota") ranks as conversation — so a resumed turn
    # was handed tools chosen for the handoff instead of for his request. Rank
    # on his sentence; the wrapper is provenance, not the subject.
    from . import resume as resume_mod
    ranking_text = resume_mod.ranking_text(user_text)
    # Every turn carries the current local time — long-lived sessions otherwise
    # drift days behind, which breaks "remind me at 3pm" style requests.
    user_text = f"{now_line()}\n{user_text}"
    # Who he means and what his documents say — judged on HIS sentence, never on
    # a handoff's wrapper (see resume.ranking_text).
    ctx = turn_context(ranking_text)
    if ctx:
        user_text = f"{ctx}\n\n{user_text}"
    if extra_context:
        user_text = f"{extra_context}\n\n{user_text}"
    prompt = user_text
    if is_new:
        prompt = _first_turn_context(conv, user_text=ranking_text) + "\n\n---\n\n" + user_text
    else:
        # The orientation block only rides on turn 1; recall keeps long-lived
        # sessions anchored to memory on every later turn (Copilot is flat-rate).
        rb = memory.recall_block(user_text)
        if rb:
            prompt = f"[{rb}]\n\n{user_text}"
    cmd = [
        "copilot",
        "--session-id" if is_new else "--resume", sid,
        "-p", prompt,
        "-s", "--no-color", "--no-ask-user", "--stream", "on",
        "--allow-all-tools", "--log-level", "none",
        # Path permission is SEPARATE from tool permission. Without this, any
        # binary outside cwd is refused ("Permission denied and could not
        # request permission from user") whenever a workspace is selected —
        # which silently broke every Teams/Outlook command. --add-dir is not
        # enough: it grants file access, not execution.
        "--allow-all-paths",
    ]
    # Defence in depth behind the routing in `_dispatch`: the CHAT path may not
    # edit files.
    #
    # The instruction has said "never plan or implement in chat yourself" for a
    # long time and the model implemented anyway, burning the 300s turn budget
    # mid-edit. Routing catches the clear cases; this catches the rest, by making
    # the wrong thing impossible rather than discouraged.
    #
    # Reading stays: `view`, `grep` and the shell generally are most of what chat
    # legitimately does, and taking the shell away would break every Teams, git
    # and log command — including the CI checks he asks for constantly. What goes
    # is writing and the three outward acts that cannot be taken back.
    #
    # Task runs (`one_shot`) are untouched: implementing is their whole job.
    from . import capabilities
    if not capabilities.chat_may_write():
        for tool in _CHAT_DENY:
            cmd += ["--deny-tool", tool]
    # Native asta tools instead of curl, when enabled. Copilot takes the config
    # as inline JSON (its flag differs from Claude's --mcp-config). --allow-all-
    # tools above already clears the MCP tools. Kept in lockstep with the shared
    # orientation swap, so the flag never tells copilot to use tools it lacks.
    if mcp_cli_enabled():
        import json as _json
        from . import mcp_server, tool_index
        # Same context-aware narrowing as the Claude path (one selector, both
        # brains): the ~handful the message needs, or the full set when ambiguous.
        selected = tool_index.select_sticky(conv["id"], ranking_text)
        cmd += ["--additional-mcp-config",
                _json.dumps(mcp_server.config_entry(tools=selected, conv_id=conv["id"]))]
    model = os.environ.get("COPILOT_CLI_MODEL")
    if model:
        cmd += ["--model", model]
    cmd += _budget_flags(os.environ.get("COPILOT_EFFORT", "medium"),
                         os.environ.get("COPILOT_MAX_CREDITS", ""))
    return cmd


def _budget_flags(effort: str, credits: str) -> list[str]:
    """Reasoning effort and a hard credit ceiling.

    Copilot was running at the provider default (`reasoningEffort: null`), which
    on Sonnet-5 means it thinks hard about everything — a two-word status
    question cost the same per turn as a refactor. Effort is the single biggest
    dial on spend; credits are the seatbelt, so a confused run can't quietly
    burn a chunk of the monthly quota before anyone notices.
    """
    flags: list[str] = []
    if effort and effort != "default":
        flags += ["--effort", effort]
    if credits:
        flags += ["--max-ai-credits", credits]
    return flags


# Reading Teams/Outlook drives a real browser, so it can wedge on a dead session
# or a stuck page. It used to be awaited with no ceiling at all, and — because it
# happens BEFORE the brain is even spawned — a wedge there looked exactly like a
# thinking model: no output, no error, no end. Context is a nice-to-have; the
# answer is not. Past the ceiling we go without it.
PREFETCH_TIMEOUT = float(os.environ.get("ASTA_PREFETCH_TIMEOUT", "25"))


async def _prefetch(user_text: str) -> str:
    """Teams + Outlook context for this message, or "" if it can't be had in time."""
    async def _both() -> str:
        return "\n\n".join(b for b in (await _teams_activity_context(user_text),
                                       await _outlook_context(user_text)) if b)
    try:
        return await asyncio.wait_for(_both(), timeout=PREFETCH_TIMEOUT)
    except (asyncio.TimeoutError, TimeoutError, Exception):
        return ""


async def run_turn(conv: dict, user_text: str,
                   on_delta: Callable[[str], Awaitable[None]] | None = None,
                   _retried: bool = False) -> str:
    """One chat turn through Copilot CLI, streaming stdout chunks to on_delta."""
    if not available():
        raise RuntimeError("Copilot CLI is not installed/authenticated (run: copilot login)")
    # Under MCP the brain reads Teams/Outlook via native tools on demand, so the
    # ~25s pre-read that blocks every turn is redundant. Kept for the MCP-off
    # fallback, where the brain can't reliably reach those itself.
    prefetched = "" if mcp_cli_enabled() else await _prefetch(user_text)
    proc = await asyncio.create_subprocess_exec(
        *_build_cmd(conv, user_text, prefetched),
        stdin=asyncio.subprocess.DEVNULL,           # see one_shot: never inherit
        cwd=_cwd(conv),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env={**os.environ, "CI": "1"},
    )
    limit = turn_timeout()
    assert proc.stdout
    # Read AFTER _build_cmd, which is what creates the id on a new conversation.
    beat = turn_budget.Heartbeat(_session_progress(_current_session(conv["id"])))
    chunks: list[str] = []

    async def _pump() -> None:
        assert proc.stdout
        while True:
            block = await proc.stdout.read(512)
            if not block:
                return
            beat.beat()
            text = block.decode(errors="replace")
            chunks.append(text)
            if on_delta:
                await on_delta(text)

    stop = await turn_budget.guard(_pump(), beat, total=limit)
    # `guard` reports timing only; the words belong to the caller either way.
    stop.chunks[:] = chunks
    try:
        if stop.answered():
            # It said its piece and then went quiet waiting on something outside
            # itself. That is an answer, not a failure: hand it back the way a
            # clean finish would. The process is still killed — nothing further
            # is coming, and leaving it alive holds a subprocess for nothing.
            proc.kill()
            store.record_outcome(
                "turn", "answered_then_idle", subject="copilot",
                detail=f"{stop.elapsed:.0f}s, silent {stop.silent_for:.0f}s, "
                       f"{len(stop.partial)} chars")
            return stop.partial
        if not stop.ok:
            # Everything it streamed is kept and travels with the error. The old
            # branch discarded it for a one-line message, which is why "did it do
            # anything?" had no answer but to go and look at the repo.
            proc.kill()
            # stderr is read AFTER the kill, not before: reading to EOF on a
            # live process just blocks until it exits, so the "safer" order
            # bought two seconds of waiting and an empty string every time.
            # Killing closes the write end, so the buffered complaint arrives.
            note = await turn_budget.tail_stderr(proc)
            store.record_outcome("turn", f"stopped_{stop.reason}", subject="copilot",
                                 detail=stop.detail(note))
            raise turn_budget.TurnStopped(stop, already_shown=on_delta is not None)
        # Closing stdout is not the same as exiting. A copilot that streamed its
        # answer and then hung on shutdown held this await forever, outside the
        # ceiling above — the turn was finished and Arun still heard nothing.
        rc = await asyncio.wait_for(proc.wait(), timeout=30)
    except asyncio.TimeoutError:
        proc.kill()
        raise turn_budget.TurnStopped(
            turn_budget.Stop("ceiling", limit, 0.0, chunks))
    except asyncio.CancelledError:
        # Arun corrected course mid-answer. Killing the process is the point:
        # it stops the wrong line of investigation from billing any further.
        proc.kill()
        with contextlib.suppress(Exception):
            await proc.wait()
        raise
    out = "".join(chunks).strip()
    if rc != 0:
        err = (await proc.stderr.read()).decode(errors="replace")[-500:] if proc.stderr else ""
        # A dead --resume session (e.g. cleaned store) gets one fresh retry.
        #
        # "One" has to be enforced by a flag, not by the session key: _session_id
        # WRITES a new key whenever it finds none, so deleting it here guaranteed
        # the very same condition was true again on the next failure. Anything
        # that fails for a reason a new session cannot fix — an exhausted monthly
        # quota, most of all — retried forever, roughly every 20 seconds, each
        # attempt leaving a fresh session directory behind. That is what left
        # 7,851 of them on disk, and why a WhatsApp message could be answered
        # with silence for three hours: the turn never returned to report it.
        if not _retried and not out and store.kv_get(f"copilot_session:{conv['id']}"):
            store.kv_del(f"copilot_session:{conv['id']}")
            return await run_turn(conv, user_text, on_delta, _retried=True)
        raise RuntimeError(f"Copilot CLI exited {rc}: {err or out[-300:] or 'no output'}")
    return out or "(Copilot returned no output)"


def last_turn_usage(conv: dict, reply_chars: int = 0):
    """Real input tokens for the turn just finished, from Copilot's own log.

    Copilot exposes no per-message usage — the fields token_audit once parsed are
    gone from the current CLI build. But every `copilot -p` process writes a
    `session.shutdown` with `currentTokens`: the full context it carried, which
    IS the input for that turn (a chat model re-sends its whole context each
    turn). Measured recently at ~24.6k per turn, of which ~14.5k is tool
    definitions — the bloat, now visible instead of guessed.

    Output tokens Copilot does not report, so those stay a char-count estimate;
    input is the dominant cost and the honest thing to measure. Read post-turn:
    run_turn has already awaited the process, so the snapshot is on disk.

    The shared CLI path picks this up by attribute, so any executor that grows a
    last_turn_usage gets real numbers with no change there — same contract as
    on_tool / on_usage.
    """
    from . import llm_meter
    sid = store.kv_get(f"copilot_session:{conv['id']}")
    if not sid:
        return llm_meter.Usage()
    path = COPILOT_SESSIONS / sid / "events.jsonl"
    current = 0
    try:
        for line in path.read_text().splitlines():
            try:
                e = json.loads(line)
            except ValueError:
                continue
            if e.get("type") == "session.shutdown":
                current = (e.get("data") or {}).get("currentTokens") or current
    except OSError:
        return llm_meter.Usage()
    if not current:
        return llm_meter.Usage()      # no snapshot yet → caller falls back to estimate
    # currentTokens IS the context this session carries into its next call — the
    # number the size-based retirement in main._run_turn_cli watches.
    return llm_meter.Usage(input=int(current), context=int(current),
                           output=reply_chars // llm_meter.CHARS_PER_TOKEN,
                           measured=True)


#: What a PLANNING leg may not do. Same spelling lesson as _CHAT_DENY above:
#: `write` is the tool's real name and denying it alone is not enough, because
#: copilot falls back to the shell.
_PLAN_DENY = ("write", "shell(git commit)", "shell(git push)", "shell(gh pr create)")


async def one_shot(prompt: str, cwd: str | None = None, timeout: int = 600,
                   agent: str = "", effort: str = "",
                   session_id: str = "", resume: bool = False,
                   on_progress=None, mcp_config: str = "", plan_only: bool = False,
                   model: str = "") -> str:
    """Headless one-off prompt.

    agent      — a workspace .github/agents/*.agent.md pipeline (e.g.
                 the staged pipeline); discovered from cwd, so cwd must be the
                 workspace root.
    session_id — pin the Copilot session so a run that pauses at a human gate
                 (solo agent Stage 1) can be resumed later with resume=True.
    effort     — per-call reasoning effort; falls back to COPILOT_EFFORT_TASK.
    mcp_config — inline mcpServers JSON to attach for this run (the dev MCP
                 servers for a code task). Empty leaves the command unchanged.
    """
    if not available():
        raise RuntimeError("Copilot CLI is not installed/authenticated")
    # A background brain is the one nobody is watching, and it was the only one
    # not told the date. See now_line.
    prompt = f"{now_line()}\n{prompt}"
    cmd = ["copilot"]
    if session_id:
        cmd += ["--resume" if resume else "--session-id", session_id]
    cmd += ["-p", prompt, "-s", "--no-color", "--no-ask-user",
            "--allow-all-tools", "--allow-all-paths", "--log-level", "none"]
    if mcp_config:
        # --additional-mcp-config ADDS to Copilot's own config (parity with the
        # chat path's flag); --allow-all-tools already clears the new tools.
        cmd += ["--additional-mcp-config", mcp_config]
    if agent:
        cmd += ["--agent", agent]
    if model:
        cmd += ["--model", model]
    if plan_only:
        # The plan gate, made structural. Told to plan and stop, a brain
        # sometimes implements anyway — and for an ad-hoc ("micro") task nothing
        # stopped it, which broke his one unconditional rule. With writing
        # denied it cannot, whatever it decides to do.
        for tool in _PLAN_DENY:
            cmd += ["--deny-tool", tool]
    # Headless workers are where the money goes — one ran 22 minutes
    # unchallenged. Their own effort/credit ceiling, separate from chat.
    cmd += _budget_flags(effort or os.environ.get("COPILOT_EFFORT_TASK", "medium"),
                         os.environ.get("COPILOT_MAX_CREDITS_TASK", ""))
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        # Never inherit stdin. The CLI appends piped stdin to the prompt, so a
        # parent with anything on it — a heredoc, a pipe — becomes part of what
        # the brain is told. Found 17 Sep: a test's own script reached the
        # standup brain as "the Python snippet at the end of your message".
        stdin=asyncio.subprocess.DEVNULL,
        cwd=cwd or str(ROOT),
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
        env={**os.environ, "CI": "1"},
    )
    async def _drain() -> str:
        # Incremental read (not communicate()) so on_progress sees the run unfold
        # and can push stage milestones while it's still working.
        parts: list[str] = []
        assert proc.stdout
        while True:
            chunk = await proc.stdout.read(512)
            if not chunk:
                break
            text = chunk.decode(errors="replace")
            parts.append(text)
            if on_progress:
                with contextlib.suppress(Exception):
                    await on_progress(text)
        await proc.wait()
        return "".join(parts)

    try:
        out = await asyncio.wait_for(_drain(), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        raise RuntimeError("Copilot one-shot timed out")
    except asyncio.CancelledError:
        # Rejecting a task must actually stop the spend. Without this the
        # awaiting coroutine goes away but copilot keeps running to completion,
        # billing every turn and still writing to the repo.
        proc.kill()
        with contextlib.suppress(Exception):
            await proc.wait()
        raise
    if proc.returncode != 0:
        raise RuntimeError(f"Copilot exited {proc.returncode}: {out[-300:]}")
    return out.strip()
