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
from . import activity, briefing, ci_watch, claude_cli, context_build, copilot_cli, health, jira, mcp_loader, memory, missions, msnotify, notify, outlook, refresh, reminders, store, tasks, teams_bridge, telegram, workspace, workspace_tools

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
    asyncio.create_task(notify.held_watch_loop())


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


def rotate_sessions(conv_id: str) -> list[str]:
    """Drop the CLI sessions behind a conversation so the next message starts clean.

    Safe precisely here: the digest has just extracted the durable knowledge into
    memory/episodes, and recall_block() resurfaces it on any later turn — so the
    raw session holds nothing worth its context cost. Without this, WhatsApp and
    Telegram (one permanent conversation each) accumulated every message ever
    sent into a single, ever-growing session.
    """
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


@app.get("/api/missions", dependencies=[Depends(require_auth)])
def api_missions():
    return store.list_missions()


@app.post("/api/missions", dependencies=[Depends(require_auth)])
async def api_create_mission(request: Request):
    b = await request.json()
    if not b.get("title") or not b.get("workspace"):
        raise HTTPException(400, "title and workspace are required")
    return await missions.start(
        b["title"], b["workspace"], b.get("repo") or None,
        b.get("jira_key") or None, b.get("description", ""), b.get("executor") or None,
    )


@app.get("/api/missions/{mission_id}", dependencies=[Depends(require_auth)])
def api_mission(mission_id: int):
    m = store.get_mission(mission_id)
    if not m:
        raise HTTPException(404)
    m["log_tail"] = missions.log_tail(mission_id)
    return m


@app.post("/api/missions/{mission_id}/approve", dependencies=[Depends(require_auth)])
async def api_approve_mission(mission_id: int):
    try:
        return await missions.approve(mission_id)
    except ValueError as e:
        raise HTTPException(400, str(e))


@app.post("/api/missions/{mission_id}/reject", dependencies=[Depends(require_auth)])
async def api_reject_mission(mission_id: int):
    try:
        return await missions.reject(mission_id)
    except ValueError as e:
        raise HTTPException(400, str(e))


@app.post("/api/missions/{mission_id}/ship", dependencies=[Depends(require_auth)])
async def api_ship_mission(mission_id: int, body: dict | None = None):
    """Commit → push → PR → watch CI. Explicit action; never automatic."""
    try:
        return await missions.ship(mission_id, review_chat=(body or {}).get("review_chat", ""))
    except Exception as e:
        raise HTTPException(400, str(e))


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
async def api_jira_issue(key: str):
    try:
        return await jira.get_issue(key)
    except RuntimeError as e:
        raise HTTPException(400, str(e))
    except httpx.HTTPStatusError as e:
        raise HTTPException(e.response.status_code, f"Jira: {e.response.status_code} for {e.request.url.path}")


@app.post("/api/jira/issue/{key}/comment", dependencies=[Depends(require_auth)])
async def api_jira_comment(key: str, request: Request):
    b = await request.json()
    if not b.get("text"):
        raise HTTPException(400, "text is required")
    try:
        return await jira.add_comment(key, b["text"])
    except RuntimeError as e:
        raise HTTPException(400, str(e))
    except httpx.HTTPStatusError as e:
        raise HTTPException(e.response.status_code, f"Jira: {e.response.status_code} for {e.request.url.path}")


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
    b = await request.json()
    if not b.get("status"):
        raise HTTPException(400, "status is required")
    try:
        return await jira.transition_issue(key, b["status"])
    except RuntimeError as e:
        raise HTTPException(400, str(e))
    except httpx.HTTPStatusError as e:
        raise HTTPException(e.response.status_code, f"Jira: {e.response.status_code} for {e.request.url.path}")


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
    conv["model"] = agent_mod.default_chat_model()
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
    conv["model"] = agent_mod.default_chat_model()
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
                                   "usage limit", "limit reached", "limit exceeded"))


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
    kwargs = ({"on_tool": on_tool}
              if "on_tool" in inspect.signature(cli.run_turn).parameters else {})
    reply = await cli.run_turn(conv, user_text, on_delta, **kwargs)
    await out.send({"type": "tool", "status": "done", "name": tool_name})
    store.add_trace(conv["id"], via, channel, first_token_ms,
                    int((time.monotonic() - t0) * 1000), 0, 0, 0,
                    0, len(user_text), [tool_name])
    store.add_ui_message(conv["id"], "assistant", reply,
                         {"tools": [tool_name], "via": via, "channel": channel})
    await out.send({"type": "done", "tools": [tool_name]})


async def _run_turn_copilot(out, conv: dict, user_text: str,
                            note: str | None = None, channel: str = "web") -> None:
    await _run_turn_cli(out, conv, user_text, copilot_cli, "copilot", note, channel)


# Why a model ran out differs, and the message should say which — Copilot's is a
# monthly credit pool, Claude's a rolling five-hour window.
_QUOTA_KV = {"copilot": "copilot_quota_down", "claude_cli": "claude_quota_down"}
_QUOTA_WORDING = {"copilot": "Copilot quota exhausted",
                  "claude_cli": "Claude subscription limit hit"}


async def _cli_fallback(out, conv: dict, user_text: str, failed: str, channel: str) -> None:
    """A CLI model ran dry — hand the turn to another CLI, else an API/local brain.

    Any model can be picked at any time, so the fallback is chosen from what is
    actually up rather than a fixed pair.
    """
    why = _QUOTA_WORDING.get(failed, f"{failed} unavailable")
    store.kv_set(_QUOTA_KV.get(failed, f"{failed}_quota_down"), str(time.time()))
    for alt in agent_mod.EXECUTORS:
        if alt != failed and agent_mod.available(alt):
            await _run_turn_cli(out, conv, user_text, agent_mod.runner(alt), alt,
                                note=f"⚡ {why} — handing this turn to {alt}", channel=channel)
            return
    try:
        fb = agent_mod.best_model_name()
    except RuntimeError:
        await out.send({"type": "error", "message":
                        f"{why} and no fallback brain is up — start LM Studio "
                        "(free, local) or add an API key in .env."})
        return
    await out.send({"type": "note", "text": f"⚡ {why} — falling back to {fb}"})
    await _run_turn_streaming(out, conv, user_text, fb, channel)


async def _run_turn(out, conv: dict, user_text: str, channel: str = "web") -> None:
    model_name = conv["model"]
    # Link any task the agent spawns this turn back to this conversation, so a
    # later follow-up here can augment or redirect it.
    tasks.bind_conversation(conv["id"])
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
        if _is_quota_error(exc) and copilot_cli.available():
            await _run_turn_copilot(
                out, conv, user_text, channel=channel,
                note=f"⚡ {model_name} unavailable ({type(exc).__name__}) — handing this turn to Copilot CLI",
            )
            return
        raise


async def _run_turn_streaming(out, conv: dict, user_text: str, model_name: str,
                              channel: str = "web") -> None:
    model = agent_mod.get_model(model_name)
    history = _load_history(conv)
    # Recall rides in the user prompt (not instructions) so the instruction
    # prefix stays byte-stable and the Anthropic prompt cache actually hits.
    prompt = _with_recall(user_text)
    instructions = agent_mod.build_instructions(conv["summary"], "", conv["workspace"], channel)
    assistant_text = ""
    tools_used: list[str] = []
    t0 = time.monotonic()
    first_token_ms: int | None = None
    trace_usage = {"input": 0, "output": 0, "cached": 0}

    async with AGENT.run_stream_events(
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
                }
                store.add_usage(conv["id"], model_name, trace_usage["input"],
                                trace_usage["output"], trace_usage["cached"])
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
                    len(instructions), len(prompt), tools_used)
    store.add_ui_message(conv["id"], "assistant", assistant_text, {"tools": tools_used, "channel": channel})
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
    """Run a turn, then fold in anything buffered while it ran. The just-finished
    work is already in the persisted history, so the augment builds on top with
    nothing re-sent and the cache prefix byte-stable."""
    cid = conv0["id"]
    text = first_text
    while True:
        conv = store.get_conversation(cid) or conv0
        conv["model"] = conv0["model"]          # honour the model picked for THIS message
        conv["workspace"] = conv0.get("workspace")
        async with _turn_lock(cid):
            try:
                await _run_turn(sink, conv, text, channel)
            except asyncio.CancelledError:
                raise
            except Exception as exc:            # a tool/model failure must not kill the socket
                with contextlib.suppress(Exception):
                    await sink.send({"type": "error", "message": f"{type(exc).__name__}: {exc}"})
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
        return


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
        await sink.send({"type": "note", "text":
                         "🧹 Fresh start — new session from here. "
                         + ("Previous context is digested into memory, so anything "
                            "durable is still recalled." if dropped
                            else "Nothing to clear.")})
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
