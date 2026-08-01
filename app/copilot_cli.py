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
import uuid
from pathlib import Path
from typing import Awaitable, Callable

from . import memory, store, untrusted, workspace_tools

COPILOT_SESSIONS = Path.home() / ".copilot" / "session-state"


def mcp_cli_enabled() -> bool:
    """Whether CLI brains reach Asta's capabilities as native MCP tools rather
    than by curling the API. Off by default: the curl path is proven, and this is
    the opt-in cutover (ASTA_CLI_MCP=1), flipped only after a real-turn test."""
    return os.environ.get("ASTA_CLI_MCP", "0").lower() in ("1", "true", "yes", "on")

ROOT = Path(__file__).resolve().parent.parent


def turn_timeout() -> int:
    """How long ONE CLI turn may run before it is abandoned.

    Was a hard 10 minutes, which is most of why a reply could take twenty: a wedged
    brain held the conversation for the full window before anyone found out. Five
    minutes is longer than any turn that was ever going to succeed, so the only
    thing the shorter ceiling costs is the waiting.
    """
    try:
        return max(30, int(os.environ.get("ASTA_TURN_TIMEOUT", "300")))
    except ValueError:
        return 300


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
    other = "claude_session" if "Copilot" in via else "copilot_session"
    if not (store.kv_get(f"{other}:{conv['id']}") or "").strip():
        return ""
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
    return ("Conversation so far (you're continuing it after a model switch — pick "
            "up where it left off, don't restart):\n" + "\n".join(lines))


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
    from . import capabilities, skills, tool_index
    name = os.environ.get("ASSISTANT_NAME", "Asta")
    parts = [
        f"You are acting as {name}, Arun's assistant, via {via}. Be concise and direct.",
    ]
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
            "ask_user, review_pr, …). Call them directly — do NOT curl the HTTP API "
            "or shell out for them. Each tool's own description carries its rules.")
    else:
        selected = tool_index.select(user_text) if user_text else None
        parts.append(capabilities.cli_block(port, str(ROOT), selected))
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
        "4. Watch CI and tell him pass or fail.\n"
        "5. On green, ASK whether to post the PR for review — and post it only to the person "
        "or group he names, never on your own initiative.\n"
        "COMMIT RULE (strict): plain `git commit -m \"msg\"`. Never add a Co-Authored-By "
        "trailer, an AI/assistant name, or a 'Generated with …' line — his commits and PRs "
        "must read as his own work. `gh` is already authenticated for push/PR."
    )
    if not (os.environ.get("JIRA_BASE_URL") and os.environ.get("JIRA_API_TOKEN")):
        parts.append("Jira is NOT configured — the jira_* endpoints above will fail; say so "
                     "rather than guessing ticket contents.")
    if os.environ.get("TEAMS_BRIDGE", "").lower() not in ("1", "true", "yes"):
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
    ranking_text = user_text            # the bare message, before prefixes muddy it
    # Every turn carries the current local time — long-lived sessions otherwise
    # drift days behind, which breaks "remind me at 3pm" style requests.
    import datetime as _dt
    now = _dt.datetime.now().strftime("%Y-%m-%d %H:%M %a")
    user_text = f"[now: {now}]\n{user_text}"
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
                _json.dumps(mcp_server.config_entry(tools=selected))]
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
        cwd=_cwd(conv),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env={**os.environ, "CI": "1"},
    )
    chunks: list[str] = []

    async def _pump() -> None:
        assert proc.stdout
        while True:
            chunk = await proc.stdout.read(512)
            if not chunk:
                break
            text = chunk.decode(errors="replace")
            chunks.append(text)
            if on_delta:
                await on_delta(text)

    limit = turn_timeout()
    try:
        await asyncio.wait_for(_pump(), timeout=limit)
        # Closing stdout is not the same as exiting. A copilot that streamed its
        # answer and then hung on shutdown held this await forever, outside the
        # ceiling above — the turn was finished and Arun still heard nothing.
        rc = await asyncio.wait_for(proc.wait(), timeout=30)
    except asyncio.TimeoutError:
        proc.kill()
        raise RuntimeError(f"Copilot CLI turn timed out after {limit}s")
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
    return llm_meter.Usage(input=int(current),
                           output=reply_chars // llm_meter.CHARS_PER_TOKEN,
                           measured=True)


async def one_shot(prompt: str, cwd: str | None = None, timeout: int = 600,
                   agent: str = "", effort: str = "",
                   session_id: str = "", resume: bool = False,
                   on_progress=None) -> str:
    """Headless one-off prompt.

    agent      — a workspace .github/agents/*.agent.md pipeline (e.g.
                 the staged pipeline); discovered from cwd, so cwd must be the
                 workspace root.
    session_id — pin the Copilot session so a run that pauses at a human gate
                 (solo agent Stage 1) can be resumed later with resume=True.
    effort     — per-call reasoning effort; falls back to COPILOT_EFFORT_TASK.
    """
    if not available():
        raise RuntimeError("Copilot CLI is not installed/authenticated")
    cmd = ["copilot"]
    if session_id:
        cmd += ["--resume" if resume else "--session-id", session_id]
    cmd += ["-p", prompt, "-s", "--no-color", "--no-ask-user",
            "--allow-all-tools", "--allow-all-paths", "--log-level", "none"]
    if agent:
        cmd += ["--agent", agent]
    # Headless workers are where the money goes — one ran 22 minutes
    # unchallenged. Their own effort/credit ceiling, separate from chat.
    cmd += _budget_flags(effort or os.environ.get("COPILOT_EFFORT_TASK", "medium"),
                         os.environ.get("COPILOT_MAX_CREDITS_TASK", ""))
    proc = await asyncio.create_subprocess_exec(
        *cmd,
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
