"""Claude Code CLI — headless task executor AND an interactive chat brain.

Used when Arun picks executor=claude, automatically while Copilot's monthly
quota is exhausted, or when he selects "Claude CLI" in the UI model picker.
Claude Code reads .claude/agents, not .github/agents, so the workspace's
project context pipeline (Asta's pipelines) rides in via
--append-system-prompt instead of --agent.

Why the CLI and not AnthropicModel: the CLI runs on Arun's Claude subscription,
which he already pays for. The API-key path is a separate prepaid account — the
same conversation would be billed twice over. Same reasoning as the Copilot CLI
path being preferred over an OpenAI key.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import shutil
import uuid
from pathlib import Path
from typing import Awaitable, Callable

from . import store

ROOT = Path(__file__).resolve().parent.parent

# Same ceiling as the Copilot chat path — a turn that runs past this is wedged.
TURN_TIMEOUT = 10 * 60


def available() -> bool:
    return bool(shutil.which("claude"))



# The CLI authenticates against the Claude subscription. Anthropic's client
# prefers ANTHROPIC_API_KEY when it is present in the environment — which is a
# DIFFERENT, prepaid account, so inheriting it silently bills the wrong one and
# fails outright once that balance runs dry ("Credit balance is too low"). Asta
# keeps the key in .env for the API-backed chat model, so it is in os.environ
# for every subprocess unless stripped here.
_STRIP_FROM_ENV = ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_BASE_URL")


def _subprocess_env() -> dict:
    env = {k: v for k, v in os.environ.items() if k not in _STRIP_FROM_ENV}
    env["CI"] = "1"
    return env


async def one_shot(prompt: str, cwd: str | None = None, timeout: int = 600,
                   agent_file: str = "", effort: str = "",
                   session_id: str = "", resume: bool = False,
                   on_progress=None) -> str:
    """Headless claude run with the same contract as copilot_cli.one_shot.

    agent_file — a .github/agents/*.agent.md whose CONTENT becomes the appended
    system prompt (re-attached on resume too; prompt caching makes that cheap).
    effort     — same ladder as Copilot (low|medium|high|xhigh|max).
    """
    if not available():
        raise RuntimeError("claude CLI is not installed")
    cmd = ["claude", "-p", prompt,
           # Parity with the copilot path's --allow-all-tools: the pipeline
           # must run builds/tests unattended. Workspace repos only.
           "--permission-mode", "bypassPermissions"]
    if session_id:
        cmd += (["--resume", session_id] if resume else ["--session-id", session_id])
    if agent_file:
        with contextlib.suppress(OSError):
            cmd += ["--append-system-prompt", Path(agent_file).read_text()]
    if effort and effort != "default":
        cmd += ["--effort", effort]
    model = os.environ.get("ASTA_CLAUDE_CLI_MODEL", "")
    if model:
        cmd += ["--model", model]
    proc = await asyncio.create_subprocess_exec(
        *cmd, cwd=cwd or str(ROOT),
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
        env=_subprocess_env(),
    )
    async def _drain() -> str:
        # Read incrementally rather than communicate(), so on_progress can watch
        # the run go by (stage milestones) instead of only seeing the final blob.
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
        raise RuntimeError("claude one-shot timed out")
    except asyncio.CancelledError:
        # Same rule as copilot: rejecting a task must actually stop the spend.
        proc.kill()
        with contextlib.suppress(Exception):
            await proc.wait()
        raise
    if proc.returncode != 0:
        raise RuntimeError(f"claude exited {proc.returncode}: {out[-300:]}")
    return out.strip()


# --- interactive chat turn ---------------------------------------------------

def _session_id(conv_id: str) -> tuple[str, bool]:
    """(session_id, is_new) — one Claude Code session per Asta conversation.

    Kept in its own kv namespace so switching the picker between Copilot and
    Claude mid-conversation keeps two independent threads rather than feeding
    one CLI another's session id.
    """
    key = f"claude_session:{conv_id}"
    sid = store.kv_get(key)
    if sid:
        return sid, False
    sid = str(uuid.uuid4())
    store.kv_set(key, sid)
    return sid, True


def _cwd(conv: dict) -> str:
    from . import workspace_tools
    ws = conv.get("workspace")
    if ws and ws in workspace_tools.WORKSPACES:
        return str(workspace_tools.WORKSPACES[ws])
    return str(ROOT)


def _build_cmd(conv: dict, user_text: str) -> list[str]:
    from . import copilot_cli, memory
    import datetime as _dt

    sid, is_new = _session_id(conv["id"])
    now = _dt.datetime.now().strftime("%Y-%m-%d %H:%M %a")
    user_text = f"[now: {now}]\n{user_text}"
    prompt = user_text
    if is_new:
        # Reuse the Copilot orientation block verbatim — same assistant, same
        # tools, same rules; only the CLI underneath differs.
        prompt = copilot_cli._first_turn_context(conv, via="Claude Code CLI") + "\n\n---\n\n" + user_text
    else:
        rb = memory.recall_block(user_text)
        if rb:
            prompt = f"[{rb}]\n\n{user_text}"
    cmd = [
        "claude",
        "--resume" if not is_new else "--session-id", sid,
        "-p", prompt,
        "--output-format", "stream-json",
        "--include-partial-messages",
        "--verbose",                       # required for stream-json
        "--permission-mode", "bypassPermissions",
    ]
    model = os.environ.get("ASTA_CLAUDE_CLI_MODEL", "")
    if model:
        cmd += ["--model", model]
    effort = os.environ.get("ASTA_CLAUDE_EFFORT", "")
    if effort and effort != "default":
        cmd += ["--effort", effort]
    return cmd


async def run_turn(conv: dict, user_text: str,
                   on_delta: Callable[[str], Awaitable[None]] | None = None,
                   on_tool: Callable[[str], Awaitable[None]] | None = None) -> str:
    """One chat turn through Claude Code CLI, streaming text deltas to on_delta.

    stream-json gives token-level deltas plus tool_use events, so the UI shows
    the same live "Checking …" activity the Copilot path does.
    """
    if not available():
        raise RuntimeError("claude CLI is not installed (get it: https://claude.com/claude-code)")
    proc = await asyncio.create_subprocess_exec(
        *_build_cmd(conv, user_text), cwd=_cwd(conv),
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        env=_subprocess_env(),
    )
    chunks: list[str] = []
    final = ""
    err_msg = ""
    limited = False

    async def _pump() -> None:
        nonlocal final, err_msg, limited
        assert proc.stdout
        buf = b""
        while True:
            block = await proc.stdout.read(4096)
            if not block:
                break
            buf += block
            # stream-json is newline-delimited; a 4k read can split a line.
            *lines, buf = buf.split(b"\n")
            for raw in lines:
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    e = json.loads(raw)
                except ValueError:
                    continue
                t = e.get("type")
                if t == "stream_event":
                    ev = e.get("event") or {}
                    if ev.get("type") == "content_block_delta":
                        d = ev.get("delta") or {}
                        if d.get("type") == "text_delta":
                            text = d.get("text") or ""
                            if text:
                                chunks.append(text)
                                if on_delta:
                                    await on_delta(text)
                elif t == "assistant" and on_tool:
                    for part in ((e.get("message") or {}).get("content") or []):
                        if isinstance(part, dict) and part.get("type") == "tool_use":
                            with contextlib.suppress(Exception):
                                await on_tool(part.get("name") or "tool")
                elif t == "rate_limit_event":
                    info = e.get("rate_limit_info") or {}
                    if info.get("status") not in ("allowed", None):
                        limited = True
                elif t == "result":
                    if e.get("is_error"):
                        err_msg = str(e.get("result") or e.get("api_error_status") or "")[:400]
                    else:
                        final = (e.get("result") or "").strip()

    try:
        await asyncio.wait_for(_pump(), timeout=TURN_TIMEOUT)
        rc = await proc.wait()
    except asyncio.TimeoutError:
        proc.kill()
        raise RuntimeError(f"Claude CLI turn timed out after {TURN_TIMEOUT}s")
    except asyncio.CancelledError:
        # Arun redirected mid-answer. Killing the process is the point: it stops
        # the wrong line of work from consuming any more of the session quota.
        proc.kill()
        with contextlib.suppress(Exception):
            await proc.wait()
        raise

    # Prefer the streamed text: `result` repeats it, and re-sending it would
    # duplicate everything the UI already rendered.
    out = ("".join(chunks).strip() or final).strip()
    if limited:
        store.kv_set("claude_quota_down", str(int(asyncio.get_event_loop().time())))
    if rc != 0 or (err_msg and not out):
        stderr = (await proc.stderr.read()).decode(errors="replace")[-500:] if proc.stderr else ""
        # A dead --resume session (cleaned store / expired) gets one fresh retry.
        if not out and store.kv_get(f"claude_session:{conv['id']}"):
            store.kv_del(f"claude_session:{conv['id']}")
            return await run_turn(conv, user_text, on_delta, on_tool)
        raise RuntimeError(f"Claude CLI exited {rc}: {err_msg or stderr or out[-300:] or 'no output'}")
    return out or "(Claude returned no output)"
