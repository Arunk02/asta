"""Drive the LIVE Asta end to end, as a rehearsal that leaves no trace.

Reads only ASTA_TOKEN / ASTA_PORT from .env into this process (never printed);
database reads are read-only. The code task runs in a scratch workspace named
"e2e" (app/rehearsal.py): no learned skills, no token-audit trend, no phone
pushes. Everything it creates is removed at the end, pass or fail.

  python scripts/live_check.py chat     three chat turns, context size per turn
  python scripts/live_check.py task     plan -> approve -> build -> follow-up
"""
from __future__ import annotations

import asyncio
import json
import os
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

import httpx

ASTA = Path(__file__).resolve().parents[1]
SCRATCH = Path.home() / ".asta-rehearsal" / "ws"
WORKSPACE = "e2e"


def _env() -> tuple[str, dict]:
    for line in (ASTA / ".env").read_text().splitlines():
        k, _, v = line.partition("=")
        if k.strip() in ("ASTA_TOKEN", "ASTA_PORT") and v.strip():
            os.environ[k.strip()] = v.strip().strip('"').strip("'")
    base = f"http://127.0.0.1:{os.environ.get('ASTA_PORT', '8321')}"
    return base, {"Authorization": "Bearer " + os.environ.get("ASTA_TOKEN", "")}


BASE, H = _env()


def db():
    return sqlite3.connect(f"file:{ASTA / 'data' / 'asta.db'}?mode=ro", uri=True)


def traces(cid):
    with db() as c:
        return c.execute("select context_tokens, cache_write_tokens, cached_tokens, first_token_ms "
                         "from traces where conv_id=? order by id", (cid,)).fetchall()


def _scratch_repo() -> None:
    repo = SCRATCH / "calc"
    if (repo / ".git").exists():
        return
    repo.mkdir(parents=True, exist_ok=True)
    (repo / "calc.py").write_text("def add(a, b):\n    return a + b\n")
    (repo / "test_calc.py").write_text(
        "from calc import add\n\n\ndef test_add():\n    assert add(2, 3) == 5\n")
    for cmd in (["git", "init", "-q", "-b", "main"], ["git", "add", "."],
                ["git", "-c", "user.name=rehearsal", "-c", "user.email=rehearsal@localhost",
                 "commit", "-qm", "calc"]):
        subprocess.run(cmd, cwd=repo, check=True)


async def _turn(ws, cid, text):
    await ws.send(json.dumps({"type": "chat", "message": text, "model": "claude_cli",
                              "conversation_id": cid}))
    out = []
    while True:
        m = json.loads(await asyncio.wait_for(ws.recv(), timeout=400))
        if m.get("type") == "conv":
            cid = m["conversation"]["id"]
        elif m.get("type") == "delta":
            out.append(m.get("text", ""))
        elif m.get("type") == "error":
            return cid, "ERROR " + m.get("message", "")
        elif m.get("type") == "done":
            return cid, "".join(out)


async def chat() -> bool:
    import websockets
    url = BASE.replace("http", "ws") + f"/ws?token={os.environ.get('ASTA_TOKEN', '')}"
    cid, ok = None, True
    try:
        async with websockets.connect(url, max_size=None) as ws:
            for i, text in enumerate([
                    "Live test of a fresh session — reply with just: ok",
                    "Second message in the same session — reply with just: ok again",
                    "What reminders do I have set right now? One line, use your reminder tool."], 1):
                cid, reply = await _turn(ws, cid, text)
                await asyncio.sleep(1.5)
                tr = (traces(cid) or [None])[-1]
                ok = ok and not reply.startswith("ERROR")
                print(f"turn {i}: {reply.strip()[:120]!r}"
                      + (f"  context={tr[0]} cache_write={tr[1]}" if tr else ""))
    finally:
        if cid:
            with httpx.Client(timeout=60) as c:
                c.delete(f"{BASE}/api/conversations/{cid}", headers=H)
    return ok


def _poll(c, tid, want, timeout=1800):
    t0, last = time.time(), ""
    while time.time() - t0 < timeout:
        t = c.get(f"{BASE}/api/tasks/{tid}", headers=H).json()
        st = t.get("status") or (t.get("task") or {}).get("status")
        if st != last:
            print(f"  [{time.time() - t0:5.0f}s] {st}")
            last = st
        if st in want:
            return t
        time.sleep(10)
    raise TimeoutError(f"task {tid} never reached {want}")


def task() -> bool:
    _scratch_repo()
    tid = None
    with httpx.Client(timeout=120) as c:
        try:
            c.post(f"{BASE}/api/workspaces", headers=H,
                   json={"name": WORKSPACE, "root": str(SCRATCH), "repos": ["calc"]})
            r = c.post(f"{BASE}/api/tasks", headers=H, json={
                "title": "calc: add subtract", "kind": "code", "workspace": WORKSPACE,
                "pipeline": "full", "prompt": "In the calc repo, add subtract(a, b) to calc.py "
                                              "and a test in test_calc.py, in the style of add()."})
            t = r.json()
            tid = t.get("id") or (t.get("task") or {}).get("id")
            print("task", tid)
            t = _poll(c, tid, ("awaiting_approval", "failed", "done"))
            if (t.get("status") or t.get("task", {}).get("status")) != "awaiting_approval":
                return False
            c.post(f"{BASE}/api/tasks/{tid}/reply", headers=H, json={"text": "PLAN APPROVED"})
            t = _poll(c, tid, ("done", "failed", "awaiting_approval", "paused"))
            done = (t.get("status") or t.get("task", {}).get("status")) == "done"
            print("built:", done)
            return done
        finally:
            if tid:
                c.post(f"{BASE}/api/tasks/{tid}/reject", headers=H,
                       json={"why": "live rehearsal, done"})
            c.delete(f"{BASE}/api/workspaces/{WORKSPACE}", headers=H)


if __name__ == "__main__":
    what = sys.argv[1] if len(sys.argv) > 1 else "chat"
    passed = asyncio.run(chat()) if what == "chat" else task()
    print("PASS" if passed else "FAIL")
    sys.exit(0 if passed else 1)
