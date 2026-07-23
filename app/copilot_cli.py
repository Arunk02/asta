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
import os
import re
import shutil
import uuid
from pathlib import Path
from typing import Awaitable, Callable

from . import memory, store, untrusted, workspace_tools

ROOT = Path(__file__).resolve().parent.parent
TURN_TIMEOUT = 10 * 60


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


def _first_turn_context(conv: dict, via: str = "Copilot CLI") -> str:
    """Orientation block for a fresh CLI session (it has no Asta memory).

    The capability list is GENERATED from the same registry that gives the chat
    agent its tools, so a new tool is taught here the moment it is added — there
    is no second description to keep in sync, and no stale copilot_session:* rows
    to clear after editing prose.

    Shared with claude_cli: same assistant, same tools, same rules — only the
    CLI underneath differs, so `via` is the one thing that changes.

    CLI brains never see agent.PERSONA, so the safety policy rides here.
    """
    from . import capabilities
    name = os.environ.get("ASSISTANT_NAME", "Asta")
    parts = [
        f"You are acting as {name}, Arun's assistant, via {via}. Be concise and direct.",
    ]
    idx = memory.index_text().strip()
    if idx:
        parts.append("Arun's memory index (for orientation):\n" + idx[:1500])
    port = os.environ.get("ASTA_PORT", "8321")
    parts.append(capabilities.cli_block(port, str(ROOT)))
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
    # Every turn carries the current local time — long-lived sessions otherwise
    # drift days behind, which breaks "remind me at 3pm" style requests.
    import datetime as _dt
    now = _dt.datetime.now().strftime("%Y-%m-%d %H:%M %a")
    user_text = f"[now: {now}]\n{user_text}"
    if extra_context:
        user_text = f"{extra_context}\n\n{user_text}"
    prompt = user_text
    if is_new:
        prompt = _first_turn_context(conv) + "\n\n---\n\n" + user_text
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


async def run_turn(conv: dict, user_text: str,
                   on_delta: Callable[[str], Awaitable[None]] | None = None) -> str:
    """One chat turn through Copilot CLI, streaming stdout chunks to on_delta."""
    if not available():
        raise RuntimeError("Copilot CLI is not installed/authenticated (run: copilot login)")
    proc = await asyncio.create_subprocess_exec(
        *_build_cmd(conv, user_text, "\n\n".join(
            b for b in (await _teams_activity_context(user_text),
                        await _outlook_context(user_text)) if b)),
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

    try:
        await asyncio.wait_for(_pump(), timeout=TURN_TIMEOUT)
        rc = await proc.wait()
    except asyncio.TimeoutError:
        proc.kill()
        raise RuntimeError(f"Copilot CLI turn timed out after {TURN_TIMEOUT}s")
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
        if not out and store.kv_get(f"copilot_session:{conv['id']}"):
            store.kv_del(f"copilot_session:{conv['id']}")
            return await run_turn(conv, user_text, on_delta)
        raise RuntimeError(f"Copilot CLI exited {rc}: {err or out[-300:] or 'no output'}")
    return out or "(Copilot returned no output)"


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
