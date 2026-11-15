"""pi as a code executor — the brain that writes code when ASTA_CODER=pi.

Planning, the check after a build and every fallback stay on Claude Code; pi
takes the legs that build (implement, follow-ups, fixes). Why pi for those: it
starts lean — four tools and a short system prompt — where a Claude Code leg
carries its own kit, and a build leg is where the turns pile up.

Same contract as claude_cli.one_shot, so tasks.py treats the two alike:
  · a session per task leg chain (`--session-id`), resumed by the same id;
  · the agent file as an appended system prompt, effort as pi's thinking level;
  · the task fence in pi's own tool_call hook (app/pi_fence.ts), the gh/git
    wrappers on PATH as before (task_fence.env);
  · usage read from pi's own session file, so a leg is measured, never guessed.

Run in RPC mode, not -p: a running leg can then take his words mid-build
(`steer`), delivered after the current tool calls finish and before the next
model call — the other brains can only hold a note until the next gate.

pi has no MCP. Asta's tools are not offered to a build leg; Serena/Context7
(ASTA_DEV_MCP) are skipped on pi legs.
"""

from __future__ import annotations

import asyncio
import contextlib
import glob
import json
import os
import shutil
import time
import uuid
from pathlib import Path

from . import store

ROOT = Path(__file__).resolve().parent.parent
FENCE = ROOT / "app" / "pi_fence.ts"
SESSIONS = ROOT / "data" / "pi-sessions"

#: Claude Code model aliases → pi model ids. routing.py speaks in aliases.
_MODELS = {"opus": "anthropic/claude-opus-5", "sonnet": "anthropic/claude-sonnet-5",
           "haiku": "anthropic/claude-haiku-4-5"}
DEFAULT_MODEL = "anthropic/claude-sonnet-5"

_DOWN_KEY = "pi_down"
#: How long pi stays out after an account error. Credit or a login does not come
#: back by itself in minutes; he is told once and the leg runs on Claude.
DOWN_SECONDS = 6 * 3600

#: The legs running right now, by task: what steer() writes to.
_live: dict[int, asyncio.subprocess.Process] = {}


class Unavailable(RuntimeError):
    """pi cannot take legs: no login, no credit, or out of quota."""


#: Account problems, as pi and the provider word them. Matched lowercase.
_ACCOUNT = ("extra usage", "oauth", "invalid_grant", "refresh token", "not logged in",
            "no api key", "authentication", "unauthorized", "401", "403",
            "quota exceeded", "429", "credit", "billing", "insufficient")


def available() -> bool:
    return shutil.which("pi") is not None


def down() -> dict:
    """{'until': ts, 'reason': str} while pi is out, else {}."""
    try:
        d = json.loads(store.kv_get(_DOWN_KEY) or "{}")
    except ValueError:
        return {}
    return d if float(d.get("until") or 0) > time.time() else {}


def usable() -> bool:
    return available() and not down()


def mark_down(reason: str, now: float | None = None) -> bool:
    """Record pi as out. True the first time in a down period — tell him then."""
    now = time.time() if now is None else now
    first = not down()
    store.kv_set(_DOWN_KEY, json.dumps({"until": now + DOWN_SECONDS, "reason": reason[:300]}))
    store.record_outcome("pi", "down", detail=reason[:300])
    return first


def mark_ok() -> None:
    if store.kv_get(_DOWN_KEY):
        store.kv_set(_DOWN_KEY, "")


def model_id(name: str) -> str:
    n = (name or "").strip()
    if not n:
        return DEFAULT_MODEL
    if "/" in n:
        return n
    return _MODELS.get(n.lower(), f"anthropic/{n}")


def account_error(text: str) -> bool:
    t = (text or "").lower()
    return any(k in t for k in _ACCOUNT)


def session_file(session_id: str) -> Path | None:
    hits = glob.glob(str(SESSIONS / "**" / f"*{glob.escape(session_id)}*.jsonl"), recursive=True)
    return Path(max(hits, key=os.path.getmtime)) if hits else None


def session_totals(session_id: str) -> dict | None:
    """Cumulative usage of a pi session, in worker_usage's fields."""
    f = session_file(session_id)
    out = {"calls": 0, "new_tokens": 0, "cache_read": 0, "out_tokens": 0}
    if f is None:
        return out
    try:
        for line in f.read_text(errors="replace").splitlines():
            try:
                row = json.loads(line)
            except ValueError:
                continue
            m = row.get("message") if isinstance(row, dict) else None
            u = m.get("usage") if isinstance(m, dict) and m.get("role") == "assistant" else None
            if not isinstance(u, dict) or not (u.get("input") or u.get("output")):
                continue
            out["calls"] += 1
            out["new_tokens"] += int(u.get("input") or 0) + int(u.get("cacheWrite") or 0)
            out["cache_read"] += int(u.get("cacheRead") or 0)
            out["out_tokens"] += int(u.get("output") or 0)
    except OSError:
        return None
    return out


def context_tokens(session_id: str) -> int:
    """The context of the session's last call — what the next resume re-reads."""
    f = session_file(session_id)
    if f is None:
        return 0
    last = 0
    with contextlib.suppress(OSError):
        for line in f.read_text(errors="replace").splitlines():
            try:
                row = json.loads(line)
            except ValueError:
                continue
            m = row.get("message") if isinstance(row, dict) else None
            u = m.get("usage") if isinstance(m, dict) and m.get("role") == "assistant" else None
            if isinstance(u, dict) and (u.get("input") or u.get("cacheRead")):
                last = int(u.get("input") or 0) + int(u.get("cacheRead") or 0) + int(u.get("cacheWrite") or 0)
    return last


def build_cmd(*, session_id: str, model: str = "", effort: str = "",
              agent_file: str = "") -> list[str]:
    cmd = ["pi", "--mode", "rpc", "--session-dir", str(SESSIONS), "--session-id", session_id,
           "--model", model_id(model),
           # Nothing of his interactive setup rides into a task leg: no skills,
           # prompt templates, themes or packages — only the fence.
           "--no-extensions", "--no-skills", "--no-prompt-templates", "--no-themes",
           "-e", str(FENCE)]
    if effort and effort != "default":
        cmd += ["--thinking", effort]
    if agent_file:
        cmd += ["--append-system-prompt", agent_file]
    return cmd


def steer(task_id: int, text: str) -> bool:
    """Hand his words to the leg building this task right now. True if it took them."""
    proc = _live.get(task_id)
    if proc is None or proc.returncode is not None or proc.stdin is None:
        return False
    try:
        proc.stdin.write((json.dumps({"type": "steer", "message": text}) + "\n").encode())
    except (BrokenPipeError, ConnectionResetError, RuntimeError):
        return False
    return True


def building(task_id: int) -> bool:
    proc = _live.get(task_id)
    return proc is not None and proc.returncode is None


async def one_shot(prompt: str, cwd: str | None = None, timeout: int = 600,
                   agent_file: str = "", effort: str = "", session_id: str = "",
                   resume: bool = False, on_progress=None, mcp_config: str = "",
                   plan_only: bool = False, model: str = "", tools_off: bool = False,
                   purpose: str = "", task_id: int | None = None) -> str:
    """One pi leg, run to the end; returns its final answer.

    `resume` needs no flag of its own: the same --session-id continues the
    session if its file exists. `mcp_config` and `tools_off` are accepted for
    the shared contract and have no pi equivalent."""
    if not available():
        raise Unavailable("pi is not installed")
    out = down()
    if out:
        raise Unavailable(f"pi is out until {time.strftime('%H:%M', time.localtime(out['until']))}: "
                          f"{out.get('reason', '')}")
    from . import copilot_cli, task_fence
    session_id = session_id or str(uuid.uuid4())
    SESSIONS.mkdir(parents=True, exist_ok=True)
    env = task_fence.env(os.environ.copy())
    env["ASTA_PI_DENY"] = json.dumps(task_fence.denied(task_fence.MODE.get()))
    env["ASTA_PI_PLAN_ONLY"] = "1" if plan_only else "0"
    env["PI_OFFLINE"] = "1"                  # no update checks inside a task leg
    proc = await asyncio.create_subprocess_exec(
        *build_cmd(session_id=session_id, model=model, effort=effort, agent_file=agent_file),
        cwd=cwd or str(ROOT), stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, env=env,
        limit=16 * 1024 * 1024)
    if task_id is not None:
        _live[task_id] = proc
    final = ""
    errors: list[str] = []

    async def _run() -> None:
        nonlocal final
        assert proc.stdin and proc.stdout
        proc.stdin.write((json.dumps({"type": "prompt",
                                      "message": f"{copilot_cli.now_line()}\n{prompt}"}) + "\n").encode())
        await proc.stdin.drain()
        async for raw in proc.stdout:
            line = raw.decode(errors="replace").rstrip("\r\n")
            if not line.startswith("{"):
                continue
            try:
                e = json.loads(line)
            except ValueError:
                continue
            t = e.get("type")
            if t == "response" and e.get("success") is False:
                errors.append(str(e.get("error") or e)[:400])
                return
            if t == "message_end":
                m = e.get("message") or {}
                if m.get("role") == "assistant":
                    if m.get("errorMessage"):
                        errors.append(m["errorMessage"][:400])
                    text = "".join(c.get("text", "") for c in m.get("content") or []
                                   if isinstance(c, dict) and c.get("type") == "text")
                    if text.strip():
                        final = text
                        if on_progress:
                            with contextlib.suppress(Exception):
                                await on_progress(text)
            elif t == "tool_execution_start" and on_progress:
                with contextlib.suppress(Exception):
                    await on_progress(f"\n[{e.get('toolName', 'tool')}] ")
            elif t == "agent_settled":
                return

    try:
        await asyncio.wait_for(_run(), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        raise RuntimeError("pi leg timed out")
    except asyncio.CancelledError:
        proc.kill()
        with contextlib.suppress(Exception):
            await proc.wait()
        raise
    finally:
        if task_id is not None and _live.get(task_id) is proc:
            _live.pop(task_id, None)
        if proc.returncode is None:
            with contextlib.suppress(Exception):
                proc.stdin.close()
            try:
                await asyncio.wait_for(proc.wait(), timeout=15)
            except asyncio.TimeoutError:
                proc.kill()
    if errors and not final.strip():
        msg = errors[-1]
        if account_error(msg):
            raise Unavailable(msg)
        raise RuntimeError(f"pi failed: {msg}")
    if not final.strip():
        err = b""
        with contextlib.suppress(Exception):
            err = await proc.stderr.read() if proc.stderr else b""
        raise RuntimeError(f"pi gave no answer (exit {proc.returncode}): {err.decode(errors='replace')[-300:]}")
    mark_ok()
    return final.strip()
