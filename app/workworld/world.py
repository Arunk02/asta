"""The simulated world, and the sandbox that keeps it simulated.

Everything Asta can touch that is not Asta — his phone, Teams, Outlook, Jira,
GitHub, the brains, the clock's effect on a task's age — is replaced here by a
recorder or a script. Two properties matter more than the fakes themselves:

  ONE GUARD, NOT FIFTY REMEMBERINGS. `install()` patches every outward door and
  the database in one place, and `assert_sandboxed()` fails loudly if a scenario
  runs without it. The alternative — each scenario remembering to patch what it
  might touch — is how a test suite eventually sends a real message.

  THE DOUBLES ARE AT THE EDGE. `tasks`, `main._dispatch`, `responder`,
  `attention`, `notify`'s own ledger logic all run for real. What is faked is
  the process boundary: a CLI brain's stdout, a browser's click, an HTTP call.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import re
import tempfile
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent


class SandboxBreach(RuntimeError):
    """Something reached for the real world. Always a scenario failure."""


@dataclass
class BrainReply:
    """One scripted answer from a brain.

    `when` — a regex the prompt must match for this reply to be used; None means
    "the next call, whatever it is". `calls` — capabilities the brain invokes
    before answering, which is what a CLI brain does over MCP.
    """
    text: str = ""
    when: str | None = None
    raises: str | None = None
    calls: list[dict] = field(default_factory=list)
    context_tokens: int = 0
    sleep: float = 0.0


class ScriptedBrain:
    """A brain whose answers are written down, so a scenario is deterministic.

    Replies are matched by `when` first (a prompt regex), else taken in order.
    Running out of script is not an error: an unscripted call gets a neutral
    answer, because a scenario should fail on what it asserts, not on how many
    times a brain happened to be consulted.
    """

    def __init__(self, replies: list[BrainReply], world: "World", kind: str):
        self.replies = list(replies)
        self.world = world
        self.kind = kind

    def _pick(self, prompt: str) -> BrainReply | None:
        for i, r in enumerate(self.replies):
            if r.when and re.search(r.when, prompt or "", re.I | re.S):
                return self.replies.pop(i)
        for i, r in enumerate(self.replies):
            if not r.when:
                return self.replies.pop(i)
        return None

    async def answer(self, prompt: str, conv_id: str = "", flags: dict | None = None) -> str:
        flags = flags or {}
        # How the leg was asked to run, not only what it was asked: effort, model
        # and the plan-only gate are what the routing scenarios assert on.
        self.world.brain_calls.append({"kind": self.kind, "prompt": prompt or "",
                                       "at": time.time(),
                                       "effort": flags.get("effort", ""),
                                       "model": flags.get("model", ""),
                                       "plan_only": bool(flags.get("plan_only"))})
        reply = self._pick(prompt or "")
        if reply is None:
            self.last_context = 0
            return "(nothing scripted)"
        # What this call carried, for the size-based session retirement to see.
        self.last_context = reply.context_tokens
        if reply.sleep:
            await asyncio.sleep(reply.sleep)
        for call in reply.calls:
            await self.world.call_capability(call.get("tool", ""), call.get("args") or {},
                                             conv_id)
        if reply.raises:
            raise RuntimeError(reply.raises)
        return reply.text


class Patcher:
    """setattr with an undo, so a scenario cannot leak into the next one."""

    def __init__(self) -> None:
        self._undo: list[tuple[object, str, object, bool]] = []

    def set(self, obj: object, name: str, value: object) -> None:
        had = hasattr(obj, name)
        self._undo.append((obj, name, getattr(obj, name, None), had))
        setattr(obj, name, value)

    def undo(self) -> None:
        while self._undo:
            obj, name, old, had = self._undo.pop()
            if had:
                setattr(obj, name, old)
            else:
                with contextlib.suppress(AttributeError):
                    delattr(obj, name)


@dataclass
class World:
    """What happened, and what the outside world would have said."""
    pushes: list[dict] = field(default_factory=list)          # to his phone
    replies: list[dict] = field(default_factory=list)         # in the chat
    sent: list[dict] = field(default_factory=list)            # OUT of the house
    brain_calls: list[dict] = field(default_factory=list)
    prs: dict = field(default_factory=dict)                   # url -> gh payload
    verify: list[dict] = field(default_factory=list)          # scripted check runs
    teams_activity: list[str] = field(default_factory=list)
    breaches: list[str] = field(default_factory=list)
    #: What each tool call handed BACK to the brain — the only way to assert what
    #: a model was told, as opposed to what it then chose to say.
    tool_results: list[dict] = field(default_factory=list)
    db_path: Path | None = None
    _patch: Patcher = field(default_factory=Patcher)

    # --- what Asta said and did ------------------------------------------

    def phone_text(self) -> str:
        return "\n".join(p["text"] for p in self.pushes)

    def chat_text(self) -> str:
        return "\n".join(r.get("text", "") for r in self.replies)

    def everything_said(self) -> str:
        return self.phone_text() + "\n" + self.chat_text()

    async def call_capability(self, name: str, args: dict, conv_id: str = "") -> object:
        """A brain calling one of Asta's own tools, the way MCP does."""
        import inspect
        from app import capabilities, tasks
        cap = capabilities.get(name)
        if cap is None or cap.fn is None:
            self.breaches.append(f"brain called unknown capability {name!r}")
            return None
        token = tasks.bind_conversation(conv_id) if conv_id else None
        try:
            out = cap.fn(**args)
            if inspect.isawaitable(out):
                out = await out
        except Exception as exc:                               # noqa: BLE001
            # What the MCP server does with a tool that raises: the brain gets
            # the error as the tool's answer, and the turn carries on.
            out = f"Error: {exc}"
        finally:
            if token is not None:
                tasks.unbind_conversation(token)
        self.tool_results.append({"tool": name, "text": str(out)[:2000]})
        return out

    def intent_guess(self, verdict: str) -> None:
        """What the brain's intent classifier WOULD say, if anyone asked it — so a
        scenario can prove a routing decision does not rest on its guess."""
        from app import agent

        async def guess(text, model_name=""):
            self.brain_calls.append({"kind": "intent", "prompt": text or "", "at": time.time()})
            return verdict
        self._patch.set(agent, "quick_intent", guess)

    def use_jira(self, issues: dict) -> None:
        """Jira reads over a transport double. The real client runs — its status
        handling, its 404 translation — and nothing leaves the house. A key that
        is not in `issues` answers 404, the way a ticket he cannot see does."""
        import httpx
        from app import jira

        def respond(request: httpx.Request) -> httpx.Response:
            path = request.url.path
            key = path.split("/issue/")[-1].split("/")[0] if "/issue/" in path else ""
            issue = issues.get(key)
            if issue is None:
                return httpx.Response(404, json={"errorMessages": ["Issue does not exist"]})
            if path.endswith("/comment"):
                return httpx.Response(200, json={"comments": [], "total": 0})
            return httpx.Response(200, json={"key": key, **issue})

        self._patch.set(jira, "configured", lambda: True)
        self._patch.set(jira, "_client", lambda: httpx.AsyncClient(
            base_url="https://jira.invalid", transport=httpx.MockTransport(respond)))

    # --- the sandbox ------------------------------------------------------

    def install(self, chat_brain: ScriptedBrain | None = None,
                task_brain: ScriptedBrain | None = None, live_brains: bool = False) -> None:
        """Put the doubles in place. Nothing may run before this."""
        from app import (attention, briefing, chat_watch, claude_cli, copilot_cli, delivery,
                         jira, main, meetings, memory, notify, outlook, presence, quiet,
                         repo_ops, store, tasks, teams_bridge, telegram, verify, wa_bridge)
        p = self._patch

        # 1. The database: a fresh file per scenario. Checked again by
        #    assert_sandboxed, because everything else rests on it.
        self.db_path = Path(tempfile.mkdtemp(prefix="workworld-")) / "world.db"
        p.set(store, "DB_PATH", self.db_path)
        store.init()
        # Every working tree a scenario may touch lives here; see the git guard.
        self.scratch = self.db_path.parent / "workspace"
        (self.scratch / "repo").mkdir(parents=True, exist_ok=True)

        # 2. His phone. notify's own ledger/dedup logic still runs; only the
        #    delivery at the end of it is a recorder.
        async def wa_send(text: str) -> bool:
            self.pushes.append({"text": text, "level": "whatsapp", "urgency": "direct"})
            return True

        async def notify_fn(text, level="info", urgency="direct", priority=None, *,
                            source="", key="", considered=False):
            self.pushes.append({"text": text, "level": level, "urgency": urgency})
            store.add_notification(text, level)
            return {"whatsapp": True}

        p.set(notify, "wa_send", wa_send)
        p.set(notify, "notify", notify_fn)
        p.set(notify, "wa_status", _async_value({"up": True, "paired": True, "enabled": True}))
        for mod in (main, tasks, chat_watch, outlook, briefing, meetings, attention, delivery):
            if hasattr(mod, "notify"):
                p.set(mod, "notify", notify)          # module object, already patched

        # 3. Outward doors. Every one records instead of acting.
        p.set(teams_bridge, "send_message", self._door("teams", "chat"))
        p.set(teams_bridge, "send_voice_note", self._door("teams-voice", "chat"))
        p.set(telegram, "send", self._door("telegram", None))
        p.set(jira, "add_comment", self._door("jira-comment", "key"))
        p.set(jira, "transition_issue", self._door("jira-transition", "key"))
        p.set(meetings, "call_person", self._door("call", "who"))
        p.set(meetings, "join", self._door("meeting-join", "join_url"))
        p.set(teams_bridge, "set_presence", self._door("presence", "state"))

        # 4. Things that read the outside world.
        p.set(teams_bridge, "read_activity", _async_value_fn(lambda *a, **k: list(self.teams_activity)))
        p.set(teams_bridge, "enabled", lambda: True)
        p.set(teams_bridge, "logged_in_once", lambda: True)
        p.set(teams_bridge, "in_a_call", lambda: False)
        p.set(outlook, "read_mail", _async_value_fn(lambda *a, **k: []))
        p.set(presence, "at_laptop", lambda *a, **k: False)
        p.set(wa_bridge, "status", lambda: {"running": True})
        p.set(quiet, "in_quiet_hours", lambda *a, **k: False)
        p.set(memory, "local_llm_complete", lambda *a, **k: None)
        p.set(memory, "local_embed", lambda *a, **k: None)
        # The workspace names his config maps to real checkouts; here they all
        # resolve into the scratch directory.
        from app import workspace as workspace_mod, workspace_tools
        for mod in (workspace_mod, workspace_tools):
            if hasattr(mod, "WORKSPACES"):
                p.set(mod, "WORKSPACES", {"booking": self.scratch, "empv3": self.scratch})

        # 5. Git and GitHub. His real checkouts are a few directories away from
        #    the workspace names in `data/workspaces.json`, and a code task
        #    branches, fetches and checks out — so the whole of git is confined
        #    to a scratch directory here, and anything outside it is a breach,
        #    not a surprise on his working tree.
        async def git(repo, *args, timeout=120):
            cmd = " ".join(str(a) for a in args)
            if str(Path(repo).resolve()) != str(self.scratch.resolve()) and \
                    self.scratch.resolve() not in Path(repo).resolve().parents:
                self.breaches.append(f"git outside the sandbox: {repo} — {cmd[:60]}")
                return 1, "blocked by the sandbox"
            self.sent.append({"door": "git", "cmd": cmd, "repo": str(repo)})
            if "gh pr create" in cmd:
                url = f"https://github.com/x/y/pull/{900 + len(self.sent)}"
                self.prs.setdefault(url, {"state": "OPEN", "statusCheckRollup": [],
                                          "reviewDecision": "", "url": url})
                return 0, url
            return 0, ""

        p.set(repo_ops, "git", git)

        # Everything a code task does to a working tree, pointed at the scratch
        # directory. The task engine itself is real; only the trees are not his.
        p.set(tasks, "_cwd", lambda ws: str(self.scratch))
        p.set(tasks, "task_cwd", lambda tid, ws: str(self.scratch))
        p.set(tasks, "code_cwd", lambda ws: str(self.scratch))
        p.set(tasks, "_prepare_branches", _async_value_fn(lambda *a, **k: []))
        p.set(tasks, "mark_rollback_point", _async_value_fn(lambda *a, **k: {}))
        p.set(tasks, "committed_so_far", lambda *a, **k: [])
        p.set(tasks, "_repos_still_needed", lambda *a, **k: [])
        p.set(tasks, "_self_review", _async_value(""))
        p.set(tasks, "_audit_note", lambda tid: "")
        p.set(tasks, "task_tools", lambda *a, **k: "")

        async def pr_state(url):
            return dict(self.prs.get(url) or {})
        p.set(tasks, "_pr_state", pr_state)

        # 6. The repo's own check, scripted — `verify.run` shells out otherwise.
        # The gate only runs when the repo HAS a check; the scratch repo has
        # none, so a scenario that scripts outcomes is declaring one.
        p.set(verify, "enabled", lambda: bool(self.verify))
        p.set(verify, "resolve_command", lambda cwd, workspace=None: "pytest -q" if self.verify else "")

        async def verify_run(cwd, cmd, _retried=False):
            step = self.verify.pop(0) if self.verify else {"ok": True}
            ok = bool(step.get("ok", True))
            return verify.VerifyResult(ran=bool(step.get("ran", True)), ok=ok,
                                       command=cmd, code=0 if ok else 1,
                                       tail=step.get("tail", ""))
        p.set(verify, "run", verify_run)

        # 7. The brains. A LIVE brain is a real subprocess with a real shell, so
        #    the one thing it must not be given is a door back into the running
        #    Asta: no MCP config, no port to curl. Its tools are its own
        #    filesystem, inside the scratch directory.
        if live_brains:
            self._env_undo = []
            for key, value in (("ASTA_CLI_MCP", "0"), ("ASTA_PORT", "0"),
                               ("ASTA_MCP_CONV", "")):
                self._env_undo.append((key, os.environ.get(key)))
                os.environ[key] = value
            p.set(copilot_cli, "mcp_cli_enabled", lambda: False)
        if not live_brains:
            chat = chat_brain or ScriptedBrain([], self, "chat")
            task = task_brain or ScriptedBrain([], self, "task")

            async def run_turn(conv, user_text, on_delta=None, on_tool=None, on_usage=None):
                out = await chat.answer(user_text, conv.get("id", ""))
                if on_delta:
                    await on_delta(out)
                if on_usage:
                    from app import llm_meter
                    ctx = getattr(chat, "last_context", 0)
                    on_usage(llm_meter.Usage(input=2, cache_read=ctx, context=ctx, measured=True))
                return out

            async def one_shot(prompt, **kw):
                return await task.answer(prompt, flags=kw)

            for mod in (claude_cli, copilot_cli):
                p.set(mod, "run_turn", run_turn)
                p.set(mod, "one_shot", one_shot)
                p.set(mod, "available", lambda: True)
            self.chat_brain, self.task_brain = chat, task

    def _door(self, name: str, first: str | None):
        """A recorder in the shape of an outward call."""
        async def door(*args, **kwargs):
            payload = {"door": name, "args": list(args), "kwargs": dict(kwargs)}
            if first and args:
                payload[first] = args[0]
            if first and first in kwargs:
                payload[first] = kwargs[first]
            payload["text"] = next((a for a in args[1:] if isinstance(a, str)),
                                   kwargs.get("text", ""))
            self.sent.append(payload)
            return "sent (simulated)"
        return door

    def assert_sandboxed(self) -> None:
        from app import store
        live = (ROOT / "data" / "asta.db").resolve()
        if store.DB_PATH is None or Path(store.DB_PATH).resolve() == live:
            raise SandboxBreach("the scenario would have written to the live database")

    def uninstall(self) -> None:
        for key, old in getattr(self, "_env_undo", []):
            if old is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = old
        self._env_undo = []
        self._patch.undo()


def _async_value(value):
    async def fn(*a, **k):
        return value
    return fn


def _async_value_fn(make):
    async def fn(*a, **k):
        return make(*a, **k)
    return fn


async def settle(timeout: float = 20.0) -> bool:
    """Wait for everything the last step set in motion. False when something hung.

    Asta's real paths spawn work — a task worker, a chat turn, a fire-and-forget
    learn pass — and a scenario that asserts before those finish measures the
    wrong moment. Anything still pending at the timeout is itself a finding.
    """
    me = asyncio.current_task()
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        pending = [t for t in asyncio.all_tasks() if t is not me and not t.done()]
        if not pending:
            return True
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait(pending, timeout=max(0.05, deadline - time.monotonic()))
        await asyncio.sleep(0)
    return not [t for t in asyncio.all_tasks() if t is not me and not t.done()]


class Sink:
    """What the chat shows him: notes, streamed text, and nothing else."""

    def __init__(self, world: World):
        self.world = world
        self.alive = True

    async def send(self, payload: dict) -> None:
        kind = payload.get("type")
        if kind in ("note", "delta"):
            self.world.replies.append({"type": kind, "text": payload.get("text", "")})


def new_conversation(channel: str = "whatsapp", workspace: str | None = None,
                     model: str = "claude_cli") -> dict:
    from app import store
    conv = store.create_conversation(model=model, workspace=workspace)
    conv["model"] = model
    conv["workspace"] = workspace
    return conv


def task_row(title: str, kind: str = "code", status: str = "running",
             result: str = "", age_hours: float = 0.0, workspace: str | None = None,
             prompt: str = "do it", teams_chat: str = "", pr_urls: str = "") -> int:
    """A task as the world found it — including how long it has been sitting."""
    from app import store
    t = store.create_task(title, kind, prompt, workspace, teams_chat)
    fields = {"status": status, "result": result}
    if pr_urls:
        fields.update(pr_urls=pr_urls, pr_state="OPEN")
    store.update_task(t["id"], **fields)
    if age_hours:
        then = time.time() - age_hours * 3600
        with store._connect() as conn:
            conn.execute("UPDATE tasks SET created_at=?, started_at=? WHERE id=?",
                         (then, then, t["id"]))
    return t["id"]


def uid() -> str:
    return uuid.uuid4().hex[:8]


def dumps(obj) -> str:
    return json.dumps(obj, default=str)
