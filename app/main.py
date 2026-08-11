"""Asta FastAPI server: WebSocket chat, REST for conversations/memory, graph hosting."""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import re
import secrets
import time

import httpx
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")

from fastapi import Depends, FastAPI, File, HTTPException, Request, UploadFile, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic_ai import AgentRunResultEvent
from pydantic_ai.messages import (
    FunctionToolCallEvent,
    FunctionToolResultEvent,
    ModelMessagesTypeAdapter,
    ModelRequest,
    PartDeltaEvent,
    PartStartEvent,
    TextPart,
    TextPartDelta,
)

from . import agent as agent_mod
from . import activity, asking, attention, briefing, capabilities, ci_watch, claude_cli, context_build, copilot_cli, delivery, health, jira, learn, llm_meter, loop, mcp_loader, memory, msnotify, notify, offers, ops, outlook, refresh, reminders, relevance, resume, router, quality, store, tasks, teams_bridge, telegram, tool_index, wa_bridge, workspace, workspace_tools

UI_DIR = ROOT / "ui"

app = FastAPI(title="Asta")

AGENT = agent_mod.build_agent()
MCP_TOOLSETS: list = []
MCP_STATUS: list[dict] = []


# --- auth --------------------------------------------------------------------

def _token() -> str:
    return os.environ.get("ASTA_TOKEN", "")


def _token_ok(request: Request) -> bool:
    expected = _token()
    if not expected:
        return True
    auth = request.headers.get("authorization", "")
    supplied = auth.removeprefix("Bearer ").strip() if auth else (
        request.query_params.get("token") or request.cookies.get("asta_token") or ""
    )
    return secrets.compare_digest(supplied, expected)


def require_auth(request: Request) -> None:
    if not _token_ok(request):
        raise HTTPException(status_code=401, detail="Invalid or missing token")


@app.middleware("http")
async def _no_stale_ui(request: Request, call_next):
    """The UI shell must always revalidate — Chrome's heuristic caching otherwise
    serves week-old app.js after edits (no Cache-Control header = guess freshness)."""
    resp = await call_next(request)
    p = request.url.path
    if p == "/" or p.startswith("/ui"):
        resp.headers["Cache-Control"] = "no-cache"
    return resp


# --- lifecycle ---------------------------------------------------------------

@app.on_event("startup")
async def startup() -> None:
    global MCP_TOOLSETS, MCP_STATUS
    store.init()
    memory.ensure_dirs()
    memory.reindex()
    # A question whose waiter died with the process can never be answered, and a
    # stale one would swallow Arun's next message as its answer.
    asking.expire_stale()
    MCP_TOOLSETS, MCP_STATUS = mcp_loader.load_toolsets()
    asyncio.create_task(_probe_mcp())
    for name, ws in workspace.available_workspaces().items():
        # graph_pages() knows where this workspace keeps its context; the
        # directory name is per-workspace, so it must not be hardcoded here.
        pages = workspace.graph_pages(name)
        if pages:
            provider = workspace.provider_for(name)
            app.mount(
                f"/graph/{name}",
                StaticFiles(directory=provider.ctx / "graph"),
                name=f"graph-{name}",
            )
    asyncio.create_task(_digest_loop())
    asyncio.create_task(refresh.scheduler_loop())
    asyncio.create_task(_jira_watch_loop())
    if msnotify.enabled():
        asyncio.create_task(_msnotify_loop())
    if telegram.enabled():
        asyncio.create_task(telegram.poll_loop(_telegram_turn))
    if teams_bridge.enabled():
        asyncio.create_task(teams_bridge.session_watch_loop())
        if teams_bridge.ACTIVITY_POLL_SECONDS > 0:
            asyncio.create_task(teams_bridge.activity_watch_loop())
        asyncio.create_task(outlook.watch_loop())
        # Needs the calendar, so it only runs when the Teams/Outlook bridge is up.
        asyncio.create_task(briefing.premeeting_loop())
    asyncio.create_task(reminders.loop())
    asyncio.create_task(briefing.scheduler_loop())
    asyncio.create_task(health.loop())
    asyncio.create_task(ci_watch.loop())
    asyncio.create_task(tasks.resume_paused_loop())
    asyncio.create_task(notify.held_watch_loop())
    asyncio.create_task(attention.sweep_loop())
    asyncio.create_task(delivery.chase_loop())
    asyncio.create_task(delivery.flush_loop())
    asyncio.create_task(_learning_loop())
    # The WhatsApp bridge is a child of Asta's lifecycle now, not a manual step:
    # it starts with the server and is restarted on crash, so "no bridge" stops
    # being the default after every restart.
    if wa_bridge.enabled():
        asyncio.create_task(wa_bridge.supervise())


@app.on_event("shutdown")
async def _shutdown() -> None:
    # Stop only the bridge WE started; a hand-started or launchd bridge is left
    # running. Without this, a restart would orphan the Node child and the next
    # start would find the port taken.
    await wa_bridge.stop()


async def _probe_mcp() -> None:
    """Drop MCP servers that fail their handshake so one dead server can't stall chat."""
    global MCP_TOOLSETS
    healthy = []
    for prefixed in MCP_TOOLSETS:
        toolset = prefixed
        while not hasattr(toolset, "list_tools") and hasattr(toolset, "wrapped"):
            toolset = toolset.wrapped
        deferred = toolset is not prefixed.wrapped
        entry = next((s for s in MCP_STATUS if s["name"] == toolset.id), None)
        try:
            async with asyncio.timeout(20):
                async with toolset:
                    tools = await toolset.list_tools()
            healthy.append(prefixed)
            if entry:
                entry["reason"] = f"{len(tools)} tools" + (" (deferred)" if deferred else "")
        except Exception as exc:
            if entry:
                entry["enabled"] = False
                entry["reason"] = f"handshake failed: {str(exc)[:120]}"
    MCP_TOOLSETS = healthy


async def _digest_loop() -> None:
    """Every 10 min, turn idle conversations into episode digests (local model, free),
    then retire the CLI session that produced them."""
    while True:
        await asyncio.sleep(600)
        try:
            for conv in store.stale_undigested_conversations(idle_seconds=1800):
                await asyncio.to_thread(memory.write_episode, conv)
                rotate_sessions(conv["id"])
        except Exception:
            pass


async def _learning_loop() -> None:
    """Once a day, actually learn something — evolve recurring waste into skills
    and drop the ones the evidence says are not helping.

    Both halves already existed but only ever ran at the end of a background task,
    so a week of chat, CI investigations and corrections moved nothing. Hanging it
    off the clock instead means the archive improves on the days he never
    delegates anything, which is most of them.

    Quiet by design: it reports through the same ambient path as everything else
    that is not addressed to him, and says nothing at all on a day it changed
    nothing.
    """
    await asyncio.sleep(300)          # let startup settle before doing housekeeping
    while True:
        try:
            line = await learn.daily_pass()
            if line:
                await notify.notify(line, "learning", urgency="ambient")
        except Exception:
            pass
        await asyncio.sleep(86400)


def rotate_sessions(conv_id: str) -> list[str]:
    """Drop the CLI sessions behind a conversation so the next message starts clean.

    Safe precisely here: the digest has just extracted the durable knowledge into
    memory/episodes, and recall_block() resurfaces it on any later turn — so the
    raw session holds nothing worth its context cost. Without this, WhatsApp and
    Telegram (one permanent conversation each) accumulated every message ever
    sent into a single, ever-growing session.
    """
    tool_index.forget(conv_id)
    dropped = []
    for key in (f"copilot_session:{conv_id}", f"claude_session:{conv_id}"):
        if (store.kv_get(key) or "").strip():
            store.kv_set(key, "")
            dropped.append(key.split(":")[0])
    return dropped


# Phone channels have no "new chat" button, so this is the way to get a clean slate.
_FRESH_START = re.compile(
    r"^\s*(new chat|fresh start|start over|reset( chat| context)?|clear context|forget (all|everything))\s*[.!]*\s*$",
    re.I)


async def _jira_watch_loop() -> None:
    """Every 5 min, poll Jira for changes on the watch JQL and notify."""
    while True:
        await asyncio.sleep(300)
        try:
            for line in await jira.check_for_changes():
                await notify.notify(line, "jira")
        except Exception:
            pass


async def _msnotify_loop() -> None:
    """Poll macOS Notification Center for Teams/Outlook mentions (TEAMS_WATCHER=1)."""
    while True:
        await asyncio.sleep(msnotify.POLL_SECONDS)
        try:
            for line in await asyncio.to_thread(msnotify.check):
                await notify.notify(f"You were mentioned: {line}", "mention")
        except Exception:
            pass


# --- REST API ----------------------------------------------------------------

@app.get("/api/status", dependencies=[Depends(require_auth)])
def api_status():
    return {
        "name": agent_mod.assistant_name(),
        "models": agent_mod.model_registry(),
        "mcp": MCP_STATUS,
        "workspaces": workspace_tools.available_workspaces(),
        "usage": store.usage_summary(),
        "memories": len(memory.list_memories()),
        "teams_watcher": msnotify.status(),
        "telegram": telegram.status(),
        "teams_bridge": teams_bridge.status(),
        "wa_bridge": wa_bridge.status(),
        "ci_watch": ci_watch.status(),
        "reminders_pending": len(store.list_reminders()),
        "health_problems": json.loads(store.kv_get("health_problems") or "[]"),
    }


@app.get("/api/conversations", dependencies=[Depends(require_auth)])
def api_conversations():
    return store.list_conversations()


@app.get("/api/conversations/{conv_id}/messages", dependencies=[Depends(require_auth)])
def api_messages(conv_id: str):
    if not store.get_conversation(conv_id):
        raise HTTPException(404)
    return store.list_ui_messages(conv_id)


@app.delete("/api/conversations/{conv_id}", dependencies=[Depends(require_auth)])
def api_delete_conversation(conv_id: str):
    store.delete_conversation(conv_id)
    return {"ok": True}


@app.get("/api/memory", dependencies=[Depends(require_auth)])
def api_memory():
    return {"index": memory.index_text(), "items": memory.list_memories()}


@app.get("/api/memory/file", dependencies=[Depends(require_auth)])
def api_memory_file(path: str):
    text = memory.read_memory_file(path)
    if text is None:
        raise HTTPException(404)
    return {"path": path, "content": text}


# These four capabilities had no endpoint, so a CLI brain (copilot/claude) could
# not reach them — "remember this" silently did nothing there while the API/local
# brains could. Giving them HTTP closes that parity gap: every brain now has the
# same reachable capability set. Each delegates to the SAME agent function the
# in-process tool calls, so there is one behaviour, not two.

@app.post("/api/memory", dependencies=[Depends(require_auth)])
def api_remember(body: dict):
    title = (body or {}).get("title", "").strip()
    fact = (body or {}).get("fact", "").strip()
    if not title or not fact:
        raise HTTPException(400, "title and fact are required")
    return {"result": agent_mod.remember(title, fact, (body or {}).get("kind", "fact"))}


@app.get("/api/memory/search", dependencies=[Depends(require_auth)])
def api_search_memory(q: str):
    if not q.strip():
        raise HTTPException(400, "q is required")
    return {"result": agent_mod.search_memory(q)}


@app.get("/api/skills/{name}", dependencies=[Depends(require_auth)])
def api_load_skill(name: str):
    return {"result": agent_mod.load_skill(name)}


@app.get("/api/workspaces/{name}/services", dependencies=[Depends(require_auth)])
def api_list_services(name: str):
    return {"result": agent_mod.list_services(name)}


@app.post("/api/_invoke", dependencies=[Depends(require_auth)])
async def api_invoke(body: dict):
    """Run one capability BY NAME, in this (the live server) process.

    This is the seam that lets a CLI brain use native MCP tool calls instead of
    curling the API: Asta's MCP server forwards each tool call here, so the
    function runs where the server's in-process state lives. That is the whole
    point — `delegate_task` spawns its worker on THIS event loop, and `ask_user`
    waits on a future THIS process will resolve. Run the same functions in the MCP
    subprocess and both silently break (the worker dies with the subprocess, the
    answer never reaches the orphaned future).

    Generic on purpose: one endpoint, every capability, no per-tool wiring and no
    parsing of the human-readable `http` specs. It is not new authority — the
    bearer token already reaches every endpoint these functions back.
    """
    import inspect
    name = (body or {}).get("tool", "")
    args = (body or {}).get("args") or {}
    cap = capabilities.get(name)
    if cap is None or cap.fn is None:
        raise HTTPException(404, f"no capability '{name}'")
    if not isinstance(args, dict):
        raise HTTPException(400, "args must be an object")
    try:
        result = cap.fn(**args)
        if inspect.isawaitable(result):
            result = await result
    except TypeError as exc:
        raise HTTPException(400, f"bad arguments for {name}: {exc}")
    return {"result": result}


@app.get("/api/graphs/{workspace}", dependencies=[Depends(require_auth)])
def api_graphs(workspace: str):
    return workspace_tools.graph_pages(workspace)


# --- workspaces --------------------------------------------------------------
# Registry-backed, so adding a codebase is a UI action rather than a code edit.
# The project context these produce stays on this machine, inside the user's own
# repos — Asta only records where to look.

@app.get("/api/workspaces", dependencies=[Depends(require_auth)])
def api_workspaces():
    return workspace.available_workspaces()


@app.post("/api/workspaces/detect", dependencies=[Depends(require_auth)])
async def api_workspaces_detect(request: Request):
    body = await request.json()
    info = workspace.detect((body.get("path") or "").strip())
    if not info["ok"]:
        raise HTTPException(400, info["error"])
    return info


@app.post("/api/workspaces", dependencies=[Depends(require_auth)])
async def api_workspaces_add(request: Request):
    body = await request.json()
    try:
        ws = workspace.add(
            (body.get("name") or "").strip(),
            (body.get("root") or "").strip(),
            repos=[r for r in (body.get("repos") or []) if r],
            jira_projects=[j for j in (body.get("jira_projects") or []) if j],
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    return {"name": ws.name, "status": workspace.available_workspaces().get(ws.name, {})}


@app.delete("/api/workspaces/{name}", dependencies=[Depends(require_auth)])
def api_workspaces_remove(name: str):
    if not workspace.remove(name):
        raise HTTPException(404, f"No workspace '{name}'")
    return {"removed": name}


@app.post("/api/workspaces/{name}/provision", dependencies=[Depends(require_auth)])
async def api_workspaces_provision(name: str):
    """Regenerate the derived indexes only. Cheap and deterministic."""
    try:
        return {"report": await workspace.provision(name)}
    except ValueError as exc:
        raise HTTPException(404, str(exc))


@app.get("/api/workspaces/{name}/context-plan", dependencies=[Depends(require_auth)])
def api_context_plan(name: str):
    """What a context build would do. The UI shows this before spending."""
    plan = context_build.plan(name)
    if not plan["ok"]:
        raise HTTPException(404, plan["error"])
    plan["in_progress"] = context_build.in_progress(name)
    return plan


@app.post("/api/workspaces/{name}/build-context", dependencies=[Depends(require_auth)])
async def api_build_context(name: str, request: Request):
    """Run the expensive pass that CREATES project context. Explicit by design:
    it reads whole repositories on a code executor and costs real tokens, so it
    is never a side effect of registering a workspace."""
    body = {}
    with contextlib.suppress(Exception):
        body = await request.json()
    if context_build.in_progress(name):
        raise HTTPException(409, "A context build is already running for this workspace.")
    plan = context_build.plan(name)
    if not plan["ok"]:
        raise HTTPException(404, plan["error"])
    repos = [r for r in (body.get("repos") or []) if r] or None
    # Long job: run detached and notify, so the HTTP call does not hang.
    asyncio.create_task(context_build.build(name, repos, body.get("executor", "")))
    return {"started": True, "workspace": name,
            "repos": repos or plan["repos_to_build"],
            "note": "Running in the background; you will be notified when it finishes."}


@app.get("/api/workspaces/{name}/resolve", dependencies=[Depends(require_auth)])
async def api_workspace_resolve(name: str, q: str):
    """The resolver, over HTTP — the CLI brains reach the same capability the
    chat agent has, instead of being told to grep a repo."""
    try:
        return {"workspace": name, "question": q,
                "context": await workspace.resolve_context(name, q)}
    except ValueError as exc:
        raise HTTPException(404, str(exc))


@app.get("/api/workspaces/{name}/file", dependencies=[Depends(require_auth)])
def api_workspace_file(name: str, path: str, start_line: int = 1, end_line: int = 0):
    try:
        return {"workspace": name, "path": path,
                "content": workspace.read_workspace_file(name, path, start_line, end_line)}
    except ValueError as exc:
        raise HTTPException(404, str(exc))


# --- ask_user ----------------------------------------------------------------

@app.get("/api/ask", dependencies=[Depends(require_auth)])
def api_open_questions():
    return asking.open_questions()


@app.post("/api/ask", dependencies=[Depends(require_auth)])
async def api_ask(request: Request):
    """Put one question to Arun and BLOCK until he answers.

    Long-poll on purpose: the caller is a worker that needs the answer to carry
    on, and the alternative — stopping the pipeline and restarting it later —
    is what this exists to avoid.
    """
    b = await request.json()
    question = (b.get("question") or "").strip()
    if not question:
        raise HTTPException(400, "question is required")
    timeout = float(b.get("timeout") or asking.DEFAULT_TIMEOUT)
    answer = await asking.ask(question, b.get("source", "cli"),
                              min(timeout, asking.DEFAULT_TIMEOUT))
    return {"answer": answer, "answered": answer != asking.NO_ANSWER}


@app.post("/api/ask/{qid}/answer", dependencies=[Depends(require_auth)])
async def api_answer(qid: int, request: Request):
    b = await request.json()
    if not asking.answer(qid, (b.get("text") or "").strip()):
        raise HTTPException(404, f"no open question #{qid}")
    return {"ok": True}


# The loop signals, reachable by a CLI brain over HTTP as well as by the
# in-process tools. A CLI brain runs in its own subprocess with no turn context,
# so it passes conv_id explicitly; the in-process tool reads it from the turn.
@app.post("/api/loop/continue", dependencies=[Depends(require_auth)])
async def api_loop_continue(request: Request):
    b = await request.json()
    cid = b.get("conv_id") or tasks.current_conversation()
    if not cid:
        raise HTTPException(400, "no conversation to continue")
    loop.set_continue(cid, (b.get("next_step") or "").strip())
    return {"ok": True}


@app.post("/api/loop/prepare-send", dependencies=[Depends(require_auth)])
async def api_loop_prepare_send(request: Request):
    b = await request.json()
    cid = b.get("conv_id") or tasks.current_conversation()
    if not cid:
        raise HTTPException(400, "no conversation")
    loop.set_pending_send(cid, b.get("what") or "", b.get("to") or "",
                          b.get("channel") or "chat", to_group=bool(b.get("to_group")))
    return {"ok": True}


@app.get("/api/missions", dependencies=[Depends(require_auth)])
def api_missions():
    """Legacy mission history, read-only.

    Missions were the first of two engines that both did plan → approve →
    implement → verify → ship. Background tasks are now the only engine; these
    rows are kept so old work stays visible, but nothing new is created here.
    """
    return store.list_missions()


@app.get("/api/missions/{mission_id}", dependencies=[Depends(require_auth)])
def api_mission(mission_id: int):
    m = store.get_mission(mission_id)
    if not m:
        raise HTTPException(404)
    return m


@app.get("/api/activity", dependencies=[Depends(require_auth)])
def api_activity():
    """Live picture of what Asta is doing. Plain HTTP, so it answers even while
    the WebSocket is busy with a long turn."""
    return {**activity.snapshot(), "summary": activity.summary()}


@app.get("/api/presence", dependencies=[Depends(require_auth)])
async def api_presence():
    from . import presence
    return await presence.state()


@app.get("/api/voice/status", dependencies=[Depends(require_auth)])
async def api_voice_status():
    from . import voice
    return await voice.status()


@app.post("/api/voice/tts", dependencies=[Depends(require_auth)])
async def api_voice_tts(body: dict):
    """Text → speech via the local Voicebox. 503 tells the UI to use browser voice."""
    from . import voice
    text = (body or {}).get("text", "").strip()
    if not text:
        raise HTTPException(400, "text is required")
    try:
        audio = await voice.speak(text, profile=(body or {}).get("profile", ""),
                                  engine=(body or {}).get("engine", ""))
    except Exception as e:
        raise HTTPException(503, str(e))
    return Response(content=audio, media_type="audio/wav")


@app.post("/api/voice/stt", dependencies=[Depends(require_auth)])
async def api_voice_stt(file: UploadFile = File(...)):
    """Recorded clip → text via local Whisper."""
    from . import voice
    try:
        return {"text": await voice.transcribe(await file.read(),
                                               filename=file.filename or "speech.webm")}
    except Exception as e:
        raise HTTPException(503, str(e))


@app.get("/api/jira/search", dependencies=[Depends(require_auth)])
async def api_jira_search(jql: str, limit: int = 15):
    try:
        return await jira.search(jql, limit=limit)
    except RuntimeError as e:
        raise HTTPException(400, str(e))
    except httpx.HTTPStatusError as e:
        raise HTTPException(e.response.status_code, f"Jira: {e.response.status_code} for {e.request.url.path}")


@app.get("/api/jira/issue/{key}", dependencies=[Depends(require_auth)])
async def api_jira_issue(key: str, comments: int = jira.COMMENT_LIMIT):
    try:
        return await jira.get_issue(key, comment_limit=max(1, comments))
    except RuntimeError as e:
        raise HTTPException(400, str(e))
    except httpx.HTTPStatusError as e:
        raise HTTPException(e.response.status_code, f"Jira: {e.response.status_code} for {e.request.url.path}")


@app.post("/api/jira/issue/{key}/comment", dependencies=[Depends(require_auth)])
async def api_jira_comment(key: str, request: Request):
    """Stage a comment for Arun's yes. Deliberately the SAME function the chat tool
    calls: a CLI brain reaching this by curl must not get a write path that the
    in-process brain doesn't have. One policy, or the rule only holds where
    someone remembered to write it."""
    b = await request.json()
    if not b.get("text"):
        raise HTTPException(400, "text is required")
    before = offers.pending()
    return _staged(await agent_mod.jira_comment(key, b["text"]), before)


@app.get("/api/jira/issue/{key}/transitions", dependencies=[Depends(require_auth)])
async def api_jira_transitions(key: str):
    try:
        return {"key": key, "transitions": await jira.list_transitions(key)}
    except RuntimeError as e:
        raise HTTPException(400, str(e))
    except httpx.HTTPStatusError as e:
        raise HTTPException(e.response.status_code, f"Jira: {e.response.status_code} for {e.request.url.path}")


@app.post("/api/jira/issue/{key}/transition", dependencies=[Depends(require_auth)])
async def api_jira_transition(key: str, request: Request):
    """Stage a status change for Arun's yes — same function as the chat tool."""
    b = await request.json()
    if not b.get("status"):
        raise HTTPException(400, "status is required")
    before = offers.pending()
    return _staged(await agent_mod.jira_transition(key, b["status"]), before)


@app.get("/api/jira/sprint", dependencies=[Depends(require_auth)])
async def api_jira_sprint(limit: int = 30):
    try:
        return {"issues": await jira.current_sprint(limit)}
    except RuntimeError as e:
        raise HTTPException(400, str(e))
    except httpx.HTTPStatusError as e:
        raise HTTPException(e.response.status_code, f"Jira: {e.response.status_code} for {e.request.url.path}")


@app.post("/api/pr-review", dependencies=[Depends(require_auth)])
async def api_pr_review(request: Request):
    """Stage a review on a PR for Arun's yes. Never posts."""
    b = await request.json()
    if not b.get("pr") or not b.get("action"):
        raise HTTPException(400, "pr and action are required")
    before = offers.pending()
    return _staged(await agent_mod.pr_review_post(
        str(b["pr"]), b["action"], b.get("body", ""),
        b.get("workspace", ""), b.get("repo", "")), before)


def _staged(message: str, before) -> dict:
    """Report whether something is REALLY waiting on Arun, not that we tried.

    These endpoints used to hardcode staged:true and put the outcome in the
    message — so a refusal ("time must be YYYY-MM-DD HH:MM, got 'thursday'") came
    back flagged as staged. A brain resolves that contradiction by believing the
    flag, tells Arun an invite is waiting for his yes, and there is nothing there.
    The flag is derived from the offer actually changing.
    """
    now = offers.pending()
    return {"staged": now is not None and (before is None or now.id != before.id),
            "message": message}


@app.post("/api/propose-next", dependencies=[Depends(require_auth)])
async def api_propose_next(request: Request):
    """A CLI brain offering Arun its next step — the same offer the chat tool makes."""
    b = await request.json()
    if not (b.get("next_step") or "").strip():
        raise HTTPException(400, "next_step is required")
    before = offers.pending()
    message = agent_mod.propose_next(b["next_step"], b.get("why", ""))
    return {"offered": _staged(message, before)["staged"], "message": message}


@app.get("/api/teams/presence", dependencies=[Depends(require_auth)])
async def api_teams_presence():
    return {"message": await agent_mod.teams_status()}


@app.post("/api/teams/presence", dependencies=[Depends(require_auth)])
async def api_set_teams_presence(request: Request):
    b = await request.json()
    if not b.get("status"):
        raise HTTPException(400, "status is required")
    return {"message": await agent_mod.teams_status(b["status"])}


@app.post("/api/meetings", dependencies=[Depends(require_auth)])
async def api_create_meeting(request: Request):
    """Stage a meeting invite for Arun's yes. Never sends."""
    b = await request.json()
    if not b.get("subject") or not b.get("when"):
        raise HTTPException(400, "subject and when are required")
    before = offers.pending()
    return _staged(await agent_mod.create_meeting(
        b["subject"], b["when"], int(b.get("minutes", 30)),
        b.get("attendees", ""), b.get("agenda", "")), before)


@app.post("/api/leave", dependencies=[Depends(require_auth)])
async def api_request_leave(request: Request):
    """Stage an all-day leave invite for Arun's yes. Never sends."""
    b = await request.json()
    if not b.get("start_date"):
        raise HTTPException(400, "start_date is required")
    before = offers.pending()
    return _staged(await agent_mod.request_leave(
        b["start_date"], b.get("end_date", ""), b.get("reason", ""), b.get("to", "")), before)


@app.post("/api/meetings/join", dependencies=[Depends(require_auth)])
async def api_join_meeting(request: Request):
    b = await request.json()
    # Either a link he pasted or a meeting he named. The named form is the one
    # that was missing: `join()` always needed a URL and nothing produced one.
    if b.get("which"):
        return {"message": await agent_mod.join_meeting_by_name(b["which"])}
    if not b.get("join_url"):
        raise HTTPException(400, "join_url or which is required")
    return {"message": await agent_mod.join_meeting(b["join_url"], b.get("title", ""))}


@app.post("/api/meetings/leave", dependencies=[Depends(require_auth)])
async def api_leave_meeting():
    return {"message": await agent_mod.leave_meeting()}


@app.get("/api/meetings/notes", dependencies=[Depends(require_auth)])
async def api_meeting_notes():
    """Captions captured from the call Asta sat in on — same function the chat tool
    calls, so a CLI brain gets the identical transcript and the identical caveats."""
    return {"notes": await agent_mod.meeting_notes()}


@app.post("/api/meetings/say", dependencies=[Depends(require_auth)])
async def api_say_in_call(request: Request):
    b = await request.json()
    if not b.get("text"):
        raise HTTPException(400, "text is required")
    return {"message": await agent_mod.say_in_call(b["text"])}


@app.post("/api/ci/watch", dependencies=[Depends(require_auth)])
async def api_ci_watch(request: Request):
    b = await request.json()
    if not b.get("what"):
        raise HTTPException(400, "what is required")
    return {"message": agent_mod.watch_ci(b["what"], b.get("repo", ""))}


@app.get("/api/tasks", dependencies=[Depends(require_auth)])
def api_tasks():
    return store.list_tasks()


@app.get("/api/token-audit", dependencies=[Depends(require_auth)])
def api_token_audit(hours: float = 24, task: int = 0):
    from . import token_audit
    if task:
        rep = token_audit.audit_task(task)
        if not rep:
            raise HTTPException(404, f"no worker session found for task #{task}")
        return rep
    return {**token_audit.audit_recent(hours), "trend": token_audit.trend_series()}


@app.get("/api/meeting-prep", dependencies=[Depends(require_auth)])
async def api_meeting_prep(title: str = ""):
    from . import agent as agent_mod
    return {"prep": await agent_mod.meeting_prep(title)}


@app.post("/api/meeting-recap", dependencies=[Depends(require_auth)])
async def api_meeting_recap(body: dict):
    from . import agent as agent_mod
    return {"recap": await agent_mod.meeting_recap(
        (body or {}).get("transcript", ""), (body or {}).get("title", ""))}


@app.get("/api/teams-draft", dependencies=[Depends(require_auth)])
async def api_teams_draft(chat: str, question: str = ""):
    from . import agent as agent_mod
    return {"draft": await agent_mod.draft_teams_reply(chat, question)}


@app.post("/api/tasks", dependencies=[Depends(require_auth)])
async def api_create_task(request: Request):
    b = await request.json()
    if not b.get("title") or not b.get("prompt"):
        raise HTTPException(400, "title and prompt are required")
    try:
        return tasks.spawn(b["title"], b["prompt"], b.get("kind", "analysis"),
                           b.get("workspace") or None, b.get("teams_chat", ""),
                           b.get("executor", ""), b.get("context_from"),
                           b.get("pipeline", ""))
    except ValueError as e:
        raise HTTPException(400, str(e))


@app.get("/api/tasks/{task_id}", dependencies=[Depends(require_auth)])
def api_task(task_id: int):
    t = store.get_task(task_id)
    if not t:
        raise HTTPException(404)
    return t


@app.post("/api/tasks/{task_id}/approve", dependencies=[Depends(require_auth)])
async def api_approve_task(task_id: int):
    try:
        return {"ok": True, "detail": await tasks.approve(task_id)}
    except ValueError as e:
        raise HTTPException(400, str(e))


@app.post("/api/tasks/{task_id}/reply", dependencies=[Depends(require_auth)])
async def api_reply_task(task_id: int, request: Request):
    b = await request.json()
    if not b.get("text"):
        raise HTTPException(400, "text is required")
    try:
        return {"ok": True, "detail": tasks.reply(task_id, b["text"])}
    except ValueError as e:
        raise HTTPException(400, str(e))


@app.post("/api/tasks/{task_id}/ship", dependencies=[Depends(require_auth)])
async def api_ship_task(task_id: int):
    try:
        return {"ok": True, "detail": await tasks.ship(task_id)}
    except (ValueError, RuntimeError) as e:
        raise HTTPException(400, str(e))


@app.post("/api/tasks/{task_id}/reject", dependencies=[Depends(require_auth)])
async def api_reject_task(task_id: int):
    try:
        return {"ok": True, "detail": await tasks.reject(task_id)}
    except ValueError as e:
        raise HTTPException(400, str(e))


@app.post("/api/tasks/{task_id}/resume", dependencies=[Depends(require_auth)])
async def api_resume_task(task_id: int, request: Request):
    b = await request.json() if request.headers.get("content-length") else {}
    try:
        return {"ok": True, "detail": await tasks.resume_task(task_id, b.get("switch_to", ""))}
    except ValueError as e:
        raise HTTPException(400, str(e))


@app.get("/api/reminders", dependencies=[Depends(require_auth)])
def api_reminders(all: bool = False):
    return store.list_reminders(pending_only=not all)


@app.post("/api/reminders", dependencies=[Depends(require_auth)])
async def api_create_reminder(request: Request):
    b = await request.json()
    try:
        return reminders.create(b.get("text", ""), b.get("due", ""), b.get("repeat", ""))
    except ValueError as e:
        raise HTTPException(400, str(e))


@app.post("/api/reminders/{reminder_id}/cancel", dependencies=[Depends(require_auth)])
def api_cancel_reminder(reminder_id: int):
    try:
        reminders.cancel(reminder_id)
        return {"ok": True}
    except ValueError as e:
        raise HTTPException(400, str(e))


@app.post("/api/brief/now", dependencies=[Depends(require_auth)])
async def api_brief_now():
    text = await briefing.morning_brief()
    await notify.notify(text, "brief")
    return {"text": text}


@app.post("/api/standup/now", dependencies=[Depends(require_auth)])
async def api_standup_now():
    text = await briefing.standup_draft()
    await notify.notify("🧍 Standup draft:\n\n" + text, "standup")
    return {"text": text}


@app.get("/api/health", dependencies=[Depends(require_auth)])
async def api_health():
    problems = await health.run_check(notify_transitions=False)
    return {"ok": not problems, "problems": problems, "report": health.report_text(problems)}


@app.get("/api/ci", dependencies=[Depends(require_auth)])
async def api_ci():
    return {"status": ci_watch.status(), "recent": await ci_watch.recent_runs()}


@app.get("/api/notifications", dependencies=[Depends(require_auth)])
def api_notifications():
    return {"items": store.list_notifications(), "unseen": len(store.list_notifications(unseen_only=True))}


@app.post("/api/notifications/seen", dependencies=[Depends(require_auth)])
def api_notifications_seen():
    store.mark_notifications_seen()
    return {"ok": True}


@app.post("/api/refresh/{workspace}", dependencies=[Depends(require_auth)])
async def api_refresh(workspace: str):
    if workspace not in workspace_tools.WORKSPACES:
        raise HTTPException(404)
    return {"summary": await refresh.refresh_workspace(workspace, reason="manual (UI)")}


@app.post("/api/review", dependencies=[Depends(require_auth)])
async def api_review(request: Request):
    """Review a PR — the same capability the chat agent has, for the CLI brains."""
    b = await request.json()
    if not b.get("pr") or not b.get("workspace"):
        raise HTTPException(400, "pr and workspace are required")
    return {"result": await agent_mod.review_pr(str(b["pr"]), b["workspace"],
                                                b.get("repo", ""))}


@app.get("/api/quality", dependencies=[Depends(require_auth)])
def api_quality(days: int = 7):
    return quality.summary(days)


@app.get("/api/traces", dependencies=[Depends(require_auth)])
def api_traces(limit: int = 30):
    return {"summary": store.trace_summary(), "recent": store.list_traces(limit)}


@app.get("/api/wa/status", dependencies=[Depends(require_auth)])
async def api_wa_status():
    return await notify.wa_status()


@app.post("/api/wa/create-group", dependencies=[Depends(require_auth)])
async def api_wa_create_group():
    """Create the dedicated assistant group chat on WhatsApp and lock the bridge to it."""
    result = await notify.wa_create_group(agent_mod.assistant_name())
    if result is None:
        raise HTTPException(503, "WhatsApp bridge is not running")
    return result


@app.post("/api/wa/config", dependencies=[Depends(require_auth)])
async def api_wa_config(request: Request):
    """Forward enable/disable + allowed-JID changes to the bridge (persisted there)."""
    body = await request.json()
    result = await notify.wa_config(
        {k: body[k] for k in ("enabled", "allowed_jid") if k in body}
    )
    if result is None:
        raise HTTPException(503, "WhatsApp bridge is not running")
    return result


def _channel_model(conv: dict) -> str:
    """Which brain answers on a phone channel.

    Both channels used to pin this to default_chat_model() on every message,
    which quietly overrode any choice he had made — so "use claude" on WhatsApp
    would appear to work and then answer on Copilot anyway. His choice wins; the
    default only fills in when there is none, or when the one he picked has since
    stopped being available (a key removed, LM Studio closed), because failing the
    message is worse than answering it on something that works.
    """
    picked = (conv.get("model") or "").strip()
    if picked and agent_mod.model_registry().get(picked, {}).get("available"):
        return picked
    return agent_mod.default_chat_model()


@app.post("/api/wa/incoming", dependencies=[Depends(require_auth)])
async def api_wa_incoming(request: Request):
    """The WhatsApp bridge posts user messages here; reply goes back to the same chat."""
    text = (await request.json()).get("text", "").strip()
    if not text:
        return {"reply": ""}
    conv_id = store.kv_get("wa_conversation")
    conv = store.get_conversation(conv_id) if conv_id else None
    if conv is None:
        conv = store.create_conversation(model=agent_mod.default_chat_model(), workspace=None)
        store.update_conversation(conv["id"], title="WhatsApp")
        store.kv_set("wa_conversation", conv["id"])
    conv["model"] = _channel_model(conv)
    # The bridge uses this HTTP reply as the answer, so a quick turn comes back
    # in-band. A long one would hold the request past the bridge's timeout and
    # look like a dead channel — so past the deadline we ack and push the answer.
    # Mid-turn steering still works: a follow-up arrives as its own request.
    sink = HybridSink(notify.wa_send, conv["id"])
    job = await _dispatch(conv, text, sink, "whatsapp")
    if job is not None:
        done, _pending = await asyncio.wait({job}, timeout=WA_INBAND_TIMEOUT)
        if not done:
            sink.handoff()
            return {"reply": "⏳ on it — I'll send the answer here in a moment."}
    return {"reply": (sink.text() or "")[:3500]}


async def _telegram_turn(text: str) -> str:
    """Telegram poll loop hands user text here; reply goes back to the bound chat."""
    conv_id = store.kv_get("telegram_conversation")
    conv = store.get_conversation(conv_id) if conv_id else None
    if conv is None:
        conv = store.create_conversation(model=agent_mod.default_chat_model(), workspace=None)
        store.update_conversation(conv["id"], title="Telegram")
        store.kv_set("telegram_conversation", conv["id"])
    conv["model"] = _channel_model(conv)
    # Fire through the shared conductor; the reply (and any augment/redirect ack)
    # is pushed to the Telegram chat by the sink, so we return "" here and let the
    # poll loop keep reading — that's what lets a follow-up land mid-turn.
    sink = PushSink(telegram.send, conv["id"])
    await _dispatch(conv, text, sink, "telegram")
    return ""


@app.post("/api/login")
async def api_login(request: Request):
    body = await request.json()
    supplied = body.get("token", "")
    if _token() and not secrets.compare_digest(supplied, _token()):
        raise HTTPException(401, "Wrong token")
    resp = JSONResponse({"ok": True})
    resp.set_cookie("asta_token", supplied, max_age=90 * 86400, samesite="lax")
    return resp


# --- chat over WebSocket -----------------------------------------------------

def _load_history(conv: dict) -> list:
    return list(ModelMessagesTypeAdapter.validate_json(conv["history"] or "[]"))


def _with_recall(user_text: str) -> str:
    recall = memory.recall_block(user_text)
    if not recall:
        return user_text
    return f"{user_text}\n\n<memory-recall>\n{recall}\n</memory-recall>"


def _trim_history(messages: list) -> list:
    """Keep the recent tail, starting on a ModelRequest so the transcript stays valid."""
    tail = messages[-memory.KEEP_RECENT_MSGS:]
    while tail and not isinstance(tail[0], ModelRequest):
        tail.pop(0)
    return tail or messages[-2:]


def _is_quota_error(exc: Exception) -> bool:
    """Claude/OpenAI ran dry (or key invalid) — the signal to hand the turn to Copilot."""
    status = getattr(exc, "status_code", None)
    if status in (401, 402, 403, 429, 529):
        return True
    text = str(exc).lower()
    return any(s in text for s in ("credit balance", "quota", "rate limit", "billing",
                                   "overloaded", "api key", "api_key",
                                   # Claude Code CLI wording for subscription caps
                                   "usage limit", "session limit", "too many requests",
                                   "limit reached", "limit exceeded"))


class Emitter:
    """Streams turn events to the browser without letting a dead socket kill the turn.

    Before this, every send was awaited directly on the WebSocket, so a reload or
    a dropped connection raised mid-turn and the reply was never persisted — the
    model had already done (and billed) the work. Now the socket is best-effort:
    if it goes away we keep computing and still write the answer to the
    conversation, so it's waiting when you come back.

    Every event carries its conversation_id so the UI can tell concurrent
    conversations apart.
    """

    def __init__(self, ws: WebSocket, conv_id: str) -> None:
        self.ws, self.conv_id, self.alive = ws, conv_id, True

    async def send(self, payload: dict) -> None:
        if not self.alive:
            return
        try:
            await self.ws.send_json({**payload, "conversation_id": self.conv_id})
        except Exception:
            self.alive = False

    async def close(self) -> None:
        """Nothing buffered — the socket got every event as it happened."""
        return


# How long the WhatsApp bridge's request may wait for an in-band answer before we
# ack and switch to pushing. Below any sane bridge/proxy timeout.
WA_INBAND_TIMEOUT = float(os.environ.get("ASTA_WA_INBAND_TIMEOUT", "20"))


class HybridSink:
    """Sink for the WhatsApp bridge, which POSTs a message and uses our HTTP reply
    as the answer.

    Quick turns answer in-band — that's the contract the bridge depends on, and
    pushing instead leaves the chat silent. But a long agentic turn would hold
    that request open past the bridge's timeout, which looks exactly like the
    channel being broken. So past a deadline we hand off: ack immediately, then
    deliver the real answer through the bridge's /send.
    """

    def __init__(self, send, conv_id: str) -> None:
        self._send = send                # async (text: str) -> bool
        self.conv_id = conv_id
        self.alive = True
        self._parts: list[str] = []
        self._pushing = False

    async def send(self, payload: dict) -> None:
        typ = payload.get("type")
        if typ == "done":
            if self._pushing:
                out = self.text()
                if out:
                    with contextlib.suppress(Exception):
                        await self._send(out[:4000])
            return
        if typ == "delta":
            self._parts.append(payload.get("text", ""))
        elif typ == "note":
            self._parts.append("\n" + payload.get("text", "") + "\n")
        elif typ == "error":
            self._parts.append(f"\n⚠️ {payload.get('message', 'error')}\n")

    def text(self) -> str:
        out = "".join(self._parts).strip()
        self._parts.clear()
        return out

    def handoff(self) -> None:
        """Turn ran long — everything from here goes out via push instead."""
        self._pushing = True

    async def close(self) -> None:
        """Flush whatever is buffered, even though no "done" ever arrived.

        This is the fix for three hours of silence. A turn that FAILS never sends
        "done" — it sends {"type": "error"} and returns — and a turn that pauses,
        or stages a send on a phone channel, returns without one too. All three
        buffered their text here and then dropped it on the floor, so the answer
        to "⏳ on it" was nothing, forever, with no way to tell a dead brain from
        a slow one.

        Delivery cannot depend on the happy path being taken. Whatever is left
        when the turn ends goes out.
        """
        if not self._pushing:
            return          # still in-band: the HTTP reply carries it
        out = self.text()
        if out:
            with contextlib.suppress(Exception):
                await self._send(out[:4000])


class PushSink:
    """Sink for headless channels (Telegram/WhatsApp). Same interface as Emitter,
    so ONE turn-runner drives every channel — but instead of streaming to a
    socket it buffers the reply and delivers it in a single push per turn via the
    channel's own send function. Notes (the '✚ adding…' / '⏹ stopped…' acks) and
    errors are pushed as they happen.
    """

    def __init__(self, send, conv_id: str) -> None:
        self._send = send            # async (text: str) -> bool
        self.conv_id = conv_id
        self.alive = True
        self._buf: list[str] = []

    async def send(self, payload: dict) -> None:
        typ = payload.get("type")
        if typ == "delta":
            self._buf.append(payload.get("text", ""))
        elif typ == "note":
            with contextlib.suppress(Exception):
                await self._send(payload.get("text", ""))
        elif typ == "error":
            with contextlib.suppress(Exception):
                await self._send(f"⚠️ {payload.get('message', 'error')}")
        elif typ == "done":
            text = "".join(self._buf).strip()
            self._buf.clear()
            if text:
                with contextlib.suppress(Exception):
                    await self._send(text[:4000])

    async def close(self) -> None:
        """Same guarantee as HybridSink: a turn that ends without "done" still
        delivers what it produced. Notes and errors already went out as they
        happened here, so this only catches half-streamed deltas."""
        await self.send({"type": "done"})


# One turn at a time per conversation (a Copilot session can't be resumed twice
# at once), but different conversations run in parallel.
_turn_locks: dict[str, asyncio.Lock] = {}


def _turn_lock(conv_id: str) -> asyncio.Lock:
    if conv_id not in _turn_locks:
        _turn_locks[conv_id] = asyncio.Lock()
    return _turn_locks[conv_id]


async def _learn_correction(conv_id: str, user_text: str) -> None:
    """Write the lesson off the reply path — the local model call takes seconds
    and a correction must never make the answer to it slower."""
    with contextlib.suppress(Exception):
        slug = await asyncio.to_thread(memory.learn_from_correction, conv_id, user_text)
        if slug:
            store.kv_set("last_lesson", slug)


async def _run_turn_cli(out, conv: dict, user_text: str, cli, via: str,
                        note: str | None = None, channel: str = "web") -> None:
    """Stream a chat turn through any CLI brain (Copilot, Claude Code, …).

    They all bill a subscription Arun already has rather than a metered API key,
    and expose the same run_turn(conv, text, on_delta) contract — so one path
    serves every one of them; only the module and the trace label differ.
    """
    tool_name = via if via.endswith("_cli") else f"{via}_cli"
    if note:
        await out.send({"type": "note", "text": note})
    await out.send({"type": "tool", "status": "start", "name": tool_name, "args": ""})
    parts: list[str] = []
    t0 = time.monotonic()
    first_token_ms: int | None = None

    async def on_delta(text: str) -> None:
        nonlocal first_token_ms
        if first_token_ms is None:
            first_token_ms = int((time.monotonic() - t0) * 1000)
        parts.append(text)
        await out.send({"type": "delta", "text": text})

    async def on_tool(name: str) -> None:
        await out.send({"type": "tool", "status": "start", "name": name, "args": ""})

    # Some CLIs emit structured per-tool events (Claude Code's stream-json),
    # others just stream stdout. Ask the function itself rather than the name,
    # so a CLI added later gets tool activity for free if it supports it.
    import inspect
    accepts = inspect.signature(cli.run_turn).parameters
    kwargs = {}
    if "on_tool" in accepts:
        kwargs["on_tool"] = on_tool

    # Same contract for usage. A turn is a whole agent loop, and a CLI may
    # report more than once (a dead session retried, for instance), so these
    # accumulate rather than overwrite.
    spent = llm_meter.Usage()

    def on_usage(u) -> None:
        nonlocal spent
        spent = spent + u

    if "on_usage" in accepts:
        kwargs["on_usage"] = on_usage

    try:
        reply = await cli.run_turn(conv, user_text, on_delta, **kwargs)
    except Exception as exc:
        # Whatever it managed to stream before dying is the only record of how far
        # it got — the CLI session itself is gone. Save it here, where `parts`
        # still exists, so a handoff can continue instead of starting over.
        if _is_quota_error(exc):
            resume.save(conv["id"], user_text, via, "".join(parts), channel)
        raise
    await out.send({"type": "tool", "status": "done", "name": tool_name})
    # A CLI that reports usage only after the turn (Copilot's session snapshot,
    # vs Claude's live on_usage stream) exposes last_turn_usage. Same
    # attribute-not-name contract as on_tool/on_usage: a brain added later gets
    # real numbers for free the day it grows this.
    if not spent.measured and hasattr(cli, "last_turn_usage"):
        with contextlib.suppress(Exception):
            spent = spent + cli.last_turn_usage(conv, len(reply))
    # A brain that reports nothing still must not read as free — estimate it and
    # flag it, so it stays visible without polluting measured comparisons.
    if not spent.measured:
        spent = spent + llm_meter.estimated(len(user_text), len(reply))
    store.add_trace(conv["id"], via, channel, first_token_ms,
                    int((time.monotonic() - t0) * 1000),
                    spent.input, spent.output, spent.cache_read,
                    0, len(user_text), [tool_name],
                    cache_write_tokens=spent.cache_write, cost_usd=spent.cost_usd,
                    measured=spent.measured)
    store.add_usage(conv["id"], via, spent.input, spent.output,
                    spent.cache_read, spent.cache_write)
    store.add_ui_message(conv["id"], "assistant", reply,
                         {"tools": [tool_name], "via": via, "channel": channel})
    await out.send({"type": "done", "tools": [tool_name]})


async def _run_turn_copilot(out, conv: dict, user_text: str,
                            note: str | None = None, channel: str = "web") -> None:
    await _run_turn_cli(out, conv, user_text, copilot_cli, "copilot", note, channel)


# Why a model ran out differs, and the message should say which — Copilot's is a
# monthly credit pool, Claude's a rolling five-hour window. Where that fact is
# RECORDED lives in agent.py, so the picker and the fallback read one table: this
# used to write a key nothing else consulted, which is why a dead Copilot went on
# being handed every new conversation.
_QUOTA_WORDING = {"copilot": "Copilot quota exhausted",
                  "claude_cli": "Claude subscription limit hit"}


def _fallback_chain(failed: str) -> list[str]:
    """Who can take over a dried-up turn, best first — the order is the trait
    table's `rank` (subscription CLIs → free local → metered API keys), so adding
    a brain to the chain is a spec value, not a code edit here. A brain is a
    candidate only if it is installed/keyed AND not itself known to be out of
    quota — the old fallback checked availability but not quota, so a dead Copilot
    got handed the turn a Claude had just failed on, and it died too."""
    return [brain for brain in agent_mod.fallback_order()
            if brain != failed
            and agent_mod.available(brain) and not agent_mod.quota_down(brain)]


async def _cli_fallback(out, conv: dict, user_text: str, failed: str, channel: str) -> None:
    """A brain ran dry mid-turn — hand the turn on, and keep handing it on until
    one brain finishes or none is left. Claude's window closes → Copilot; Copilot
    also out → the local LM Studio model; and so on down the chain.

    What is handed over is the CHECKPOINT, not the original message, so each brain
    continues rather than re-deriving what the last one already established. If a
    fallback ALSO runs dry it is marked down and the next one is tried — a second
    dead brain used to surface as a hard error. When the chain is exhausted the
    checkpoint deliberately survives, so "resume"/a manual switch picks it up
    later from any channel.
    """
    why = _QUOTA_WORDING.get(failed, f"{failed} unavailable")
    agent_mod.mark_quota_down(failed)
    point = resume.get(conv["id"]) or resume.save(conv["id"], user_text, failed,
                                                  channel=channel, why=why)
    point["why"] = why          # the CLI saved the checkpoint; only here knows the wording
    for brain in _fallback_chain(failed):
        try:
            if agent_mod.is_cli(brain):
                await _run_turn_cli(out, conv, resume.handoff_prompt(point, brain),
                                    agent_mod.runner(brain), brain,
                                    note=resume.note(point, brain), channel=channel)
            else:
                await out.send({"type": "note", "text": resume.note(point, brain)})
                await _run_turn_streaming(out, conv, resume.handoff_prompt(point, brain),
                                          brain, channel)
            resume.clear(conv["id"])      # taken and finished — consume the checkpoint
            return
        except Exception as exc:
            if not _is_quota_error(exc):
                raise
            # This fallback ran dry too. Mark it, refresh the checkpoint (a CLI
            # re-saves its own partial), and try the next brain instead of erroring.
            agent_mod.mark_quota_down(brain)
            why = _QUOTA_WORDING.get(brain, f"{brain} unavailable")
            point = resume.get(conv["id"]) or point
            point["why"] = why
    # Nothing could take it. The checkpoint survives for a later resume/switch.
    await out.send({"type": "note", "text": resume.parked_note(point)})


async def _run_turn(out, conv: dict, user_text: str, channel: str = "web") -> None:
    model_name = conv["model"]
    # Link any task the agent spawns this turn back to this conversation, so a
    # later follow-up here can augment or redirect it.
    tasks.bind_conversation(conv["id"])
    # Remember what he actually said, so the relevance gate can catch a passive
    # question that tries to spawn work (a question is not a request to go do it).
    relevance.bind_trigger(user_text)
    # Capture BEFORE this message is stored — the learner needs the assistant's
    # previous reply, which is currently the last row.
    if memory.looks_like_correction(user_text):
        asyncio.create_task(_learn_correction(conv["id"], user_text))
    store.add_ui_message(conv["id"], "user", user_text, {"channel": channel})
    if conv["title"] == "New chat":
        store.update_conversation(conv["id"], title=user_text[:60])

    # "what's the update?" is answerable from our own state. Sending it to the
    # brain resumed a session carrying hundreds of tool runs and billed a full
    # agentic turn to say "still working".
    if activity.is_status_ask(user_text):
        reply = await asyncio.to_thread(activity.summary)
        await out.send({"type": "delta", "text": reply})
        store.add_ui_message(conv["id"], "assistant", reply, {"via": "local-status", "channel": channel})
        await out.send({"type": "done", "tools": []})
        return

    # A pure pleasantry ("hi", "thanks", "great") answered on the free local brain —
    # never spawn a paid CLI (~24k tokens) to say you're welcome. Real content, and
    # anything longer than a few words, falls straight through to the picked brain.
    if router.enabled() and router.is_trivial(user_text):
        r = await router.reply(user_text)
        await out.send({"type": "delta", "text": r})
        store.add_ui_message(conv["id"], "assistant", r, {"via": "local-router", "channel": channel})
        await out.send({"type": "done", "tools": []})
        return

    # Any CLI-backed model (Copilot, Claude Code, and anything added to the
    # spec table later) runs through the same path — the table says which module
    # drives it, so no new branch is needed per model.
    if agent_mod.is_cli(model_name):
        try:
            await _run_turn_cli(out, conv, user_text, agent_mod.runner(model_name),
                                model_name, channel=channel)
        except Exception as exc:
            if not _is_quota_error(exc):
                raise
            await _cli_fallback(out, conv, user_text, model_name, channel)
        return

    try:
        await _run_turn_streaming(out, conv, user_text, model_name, channel)
    except Exception as exc:
        store.add_trace(conv["id"], model_name, channel, None, 0, 0, 0, 0, 0,
                        len(user_text), [], error=str(exc))
        if _is_quota_error(exc):
            # Same handoff policy as the CLI brains — one function decides who
            # takes over and what they are told, so an API model running dry
            # behaves identically to a CLI one running dry.
            await _cli_fallback(out, conv, user_text, model_name, channel)
            return
        raise


async def _run_turn_streaming(out, conv: dict, user_text: str, model_name: str,
                              channel: str = "web") -> None:
    model = agent_mod.get_model(model_name)
    history = _load_history(conv)
    # Recall rides in the user prompt (not instructions) so the instruction
    # prefix stays byte-stable and the Anthropic prompt cache actually hits.
    prompt = _with_recall(user_text)
    # Only the tools this conversation has needed, not all 32 — see tool_index
    # for why the selection is sticky rather than per-message.
    selected = tool_index.select_sticky(conv["id"], user_text)
    turn_agent = AGENT if selected is None else agent_mod.build_agent(selected)
    instructions = agent_mod.build_instructions(conv["summary"], "", conv["workspace"],
                                                channel, selected)
    assistant_text = ""
    tools_used: list[str] = []
    t0 = time.monotonic()
    first_token_ms: int | None = None
    trace_usage = {"input": 0, "output": 0, "cached": 0, "cache_write": 0}

    async with turn_agent.run_stream_events(
        prompt,
        model=model,
        message_history=history or None,
        instructions=instructions,
        model_settings=agent_mod.model_settings(model_name),
        toolsets=MCP_TOOLSETS or None,
    ) as stream:
        async for event in stream:
            if isinstance(event, PartStartEvent) and isinstance(event.part, TextPart):
                if first_token_ms is None:
                    first_token_ms = int((time.monotonic() - t0) * 1000)
                assistant_text += event.part.content
                await out.send({"type": "delta", "text": event.part.content})
            elif isinstance(event, PartDeltaEvent) and isinstance(event.delta, TextPartDelta):
                if first_token_ms is None:
                    first_token_ms = int((time.monotonic() - t0) * 1000)
                assistant_text += event.delta.content_delta
                await out.send({"type": "delta", "text": event.delta.content_delta})
            elif isinstance(event, FunctionToolCallEvent):
                tools_used.append(event.part.tool_name)
                await out.send({
                    "type": "tool", "status": "start",
                    "name": event.part.tool_name,
                    "args": str(event.part.args)[:300],
                })
            elif isinstance(event, FunctionToolResultEvent):
                await out.send({
                    "type": "tool", "status": "done",
                    "name": getattr(event.result, "tool_name", "") or "",
                })
            elif isinstance(event, AgentRunResultEvent):
                result = event.result
                usage = result.usage
                trace_usage = {
                    "input": getattr(usage, "input_tokens", 0) or 0,
                    "output": getattr(usage, "output_tokens", 0) or 0,
                    "cached": getattr(usage, "cache_read_tokens", 0) or 0,
                    # Cache writes bill at 1.25x and were being dropped here, so
                    # the one path that DID measure still under-reported its
                    # most expensive first turn.
                    "cache_write": getattr(usage, "cache_write_tokens", 0) or 0,
                }
                store.add_usage(conv["id"], model_name, trace_usage["input"],
                                trace_usage["output"], trace_usage["cached"],
                                trace_usage["cache_write"])
                all_msgs = result.all_messages()
                summary = conv["summary"]
                if len(all_msgs) > memory.KEEP_RECENT_MSGS * 2:
                    new_summary = await asyncio.to_thread(memory.compact_summary, conv)
                    if new_summary:
                        summary = new_summary
                        all_msgs = _trim_history(all_msgs)
                fields = {
                    "history": ModelMessagesTypeAdapter.dump_json(all_msgs).decode(),
                    "summary": summary,
                    "digested": 0,
                }
                store.update_conversation(conv["id"], **fields)

    store.add_trace(conv["id"], model_name, channel, first_token_ms,
                    int((time.monotonic() - t0) * 1000),
                    trace_usage["input"], trace_usage["output"], trace_usage["cached"],
                    len(instructions), len(prompt), tools_used,
                    cache_write_tokens=trace_usage["cache_write"],
                    measured=bool(trace_usage["input"] or trace_usage["output"]))
    store.add_ui_message(conv["id"], "assistant", assistant_text, {"tools": tools_used, "channel": channel})
    # Measure-only: did the answer address the question? Fire-and-forget so it never
    # delays delivery, and a no-op unless ASTA_RELEVANCE is on.
    if relevance.enabled():
        asyncio.create_task(relevance.judge_answer(user_text, assistant_text))
    await out.send({"type": "done", "tools": tools_used})


@app.websocket("/ws")
async def ws_chat(ws: WebSocket) -> None:
    expected = _token()
    supplied = ws.query_params.get("token", "") or ws.cookies.get("asta_token", "")
    if expected and not secrets.compare_digest(supplied, expected):
        await ws.close(code=4401)
        return
    await ws.accept()
    try:
        while True:
            raw = await ws.receive_text()
            msg = json.loads(raw)
            if msg.get("type") != "chat":
                continue
            conv_id = msg.get("conversation_id")
            conv = store.get_conversation(conv_id) if conv_id else None
            if conv is None:
                conv = store.create_conversation(
                    model=msg.get("model", "claude"),
                    workspace=msg.get("workspace") or None,
                )
                await ws.send_json({"type": "conv", "conversation": conv})
            elif msg.get("model") and msg["model"] != conv["model"]:
                store.update_conversation(conv["id"], model=msg["model"])
                conv["model"] = msg["model"]
            if msg.get("workspace") != conv.get("workspace"):
                store.update_conversation(conv["id"], workspace=msg.get("workspace") or None)
                conv["workspace"] = msg.get("workspace") or None
                # He picked it — an explicit anchor, so clear any inherited stamp.
                relevance.clear_inherited_workspace(conv["id"])
            # Fire and keep reading. Awaiting the turn here meant one socket
            # could only ever run one turn: a status question sat in a client-side
            # queue behind a five-minute analysis, and a disconnect mid-turn threw
            # the answer away. Turns now outlive both the read loop and the socket.
            asyncio.create_task(_dispatch(conv, msg.get("message", ""), Emitter(ws, conv["id"]), "web"))
    except WebSocketDisconnect:
        pass


# --- conversation conductor --------------------------------------------------
# One place, shared by every channel, that decides what a message MEANS relative
# to work already running for that conversation — a fresh turn, a free local
# status answer, an augment folded into the running work, or a redirect that
# stops it. Web streams through an Emitter; Telegram/WhatsApp push through a
# PushSink; the routing is identical.

# The turn currently answering each conversation, so a correction can stop it.
_inflight: dict[str, asyncio.Task] = {}
# Augments that arrived mid-turn, applied on top the moment the turn finishes.
_addenda: dict[str, list[str]] = {}
# Mid-turn messages that were NOT refinements — answered as their own turns next.
_followups: dict[str, list[str]] = {}


def _clear_inflight(job: asyncio.Task, cid: str) -> None:
    if _inflight.get(cid) is job:
        _inflight.pop(cid, None)


def _start_turn(conv: dict, user_text: str, sink, channel: str) -> asyncio.Task:
    job = asyncio.create_task(_conducted_turn(conv, user_text, sink, channel))
    _inflight[conv["id"]] = job
    job.add_done_callback(lambda j: _clear_inflight(j, conv["id"]))
    return job


async def _conducted_turn(conv0: dict, first_text: str, sink, channel: str) -> None:
    """Wrapper that guarantees delivery. See _conduct for the actual loop.

    Whatever happens in there — a raise, a pause, a staged send, a backstop
    firing — the sink is closed on the way out, so anything it buffered reaches
    the channel. Nothing about "did the turn succeed" may decide "did he hear
    back", because the case where he most needs to hear back is the failure."""
    try:
        await _conduct(conv0, first_text, sink, channel)
    finally:
        if hasattr(sink, "close"):
            with contextlib.suppress(Exception):
                await sink.close()


# A single turn may not exceed this, whatever it is doing. The CLI brains have
# their own 300s ceiling, but it covers only the part where output is being
# pumped — the Teams/Outlook pre-fetch before the process spawns, and the wait
# for it to exit afterwards, both sat outside every timeout. That is how one
# message went unanswered for three hours. This is the backstop: deliberately
# above the CLI's own limit, so the brain's clearer error normally wins and this
# only catches a hang nobody anticipated.
TURN_CEILING = float(os.environ.get("ASTA_TURN_CEILING", "420"))


async def _conduct(conv0: dict, first_text: str, sink, channel: str) -> None:
    """Run a turn, then fold in anything buffered while it ran — and, when nothing
    is buffered, drive the model's own next step instead of idling. The just-finished
    work is already in the persisted history, so each continuation builds on top with
    nothing re-sent and the cache prefix byte-stable.

    Order of precedence after every turn: a staged outward send interrupts to ask
    Arun first; then his own interjections (augments, follow-ups) always win; and only
    if he left nothing does the loop auto-continue, bounded by ASTA_LOOP_MAX_STEPS."""
    cid = conv0["id"]
    text = first_text
    loop.reset_steps(cid)                        # the step budget is per user message
    while True:
        conv = store.get_conversation(cid) or conv0
        conv["model"] = conv0["model"]          # honour the model picked for THIS message
        conv["workspace"] = conv0.get("workspace")
        async with _turn_lock(cid):
            try:
                await asyncio.wait_for(_run_turn(sink, conv, text, channel),
                                       timeout=TURN_CEILING)
            except asyncio.CancelledError:
                raise
            except (asyncio.TimeoutError, TimeoutError):
                loop.clear(cid)
                # Say it in the words the failure actually has for him: the brain
                # stopped answering, here is how to get moving again. A checkpoint
                # is saved so switching picks up rather than starting over.
                resume.save(cid, text, conv.get("model", "brain"), "", channel,
                            why=f"{conv.get('model', 'the brain')} stopped responding")
                with contextlib.suppress(Exception):
                    await sink.send({"type": "error", "message":
                                     f"{conv.get('model', 'That brain')} stopped responding after "
                                     f"{int(TURN_CEILING // 60)} min, so I gave up on it rather "
                                     f"than leave you waiting. Say “use claude cli” (or another "
                                     f"brain) and I'll carry on from here."})
            except Exception as exc:            # a tool/model failure must not kill the socket
                loop.clear(cid)                 # a failed turn ends the loop, doesn't spin
                with contextlib.suppress(Exception):
                    await sink.send({"type": "error", "message": f"{type(exc).__name__}: {exc}"})

        intent = loop.take(cid) if loop.enabled() else None

        # A drafted outward send is the one thing that interrupts everything: show it
        # and wait for his yes/no — nothing leaves the machine unconfirmed.
        if intent and intent["kind"] == "send":
            loop.stage(cid, intent)
            await _present_staged_send(sink, cid, intent, channel)
            return

        pending = _addenda.pop(cid, None)
        if pending:
            text = ("While you were working, add / adjust the following on top of what "
                    "you just did:\n" + "\n".join(f"- {p}" for p in pending))
            continue
        # Queued messages that weren't refinements run verbatim, one turn each,
        # so a question asked mid-turn still gets a real answer.
        queued = _followups.get(cid)
        if queued:
            text = queued.pop(0)
            if not queued:
                _followups.pop(cid, None)
            continue

        # Arun left nothing to do — so if the model said it wasn't finished, run its
        # next step itself rather than stopping and waiting for a message.
        if intent and intent["kind"] == "continue":
            if loop.budget_left(cid):
                loop.bump_steps(cid)
                step = intent["next_step"] or "continue the task"
                await sink.send({"type": "note", "text": f"🔁 {step}"})
                text = (f"Continue now — do not wait for me. The next step you named: {step}\n"
                        "Keep going until the whole task is done. If you draft anything to send "
                        "outside this chat, stage it with prepare_to_send instead of sending it.")
                continue
            # Say WHICH budget ran out. "Paused" with no reason reads like a bug;
            # "I've been at this 10 minutes" tells him whether to push or rethink.
            reason = (f"been at this {int(loop.elapsed(cid) // 60)} min"
                      if not loop.time_left(cid) else
                      f"auto-continued {loop.max_steps()} steps")
            await sink.send({"type": "note", "text":
                             f"⏸ Paused — {reason}, so it can't run away with your time. "
                             "Say “continue” to keep going."})
        return


async def _present_staged_send(sink, cid: str, intent: dict, channel: str) -> None:
    """Show Arun a drafted outward message and ask before it is sent. The draft is
    persisted as an assistant turn so it survives in history, and the loop waits:
    his next message is routed as the yes/no (see _dispatch)."""
    # A group is named as a group. The difference between a 1:1 and a fourteen-person
    # thread is the whole risk of the question being asked, and "to *Vinish*" and
    # "to *prod issue - triaging*" look identical when skimmed on a phone.
    where = "👥 GROUP " if intent.get("to_group") else ""
    to = f" to {where}*{intent['to']}*" if intent.get("to") else ""
    body = (f"📤 Ready to send{to} on **{intent.get('channel', 'chat')}** — can I send this?\n\n"
            f"———\n{intent.get('what', '')}\n———\n\n"
            "Reply “send” to confirm, or tell me what to change.")
    store.add_ui_message(cid, "assistant", body, {"via": "loop-confirm-send", "channel": channel})
    await sink.send({"type": "delta", "text": body})
    if channel == "web":
        await sink.send({"type": "done", "tools": []})


_ANSWER_CMD = re.compile(r"^\s*answer\s+#?(\d+)\s+(.+)$", re.I | re.S)
# A clear no to an offer — so "not now" closes it instead of being mistaken for
# the destination in the share-the-build step.
_DECLINE = re.compile(r"^\s*(no|nope|nah|not now|later|skip|ignore|drop it|leave it|"
                      r"don'?t|stop|👎|❌)\s*[.!]*\s*$", re.I)


def _offer_prompt(o, where: str = "") -> str:
    """Turn an accepted offer into the instruction that does the work.

    Each step ends by staging the NEXT offer, which is what makes the chain run:
    analyse → fix → share, with his yes between every pair.

    An offer that brought its own `action` uses it. That is what lets the loop
    cover work nobody wrote a branch for — implement this ticket, chase that
    review, update the status — instead of only the three CI steps below.
    """
    ctx = f"Context:\n{o.context}\n\n"
    if o.action:
        extra = f"\n\nHis answer: {where}" if where else ""
        return (f"{ctx}Arun approved this next step:\n{o.action}{extra}\n\n"
                f"Carry it out now — do not wait for further confirmation on the step "
                f"itself. If anything you produce needs to leave this chat, stage it with "
                f"prepare_to_send. When you are done, report in a few lines and, if there "
                f"is an obvious next step, offer it with propose_next rather than doing it.")
    if o.kind == "analyse":
        return (f"{ctx}Arun approved analysing this failure. Work it as a senior engineer on THIS "
                f"codebase, not a log reader:\n"
                f"1. Pull the failing job's log (gh run view) and find the exact failing "
                f"step / test / assertion — the real error, not the summary line.\n"
                f"2. Open the actual code and this project's context/conventions, and trace the "
                f"failure to its ROOT CAUSE in the code — the thing that, if changed, stops it. "
                f"Not the symptom in the log.\n"
                f"3. If you cannot be sure without reproducing it, SAY SO and write the smallest "
                f"failing test (or mock) that reproduces it — a confirmed repro is how we know "
                f"it's the real bug and not a guess. Tell Arun that's what you're doing.\n"
                f"4. Report in a few lines: what broke, the root cause (file:line), how it "
                f"reproduces, and the smallest correct fix that fits how this codebase already "
                f"does things.\n"
                f"Do NOT change production code yet. End by asking whether to fix it and raise the PR.")
    if o.kind == "raise_pr":
        return (f"{ctx}Arun approved the fix. Do it the way a careful engineer would:\n"
                f"1. On a branch, first make sure a test reproduces the bug and FAILS for the "
                f"right reason (write it if the analysis didn't already).\n"
                f"2. Make the smallest fix that follows this codebase's existing patterns.\n"
                f"3. Confirm that test now passes AND the wider suite is still green — if it "
                f"isn't, stop and report, don't paper over it.\n"
                f"4. Raise the PR from his personal account; title and body state the root cause "
                f"and the fix in plain terms.\n"
                f"Report the PR link in one line, then ask where he wants the build shared for approval.")
    if o.kind == "share_build":
        dest = where or "wherever he just named"
        return (f"{ctx}Arun wants the build shared with: {dest}. Draft the message announcing it "
                f"(include the PR link and what changed), then stage it with prepare_to_send — "
                f"do NOT send it yourself.")
    return f"{ctx}Arun approved: {o.prompt}. Carry it out, then report back in one line."
# A bare yes to "can I send this?" — anything else is treated as change-requests.
_AFFIRM = re.compile(r"^\s*(send( it)?|yes|yep|yeah|y|ok(ay)?( send| do it)?|go( ahead)?|"
                     r"confirm|do it|👍|✅)\s*[.!]*\s*$", re.I)


def _mechanical_send(staged: dict) -> dict | None:
    """The recorded call for an approved draft, or None if it needs a brain.

    Only Teams today. Email and PR bodies still go back through the model because
    nothing here composes them; chat has nowhere to send to. Returning None keeps
    those on exactly the path they were already on.
    """
    if (staged.get("channel") or "").strip().lower() != "teams":
        return None
    to, what = (staged.get("to") or "").strip(), (staged.get("what") or "").strip()
    if not to or not what:
        return None            # nothing to address it to — let the model sort it out
    return {"name": "teams_send", "args": {"to": to, "text": what,
                                           "to_group": bool(staged.get("to_group"))}}


async def _run_op(op: dict, cid: str, sink, channel: str) -> None:
    """Run one recorded outward call and report the outcome, success or failure."""
    try:
        line = await ops.run(op)
    except Exception as exc:
        line = f"⚠️ Couldn't do it — {type(exc).__name__}: {exc}"
    store.add_ui_message(cid, "assistant", line, {"via": "staged-send", "channel": channel})
    await sink.send({"type": "note", "text": line})
    if channel == "web":
        await sink.send({"type": "done", "tools": []})


async def _run_staged_op(o, cid: str, sink, channel: str) -> None:
    """Execute an approved outward write and say what happened, either way.

    A failure here is reported in full rather than swallowed. He said yes to a
    comment going onto a ticket; if it never landed, silence would leave him
    believing it had — and he would find out from the colleague who never got it.
    """
    try:
        line = await ops.run(o.op)
    except Exception as exc:
        line = f"⚠️ Couldn't do it — {type(exc).__name__}: {exc}"
    store.add_ui_message(cid, "assistant", line, {"via": "offer-op", "channel": channel})
    await sink.send({"type": "note", "text": line})
    if channel == "web":
        await sink.send({"type": "done", "tools": []})


# "use copilot", "switch to claude", "which model" — the model picker the web UI
# has as a dropdown. Phone channels had no equivalent, which meant the one place
# he actually reads a quota warning was the one place he could not act on it.
# Unambiguous phrasing: this IS a request to change brains, whatever it names —
# so an unknown name gets the list of real ones rather than being run as a
# message.
_MODEL_CMD = re.compile(r"^\s*(?:use|switch to|swap to|change to|model)\s+([\w.\- ]{2,40})\s*[.!]*\s*$", re.I)

# "Change the LLM model to Claude cli" matched none of the above — the verb had
# to be followed immediately by "to", and his was followed by "the LLM model". So
# the one message that could have rescued a dead brain was queued behind the dead
# brain. This absorbs whatever he calls it ("the model", "llm", "brain").
#
# It is deliberately only half a rule: it is loose enough to also match "change
# the ticket status to done", so a hit here counts ONLY when the name resolves to
# a brain that exists. Loose about how he says it, strict about what he names.
_MODEL_CMD_LOOSE = re.compile(
    r"^\s*(?:use|switch|swap|change|set)\s+"
    r"(?:the|my)?\s*(?:llm|ai|chat)?\s*(?:model|brain)\s*(?:to|=)?\s+"
    r"([\w.\- ]{2,40})\s*[.!]*\s*$", re.I)
_MODEL_ASK = re.compile(r"^\s*(which model|what model|models|list models|brains?)\s*\??\s*$", re.I)

# Deliberately NOT a bare "continue": that word already belongs to the conductor
# loop's pause, and stealing it would break the more common of the two.
_RESUME_CMD = re.compile(
    r"^\s*(resume|carry on|pick (it )?up|(continue|carry on|pick up) (from )?where you "
    r"(left off|stopped)|continue where you left off)\s*[.!]*\s*$", re.I)

# "resume task 53", "retry task 7", "continue task 12" — pick a paused delegated
# task back up. Handled here (not via a brain tool) so it works even when the
# chat brain is itself limited or has lost the task from its context.
_RESUME_TASK = re.compile(
    r"^\s*(?:resume|retry|restart|continue|carry on|pick (?:it )?up)\s+"
    r"(?:task\s*)?#?(\d{1,5})\b", re.I)
# "task 53 use copilot", "53 switch to claude" — resume, but on a different brain.
_TASK_SWITCH = re.compile(
    r"^\s*(?:task\s*)?#?(\d{1,5})\b.*?\b(?:use|switch to|run on)\s+([\w.\-]{2,30})\s*[.!]*\s*$",
    re.I)


# "what's pending", "anything waiting on me" — the missing half of a yes/no
# interface. Four separate mechanisms can own his next message (a staged send, an
# offer, an open question, a live task) and until now nothing could tell him
# which. A one-word answer to an invisible question is a coin flip.
_PENDING_ASK = re.compile(
    r"^\s*(what'?s? pending|pending|anything (waiting|pending)( on me)?|"
    r"what (are you |you )?waiting (on|for)|open (questions?|items?))\s*\??\s*[.!]*\s*$", re.I)


def _pending_summary(cid: str) -> str:
    """Everything currently waiting on him, in the order a reply would be routed.

    The order is not cosmetic — it IS the dispatch precedence below. Showing them
    in any other order would be a plausible-looking lie about what "yes" does.
    """
    lines: list[str] = []
    staged = loop.awaiting(cid)
    if staged:
        to = f" to {staged['to']}" if staged.get("to") else ""
        lines.append(f"1. 📤 A message{to} on {staged.get('channel', 'chat')} is drafted "
                     f"and waiting — “send” posts it.")
    o = offers.pending()
    if o:
        # What will RUN, not what was asked. The question was phrased for the
        # moment it was sent ("Implement it?"); hours later on a different channel
        # that sentence no longer says what "it" is.
        what = ops.describe(o.op) if o.mechanical() else (o.action or o.prompt)
        lines.append(f"{len(lines) + 1}. ▶ {o.subject} — “yes” means: {what}")
    q = asking.pending_for_reply()
    if q:
        lines.append(f"{len(lines) + 1}. ❓ {q['text'][:100]}")
    live = tasks.live_tasks_for(cid)
    for tid in live:
        title = (store.get_task(tid) or {}).get("title", "")[:45]
        lines.append(f"{len(lines) + 1}. ⚙️ task #{tid} running — {title}")
    if not lines:
        return "Nothing is waiting on you."
    return ("Waiting on you (a bare “yes” answers the first one):\n"
            + "\n".join(lines))


def _model_listing(current: str) -> str:
    """Every brain, whether it is up, and which one is answering right now."""
    lines = []
    for name, info in agent_mod.model_registry().items():
        mark = "→" if name == current else ("·" if info.get("available") else "×")
        state = "" if info.get("available") else f"  — {info.get('detail') or 'not configured'}"
        lines.append(f" {mark} {name}: {info.get('label') or name}{state}")
    return ("Brains:\n" + "\n".join(lines) +
            "\n\nSay “use <name>” to switch. The switch sticks for this chat.")


def _resolve_brain(wanted: str) -> str:
    """The registry name `wanted` refers to, or "" if it names no brain.

    Shares its resolution with _switch_model so "does this name a brain?" and
    "which brain is it?" can never disagree.
    """
    registry = agent_mod.model_registry()
    want = agent_mod.normalize_model((wanted or "").strip().lower().replace(" ", "_"))
    if want in registry:
        return want
    hit = [n for n in registry if want and (want in n or n in want)]
    return hit[0] if len(hit) == 1 else ""


def _model_request(text: str) -> str:
    """The brain name this message asks for, or "" if it isn't asking.

    Unambiguous phrasing counts even when the name is unknown — "use frobnicator"
    is still a request to change brains, and answering it with the list of real
    ones is more use than running it as a message. Loose phrasing has to name a
    brain that exists, or "change the ticket status to done" becomes a model
    switch. Both roads lead to _switch_model, which does the resolving.
    """
    m = _MODEL_CMD.match(text or "")
    if m:
        name = m.group(1).strip()
        # "use X" is only a brain request when X could BE a brain name. This
        # pattern predates the bug and quietly ate "use it for the standup notes"
        # — answered with "I don't have a brain called 'it for the standup
        # notes'" instead of doing the thing. A name nobody has is still worth
        # the list; a sentence is not a name.
        if _resolve_brain(name) or len(name.split()) <= 2:
            return name
        return ""
    m = _MODEL_CMD_LOOSE.match(text or "")
    if m and _resolve_brain(m.group(1)):
        return m.group(1).strip()
    return ""


def _switch_model(conv: dict, wanted: str) -> str:
    """Point this conversation at another brain. Returns what to tell him.

    Resolved through the shared registry rather than a hardcoded list, so a brain
    added to the spec table is switchable from his phone the same day — the whole
    point of not special-casing per model.
    """
    registry = agent_mod.model_registry()
    want = agent_mod.normalize_model(wanted.strip().lower().replace(" ", "_"))
    if want not in registry:
        hit = [n for n in registry if want in n or n in want]
        if len(hit) != 1:
            return (f"I don't have a brain called “{wanted.strip()}”.\n\n"
                    + _model_listing(conv.get("model", "")))
        want = hit[0]
    info = registry[want]
    store.update_conversation(conv["id"], model=want)
    conv["model"] = want
    if not info.get("available"):
        # Switched anyway: he may be about to add the key, and refusing a choice
        # he stated is more annoying than a warning he can ignore.
        return (f"Switched to {want}, but it isn't configured yet — "
                f"{info.get('label', want)} needs its key or CLI before it can answer.")
    return f"✅ Now using {info.get('label') or want} for this chat."


async def _dispatch(conv: dict, user_text: str, sink, channel: str = "web") -> asyncio.Task | None:
    """Route one incoming message. Called per message on every channel.

    Returns the turn it started, or None when the message was handled without
    starting one (status answer, augment folded in, task steered) — so a
    request/response channel knows whether there's anything to wait for."""
    cid = conv["id"]

    # "new chat" on a phone channel — the clean slate the web UI gets from its
    # button. Answered here so it never reaches a brain and costs a turn.
    if _FRESH_START.match(user_text or ""):
        dropped = rotate_sessions(cid)
        _addenda.pop(cid, None)
        _followups.pop(cid, None)
        loop.clear(cid)
        await sink.send({"type": "note", "text":
                         "🧹 Fresh start — new session from here. "
                         + ("Previous context is digested into memory, so anything "
                            "durable is still recalled." if dropped
                            else "Nothing to clear.")})
        if channel == "web":
            await sink.send({"type": "done", "tools": []})
        return None

    # Meta-commands, answered here on every channel. They deliberately run BEFORE
    # the yes/no routing below and leave any open question standing: picking a
    # different brain, or checking what he owes an answer to, is not an answer to
    # the question and must not be mistaken for changing the subject.
    if _MODEL_ASK.match(user_text or ""):
        await sink.send({"type": "note", "text": _model_listing(conv.get("model", ""))})
        if channel == "web":
            await sink.send({"type": "done", "tools": []})
        return None
    # Picking a paused/failed delegated task back up. Resolved BEFORE the
    # conversation-level "use <brain>" switch, so "task 53 use copilot" steers
    # that task rather than repointing the whole chat's brain.
    _mrt = _RESUME_TASK.match(user_text or "")
    _mts = None if _mrt else _TASK_SWITCH.match(user_text or "")
    if _mrt or _mts:
        _tid = int((_mrt or _mts).group(1))
        _rt = store.get_task(_tid)
        if _rt and _rt["kind"] == "code" and _rt["status"] in ("paused", "failed"):
            try:
                _detail = await tasks.resume_task(_tid, _mts.group(2).strip() if _mts else "")
            except ValueError as exc:
                _detail = str(exc)
            await sink.send({"type": "note", "text": "▶️ " + _detail})
            if channel == "web":
                await sink.send({"type": "done", "tools": []})
            return None
        # A number that names no paused task falls through — it may be a live task
        # to augment, or just an ordinary message.
    switch = _model_request(user_text or "")
    if switch:
        before = conv.get("model", "")
        await sink.send({"type": "note", "text": _switch_model(conv, switch)})
        # He switched because something ran dry — which is the whole reason he
        # asked for this. Carrying on is what he meant; making him then type
        # "resume" would be theatre.
        point = resume.get(cid) if conv.get("model") != before else None
        if point is not None:
            resume.clear(cid)
            await sink.send({"type": "note", "text":
                             f"↩️ Carrying on from where {point.get('brain', 'it')} stopped."})
            return _start_turn(conv, resume.handoff_prompt(point, conv["model"]),
                               sink, channel)
        if channel == "web":
            await sink.send({"type": "done", "tools": []})
        return None
    if _PENDING_ASK.match(user_text or ""):
        await sink.send({"type": "note", "text": _pending_summary(cid)})
        if channel == "web":
            await sink.send({"type": "done", "tools": []})
        return None
    # "resume" only means anything when something is actually parked. With no
    # checkpoint this falls THROUGH to the ordinary path rather than answering
    # "nothing to resume" — otherwise it would eat a perfectly normal "carry on"
    # aimed at the conductor loop's pause.
    if _RESUME_CMD.match(user_text or ""):
        point = resume.get(cid)
        if point is not None:
            resume.clear(cid)
            await sink.send({"type": "note", "text":
                             f"↩️ Picking up where {point.get('brain', 'it')} stopped "
                             f"{resume.age_minutes(point)} min ago, "
                             f"on {conv.get('model', 'this brain')}."})
            return _start_turn(conv, resume.handoff_prompt(point, conv.get("model", "")),
                               sink, channel)
        # No conversation checkpoint, but a delegated task parked on a usage limit
        # is exactly what a bare "resume" means when there's just one of them.
        paused = tasks.paused_tasks_for(cid)
        if len(paused) == 1:
            try:
                detail = await tasks.resume_task(paused[0])
            except ValueError as exc:
                detail = str(exc)
            await sink.send({"type": "note", "text": "▶️ " + detail})
            if channel == "web":
                await sink.send({"type": "done", "tools": []})
            return None

    # A drafted outward send is waiting on "can I send this?" — his next message is
    # the answer. A bare yes sends it (via the model's real send tool, so the send
    # itself still runs through that tool's own rules); anything else is revision.
    staged = loop.awaiting(cid)
    if staged and (user_text or "").strip():
        loop.clear_awaiting(cid)
        if _AFFIRM.match(user_text):
            # A Teams send runs as a recorded call, not as a prompt asking a brain
            # to perform the send it just described. Handing it back to the model
            # is what made "send it" unreliable: it could reword the message,
            # resolve a different person of that name, treat the tool call as
            # optional, or simply answer ABOUT sending — and every one of those
            # ends with Arun believing a message went out that never did.
            op = _mechanical_send(staged)
            if op:
                await _run_op(op, cid, sink, channel)
                return None
            prompt = (f"Arun approved sending this. Send it now using the right tool for "
                      f"channel '{staged.get('channel', 'chat')}'"
                      + (f" to {staged['to']}" if staged.get("to") else "")
                      + f":\n\n{staged.get('what', '')}\n\nAfter it's sent, confirm in one line.")
        else:
            prompt = (f"Arun did NOT approve sending the draft as-is. His feedback:\n"
                      f"{user_text.strip()}\n\nRevise accordingly. When it's ready to send "
                      f"again, stage it with prepare_to_send; otherwise just continue the work.")
        return _start_turn(conv, prompt, sink, channel)

    # Asta offered to go do something ("CI failed — want me to analyse?"). His yes
    # is what starts the work: nothing was investigated unasked, and nothing is
    # dropped either. Answerable from any channel, because the question went to
    # his phone and the answer usually comes back the same way.
    open_offer = offers.pending()
    if open_offer and (user_text or "").strip():
        if _AFFIRM.match(user_text):
            offers.accept()
            # An outward write was staged with its exact arguments. Run THAT,
            # rather than asking a brain to perform the thing it described — the
            # words he approved are the words that go out.
            if open_offer.mechanical():
                await _run_staged_op(open_offer, cid, sink, channel)
                return None
            # A CI-failure offer carries the workspace its repo lives in. Adopt it
            # for the conversation so the analysis — and the fix and PR that follow
            # from it — run against that project's code and context, not Asta's own
            # repo. Persisted so the later steps in the chain inherit it too.
            ws_name = (open_offer.payload or {}).get("workspace")
            if ws_name and conv.get("workspace") != ws_name:
                conv["workspace"] = ws_name
                with contextlib.suppress(Exception):
                    store.update_conversation(cid, workspace=ws_name)
                # Adopted silently, not named by Arun — mark it so a later spawn
                # into this repo that the ask never mentioned reads as drift.
                relevance.mark_inherited_workspace(cid, ws_name)
            return _start_turn(conv, _offer_prompt(open_offer), sink, channel)
        if _DECLINE.match(user_text):
            offers.decline()
            await sink.send({"type": "note", "text": "👍 Dropped it."})
            if channel == "web":
                await sink.send({"type": "done", "tools": []})
            return None
        # The share-the-build step ASKS for a destination, so his reply is the
        # answer rather than a change of subject.
        if open_offer.kind == "share_build":
            offers.accept()
            return _start_turn(conv, _offer_prompt(open_offer, where=user_text.strip()),
                               sink, channel)
        # Anything else: he moved on. Drop the offer instead of holding a stale
        # question that would misread a much later "yes".
        offers.clear()

    # An open ask_user question owns the next message. Explicit form first
    # ("answer 3 the second one"), then the bare reply — which is how a person
    # actually answers a question on their phone.
    m = _ANSWER_CMD.match(user_text or "")
    if m and asking.answer(int(m.group(1)), m.group(2).strip()):
        await sink.send({"type": "note", "text": f"✅ Answer delivered to question #{m.group(1)}."})
        if channel == "web":
            await sink.send({"type": "done", "tools": []})
        return None
    pending = asking.pending_for_reply()
    if pending and (user_text or "").strip():
        asking.answer(pending["id"], user_text.strip())
        await sink.send({"type": "note", "text":
                         f"✅ Passed that back to whatever asked: “{pending['text'][:80]}”"})
        if channel == "web":
            await sink.send({"type": "done", "tools": []})
        return None

    # A live background CODE task owns the conversation's attention: augment it
    # (delivered at its next gate — no session restart) or redirect it (cancel).
    live = tasks.live_tasks_for(cid)
    named = _named_task(user_text, live)
    if named is not None:
        if await _route_to_task(named, _strip_task_ref(user_text), sink, channel,
                                conv.get("model", "")):
            return None
    elif len(live) > 1:
        # Several tasks live and none named. Guessing (it used to take the
        # newest) silently steered the wrong one — on WhatsApp, where every
        # task shares one conversation, that is invisible until damage is done.
        listing = "\n".join(
            f"  #{i} {(store.get_task(i) or {}).get('title', '')[:45]}" for i in live)
        await sink.send({"type": "note", "text":
                         f"Which task do you mean?\n{listing}\n\n"
                         "Say the number — e.g. “14 also cover the amend path” or “stop 15”."})
        return None
    elif len(live) == 1:
        if await _route_to_task(live[0], user_text, sink, channel, conv.get("model", "")):
            return None
        # Not about the task — fall through and answer it as an ordinary message.

    prev = _inflight.get(cid)
    if prev is None or prev.done():
        return _start_turn(conv, user_text, sink, channel)

    intent = await activity.resolve_interjection(user_text, conv.get("model", ""))
    if intent == "status":
        summary = await asyncio.to_thread(activity.summary)
        if channel == "web":
            await sink.send({"type": "delta", "text": summary})
            await sink.send({"type": "done", "tools": []})
        else:
            await sink.send({"type": "note", "text": summary})
        return None
    if intent == "augment":
        _addenda.setdefault(cid, []).append(user_text)
        await sink.send({"type": "note",
                         "text": "✚ adding that to what I'm doing — same task, no restart."})
        return None
    if intent == "ambiguous":
        # Not clearly a refinement of the running work, so DON'T glue it on —
        # that both corrupts the instruction and eats the message. Answer it
        # next, as its own turn.
        _followups.setdefault(cid, []).append(user_text)
        await sink.send({"type": "note",
                         "text": "💬 still finishing the previous one — I'll answer this right after."})
        return None
    # redirect — the running work is wrong now; stop it and take this instead.
    prev.cancel()
    with contextlib.suppress(asyncio.CancelledError, Exception):
        await prev
    _addenda.pop(cid, None)
    if channel == "web":
        await sink.send({"type": "done", "conversation_id": cid, "tools": []})
    await sink.send({"type": "note", "text": "⏹ stopped what I was doing — taking this instead."})
    return _start_turn(conv, user_text, sink, channel)


# "14 also cover the amend path", "stop 15", "#14 …", "task 14 …" — the same
# shape as the `approve task 14` command Arun already uses.
_TASK_REF = re.compile(r"(?:^|\b)(?:task\s*)?#?(\d{1,5})\b")


def _named_task(text: str, live: list[int]) -> int | None:
    """The live task this message explicitly names, if any."""
    for m in _TASK_REF.finditer(text or ""):
        n = int(m.group(1))
        if n in live:
            return n
    return None


def _strip_task_ref(text: str) -> str:
    """Drop the id so the instruction reads naturally to the pipeline."""
    return _TASK_REF.sub("", text or "", count=1).strip(" ,:—-") or (text or "")


async def _route_to_task(task_id: int, user_text: str, sink, channel: str,
                         model_name: str = "") -> bool:
    """A follow-up arrived while a background code task is live. Fold it in at the
    task's next gate, or cancel the task if it's a redirect.

    False means "this wasn't about the task" — the caller answers it normally
    rather than burying an unrelated message in the task's instructions.
    """
    intent = await activity.resolve_interjection(user_text, model_name)
    if intent == "status":
        summary = await asyncio.to_thread(activity.summary)
        await sink.send({"type": "note" if channel != "web" else "delta", "text": summary})
        if channel == "web":
            await sink.send({"type": "done", "tools": []})
        return True
    if intent == "redirect":
        with contextlib.suppress(Exception):
            await tasks.cancel(task_id)
        store.kv_set(f"task_addenda:{task_id}", "")   # drop anything buffered for it
        await sink.send({"type": "note",
                         "text": f"⏹ stopped task #{task_id} — tell me the new direction and I'll start fresh."})
        return True
    if intent != "augment":
        return False
    # augment: buffer it; deliver at the next gate (no expensive session restart).
    note = tasks.augment(task_id, user_text)
    await sink.send({"type": "note", "text": note})
    return True


# --- UI ----------------------------------------------------------------------

@app.get("/")
def index():
    return FileResponse(UI_DIR / "index.html")


app.mount("/ui", StaticFiles(directory=UI_DIR), name="ui")


def main() -> None:
    import uvicorn
    uvicorn.run(
        "app.main:app",
        host=os.environ.get("ASTA_HOST", "127.0.0.1"),
        port=int(os.environ.get("ASTA_PORT", "8321")),
    )


if __name__ == "__main__":
    main()
