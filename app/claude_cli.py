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

# Same ceiling as the other CLI chat path — a turn past this is wedged, not slow.
TURN_TIMEOUT = 10 * 60          # legacy constant; live callers use turn_timeout()


def turn_timeout() -> int:
    """Shared ceiling for one CLI turn (ASTA_TURN_TIMEOUT, default 5 min)."""
    from . import copilot_cli
    return copilot_cli.turn_timeout()


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
                   on_progress=None, mcp_config: str = "") -> str:
    """Headless claude run with the same contract as copilot_cli.one_shot.

    agent_file — a .github/agents/*.agent.md whose CONTENT becomes the appended
    system prompt (re-attached on resume too; prompt caching makes that cheap).
    effort     — same ladder as Copilot (low|medium|high|xhigh|max).
    mcp_config — inline mcpServers JSON to attach for this run (e.g. the dev MCP
                 servers for a code task). Empty leaves the command untouched, so
                 the default path is byte-for-byte what it was.
    """
    if not available():
        raise RuntimeError("claude CLI is not installed")
    cmd = ["claude", "-p", prompt,
           # Parity with the copilot path's --allow-all-tools: the pipeline
           # must run builds/tests unattended. Workspace repos only.
           "--permission-mode", "bypassPermissions"]
    if mcp_config:
        # No --strict-mcp-config: these servers ADD to whatever the workspace
        # already configures, they don't replace it. `claude --mcp-config` takes
        # a JSON string as readily as a file, so nothing touches disk.
        cmd += ["--mcp-config", mcp_config]
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


def _build_cmd(conv: dict, user_text: str, prefetched: str = "") -> list[str]:
    from . import copilot_cli, memory
    import datetime as _dt

    sid, is_new = _session_id(conv["id"])
    ranking_text = user_text            # the bare message, before the [now:] prefix
    now = _dt.datetime.now().strftime("%Y-%m-%d %H:%M %a")
    umsg = f"[now: {now}]\n{user_text}"
    if prefetched:
        # Live Teams/Outlook context, exactly as Copilot gets it. Same assistant,
        # same rules — a brain that answers "any messages for me?" from a real
        # inbox on one CLI and guesses on the other is two assistants.
        umsg = f"{prefetched}\n\n{umsg}"
    if not is_new:
        rb = memory.recall_block(user_text)
        if rb:
            umsg = f"[{rb}]\n\n{umsg}"
    # Asta's identity, capabilities and rules go in the SYSTEM prompt, NOT the
    # user message. Delivered as user text (the old way), a strong model read its
    # own operating manual as untrusted injected content and refused to act as
    # Asta at all — "I'm just Claude Code, I don't have those tools" — the exact
    # failure Arun hit. As a system prompt it is authoritative, the same way the
    # task pipeline (--append-system-prompt) always worked where chat didn't.
    # Re-sent every turn (a stable system prompt is nearly free under prompt
    # caching) so a resumed session never drifts back to plain Claude Code.
    system = copilot_cli._first_turn_context(
        conv, via="Claude Code CLI", user_text=ranking_text)
    cmd = [
        "claude",
        "--resume" if not is_new else "--session-id", sid,
        "--append-system-prompt", system,
        "-p", umsg,
        "--output-format", "stream-json",
        "--include-partial-messages",
        "--verbose",                       # required for stream-json
        "--permission-mode", "bypassPermissions",
    ]
    # Native tools instead of curl, when enabled: Claude Code spawns Asta's MCP
    # server and calls capabilities as `mcp__asta__*` tools that forward to the
    # running server. Off by default — the curl path is the proven one, and this
    # is the opt-in cutover, flipped only after a real-turn test.
    if copilot_cli.mcp_cli_enabled():
        import json as _json
        from . import mcp_server, tool_index
        # Context-aware: expose the ~handful of tools this message needs (+ the
        # ALWAYS floor), not all 50 schemas. select_sticky returns None for an
        # ambiguous message → the full set (safe default), and the floor only for
        # a conversational one. INLINE JSON, not a file: `claude --mcp-config`
        # takes "files or strings", so passing the config directly avoids a
        # per-conversation file on disk (each held a copy of the token and leaked
        # when a chat was deleted) and matches the Copilot path exactly.
        selected = tool_index.select_sticky(conv["id"], ranking_text)
        cmd += ["--mcp-config", _json.dumps(mcp_server.config_entry(tools=selected)),
                "--strict-mcp-config"]
    model = os.environ.get("ASTA_CLAUDE_CLI_MODEL", "")
    if model:
        cmd += ["--model", model]
    effort = os.environ.get("ASTA_CLAUDE_EFFORT", "")
    if effort and effort != "default":
        cmd += ["--effort", effort]
    return cmd


async def run_turn(conv: dict, user_text: str,
                   on_delta: Callable[[str], Awaitable[None]] | None = None,
                   on_tool: Callable[[str], Awaitable[None]] | None = None,
                   on_usage: Callable[[object], None] | None = None) -> str:
    """One chat turn through Claude Code CLI, streaming text deltas to on_delta.

    stream-json gives token-level deltas plus tool_use events, so the UI shows
    the same live "Checking …" activity the Copilot path does.
    """
    if not available():
        raise RuntimeError("claude CLI is not installed (get it: https://claude.com/claude-code)")
    from . import copilot_cli
    # The Teams/Outlook pre-fetch existed because a CLI brain couldn't reliably
    # reach Asta to read them itself. With native MCP tools it can (and does), so
    # the pre-read is redundant AND costly — it blocks the turn ~25s BEFORE the
    # brain even starts. Under MCP the brain calls teams_activity/outlook_mail on
    # demand instead; the curl-era pre-fetch stays only for the MCP-off fallback.
    prefetched = "" if copilot_cli.mcp_cli_enabled() else await copilot_cli._prefetch(user_text)
    proc = await asyncio.create_subprocess_exec(
        *_build_cmd(conv, user_text, prefetched),
        cwd=_cwd(conv),
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        env=_subprocess_env(),
    )
    chunks: list[str] = []
    final = ""
    err_msg = ""
    limited = False
    # A CLI turn is a whole agent loop — many model calls, each with its own
    # usage block. Summing them is the point: the expensive turns are the ones
    # that looped, and a turn that only reported its last call would hide that.
    from . import llm_meter
    usage = llm_meter.Usage()
    seen_usage: set[str] = set()

    async def _pump() -> None:
        nonlocal final, err_msg, limited, usage
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
                elif t == "assistant":
                    message = e.get("message") or {}
                    # The same assistant message is emitted more than once in a
                    # stream (partials, then the settled copy). Counting its
                    # usage twice would roughly double every reported figure, so
                    # each message id contributes exactly once.
                    mid = message.get("id") or ""
                    if mid not in seen_usage:
                        seen_usage.add(mid)
                        usage = usage + llm_meter.from_anthropic(message.get("usage"))
                    if on_tool:
                        for part in (message.get("content") or []):
                            if isinstance(part, dict) and part.get("type") == "tool_use":
                                with contextlib.suppress(Exception):
                                    await on_tool(part.get("name") or "tool")
                elif t == "rate_limit_event":
                    info = e.get("rate_limit_info") or {}
                    if info.get("status") not in ("allowed", None):
                        limited = True
                elif t == "result":
                    # The CLI's own tally of the whole turn. On a subscription
                    # this is 0, which is honest — the token columns are what
                    # compares across brains.
                    usage.cost_usd = float(e.get("total_cost_usd") or 0.0)
                    if e.get("is_error"):
                        err_msg = str(e.get("result") or e.get("api_error_status") or "")[:400]
                    else:
                        final = (e.get("result") or "").strip()

    def _report() -> None:
        """Hand back whatever was spent — including on the paths that failed.

        A timed-out or redirected turn has already burned its tokens; those are
        the turns most worth seeing, so reporting only on success would blind
        the meter to the worst cases."""
        if on_usage:
            with contextlib.suppress(Exception):
                on_usage(usage)

    limit = turn_timeout()
    try:
        await asyncio.wait_for(_pump(), timeout=limit)
        # Same reason as copilot_cli: end-of-output is not end-of-process, and an
        # unbounded wait here holds a finished turn open indefinitely.
        rc = await asyncio.wait_for(proc.wait(), timeout=30)
    except asyncio.TimeoutError:
        proc.kill()
        _report()
        raise RuntimeError(f"CLI turn timed out after {limit}s")
    except asyncio.CancelledError:
        # Arun redirected mid-answer. Killing the process is the point: it stops
        # the wrong line of work from consuming any more of the session quota.
        proc.kill()
        with contextlib.suppress(Exception):
            await proc.wait()
        _report()
        raise
    _report()

    # Prefer the streamed text: `result` repeats it, and re-sending it would
    # duplicate everything the UI already rendered.
    out = ("".join(chunks).strip() or final).strip()
    if limited:
        # ONE quota table, keyed the same way for every brain. This used to write
        # "claude_quota_down", a key nothing read — the picker and fallback both
        # consult agent.quota_down("claude_cli") (i.e. "claude_cli_quota_down"),
        # so the interactive rate-limit signal was silently dropped.
        from . import agent as agent_mod
        agent_mod.mark_quota_down("claude_cli")
    if rc != 0 or (err_msg and not out):
        stderr = (await proc.stderr.read()).decode(errors="replace")[-500:] if proc.stderr else ""
        # A dead --resume session (cleaned store / expired) gets one fresh retry.
        if not out and store.kv_get(f"claude_session:{conv['id']}"):
            store.kv_del(f"claude_session:{conv['id']}")
            # The dead attempt already reported what it spent; the retry reports
            # its own. The caller accumulates, so a wasted session shows up as
            # the extra cost it really is rather than being written off.
            return await run_turn(conv, user_text, on_delta, on_tool, on_usage)
        raise RuntimeError(f"Claude CLI exited {rc}: {err_msg or stderr or out[-300:] or 'no output'}")
    return out or "(Claude returned no output)"
