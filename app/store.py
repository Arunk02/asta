"""SQLite persistence: conversations, UI messages, usage, and the memory FTS index."""

from __future__ import annotations

import json
import sqlite3
import time
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DB_PATH = ROOT / "data" / "asta.db"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS conversations (
    id TEXT PRIMARY KEY,
    title TEXT NOT NULL DEFAULT 'New chat',
    model TEXT NOT NULL DEFAULT 'claude',
    workspace TEXT,
    summary TEXT NOT NULL DEFAULT '',
    history TEXT NOT NULL DEFAULT '[]',
    digested INTEGER NOT NULL DEFAULT 0,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS ui_messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    conv_id TEXT NOT NULL,
    role TEXT NOT NULL,
    content TEXT NOT NULL,
    meta TEXT NOT NULL DEFAULT '{}',
    created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_ui_messages_conv ON ui_messages(conv_id);
CREATE TABLE IF NOT EXISTS usage (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    conv_id TEXT NOT NULL,
    model TEXT NOT NULL,
    input_tokens INTEGER NOT NULL DEFAULT 0,
    output_tokens INTEGER NOT NULL DEFAULT 0,
    cache_read_tokens INTEGER NOT NULL DEFAULT 0,
    created_at REAL NOT NULL
);
CREATE VIRTUAL TABLE IF NOT EXISTS memory_fts USING fts5(
    path, title, mtype, body, date UNINDEXED
);
CREATE TABLE IF NOT EXISTS missions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    title TEXT NOT NULL,
    jira_key TEXT,
    workspace TEXT NOT NULL,
    repo TEXT,
    description TEXT NOT NULL DEFAULT '',
    plan TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'planning',
    executor TEXT NOT NULL DEFAULT 'copilot',
    log_path TEXT,
    error TEXT NOT NULL DEFAULT '',
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS tasks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    title TEXT NOT NULL,
    kind TEXT NOT NULL DEFAULT 'analysis',
    prompt TEXT NOT NULL,
    workspace TEXT,
    teams_chat TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'running',
    result TEXT NOT NULL DEFAULT '',
    error TEXT NOT NULL DEFAULT '',
    created_at REAL NOT NULL,
    started_at REAL,
    finished_at REAL
);
CREATE TABLE IF NOT EXISTS reminders (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    text TEXT NOT NULL,
    due_at REAL NOT NULL,
    repeat TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'pending',
    created_at REAL NOT NULL,
    fired_at REAL
);
CREATE TABLE IF NOT EXISTS notifications (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    text TEXT NOT NULL,
    level TEXT NOT NULL DEFAULT 'info',
    seen INTEGER NOT NULL DEFAULT 0,
    created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS kv (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS traces (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    conv_id TEXT NOT NULL,
    model TEXT NOT NULL,
    channel TEXT NOT NULL DEFAULT 'web',
    first_token_ms INTEGER,
    total_ms INTEGER NOT NULL,
    input_tokens INTEGER NOT NULL DEFAULT 0,
    output_tokens INTEGER NOT NULL DEFAULT 0,
    cached_tokens INTEGER NOT NULL DEFAULT 0,
    instructions_chars INTEGER NOT NULL DEFAULT 0,
    prompt_chars INTEGER NOT NULL DEFAULT 0,
    tools TEXT NOT NULL DEFAULT '[]',
    error TEXT NOT NULL DEFAULT '',
    created_at REAL NOT NULL
);
"""


def _connect() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def init() -> None:
    with _connect() as conn:
        conn.executescript(_SCHEMA)


# --- conversations -----------------------------------------------------------

def create_conversation(model: str, workspace: str | None) -> dict:
    now = time.time()
    conv_id = uuid.uuid4().hex[:12]
    with _connect() as conn:
        conn.execute(
            "INSERT INTO conversations (id, model, workspace, created_at, updated_at) VALUES (?,?,?,?,?)",
            (conv_id, model, workspace, now, now),
        )
    return get_conversation(conv_id)


def get_conversation(conv_id: str) -> dict | None:
    with _connect() as conn:
        row = conn.execute("SELECT * FROM conversations WHERE id=?", (conv_id,)).fetchone()
    return dict(row) if row else None


def list_conversations(limit: int = 50) -> list[dict]:
    with _connect() as conn:
        rows = conn.execute(
            "SELECT id, title, model, workspace, updated_at FROM conversations "
            "ORDER BY updated_at DESC LIMIT ?",
            (limit,),
        ).fetchall()
    return [dict(r) for r in rows]


def delete_conversation(conv_id: str) -> None:
    with _connect() as conn:
        conn.execute("DELETE FROM conversations WHERE id=?", (conv_id,))
        conn.execute("DELETE FROM ui_messages WHERE conv_id=?", (conv_id,))


def update_conversation(conv_id: str, **fields) -> None:
    if not fields:
        return
    fields["updated_at"] = time.time()
    keys = ", ".join(f"{k}=?" for k in fields)
    with _connect() as conn:
        conn.execute(f"UPDATE conversations SET {keys} WHERE id=?", (*fields.values(), conv_id))


def stale_undigested_conversations(idle_seconds: float = 1800) -> list[dict]:
    """Conversations idle for a while and not yet turned into an episode digest."""
    cutoff = time.time() - idle_seconds
    with _connect() as conn:
        rows = conn.execute(
            "SELECT * FROM conversations WHERE digested=0 AND updated_at < ? "
            "AND EXISTS (SELECT 1 FROM ui_messages m WHERE m.conv_id=conversations.id)",
            (cutoff,),
        ).fetchall()
    return [dict(r) for r in rows]


# --- UI messages -------------------------------------------------------------

def add_ui_message(conv_id: str, role: str, content: str, meta: dict | None = None) -> None:
    with _connect() as conn:
        conn.execute(
            "INSERT INTO ui_messages (conv_id, role, content, meta, created_at) VALUES (?,?,?,?,?)",
            (conv_id, role, content, json.dumps(meta or {}), time.time()),
        )
        conn.execute("UPDATE conversations SET updated_at=? WHERE id=?", (time.time(), conv_id))


def list_ui_messages(conv_id: str) -> list[dict]:
    with _connect() as conn:
        rows = conn.execute(
            "SELECT role, content, meta, created_at FROM ui_messages WHERE conv_id=? ORDER BY id",
            (conv_id,),
        ).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["meta"] = json.loads(d["meta"])
        out.append(d)
    return out


# --- usage -------------------------------------------------------------------

def add_usage(conv_id: str, model: str, input_tokens: int, output_tokens: int, cache_read: int) -> None:
    with _connect() as conn:
        conn.execute(
            "INSERT INTO usage (conv_id, model, input_tokens, output_tokens, cache_read_tokens, created_at) "
            "VALUES (?,?,?,?,?,?)",
            (conv_id, model, input_tokens, output_tokens, cache_read, time.time()),
        )


def usage_summary(days: int = 7) -> list[dict]:
    cutoff = time.time() - days * 86400
    with _connect() as conn:
        rows = conn.execute(
            "SELECT model, SUM(input_tokens) AS input, SUM(output_tokens) AS output, "
            "SUM(cache_read_tokens) AS cached, COUNT(*) AS turns FROM usage "
            "WHERE created_at > ? GROUP BY model",
            (cutoff,),
        ).fetchall()
    return [dict(r) for r in rows]


# --- traces ------------------------------------------------------------------

def add_trace(conv_id: str, model: str, channel: str, first_token_ms: int | None,
              total_ms: int, input_tokens: int, output_tokens: int, cached_tokens: int,
              instructions_chars: int, prompt_chars: int, tools: list, error: str = "") -> None:
    with _connect() as conn:
        conn.execute(
            "INSERT INTO traces (conv_id, model, channel, first_token_ms, total_ms, input_tokens, "
            "output_tokens, cached_tokens, instructions_chars, prompt_chars, tools, error, created_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (conv_id, model, channel, first_token_ms, total_ms, input_tokens, output_tokens,
             cached_tokens, instructions_chars, prompt_chars, json.dumps(tools), error[:500], time.time()),
        )
        conn.execute(  # keep the table bounded
            "DELETE FROM traces WHERE id NOT IN (SELECT id FROM traces ORDER BY id DESC LIMIT 500)")


def list_traces(limit: int = 50) -> list[dict]:
    with _connect() as conn:
        rows = conn.execute(
            "SELECT * FROM traces ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["tools"] = json.loads(d["tools"] or "[]")
        out.append(d)
    return out


def trace_summary(days: int = 7) -> list[dict]:
    """Per-model medians/averages for latency + token shape — the perf dashboard row."""
    cutoff = time.time() - days * 86400
    with _connect() as conn:
        rows = conn.execute(
            "SELECT model, COUNT(*) AS turns, CAST(AVG(total_ms) AS INT) AS avg_ms, "
            "MAX(total_ms) AS max_ms, CAST(AVG(first_token_ms) AS INT) AS avg_first_ms, "
            "SUM(input_tokens) AS input, SUM(output_tokens) AS output, SUM(cached_tokens) AS cached, "
            "CAST(AVG(instructions_chars) AS INT) AS avg_instr_chars, "
            "SUM(CASE WHEN error != '' THEN 1 ELSE 0 END) AS errors "
            "FROM traces WHERE created_at > ? GROUP BY model ORDER BY turns DESC",
            (cutoff,),
        ).fetchall()
    return [dict(r) for r in rows]


# --- missions ----------------------------------------------------------------

def create_mission(title: str, workspace: str, repo: str | None, jira_key: str | None,
                   description: str, executor: str) -> dict:
    now = time.time()
    with _connect() as conn:
        cur = conn.execute(
            "INSERT INTO missions (title, jira_key, workspace, repo, description, executor, created_at, updated_at) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (title, jira_key, workspace, repo, description, executor, now, now),
        )
        mid = cur.lastrowid
    return get_mission(mid)


def get_mission(mission_id: int) -> dict | None:
    with _connect() as conn:
        row = conn.execute("SELECT * FROM missions WHERE id=?", (mission_id,)).fetchone()
    return dict(row) if row else None


def list_missions(limit: int = 30) -> list[dict]:
    with _connect() as conn:
        rows = conn.execute("SELECT * FROM missions ORDER BY updated_at DESC LIMIT ?", (limit,)).fetchall()
    return [dict(r) for r in rows]


def update_mission(mission_id: int, **fields) -> None:
    if not fields:
        return
    fields["updated_at"] = time.time()
    keys = ", ".join(f"{k}=?" for k in fields)
    with _connect() as conn:
        conn.execute(f"UPDATE missions SET {keys} WHERE id=?", (*fields.values(), mission_id))


# --- background tasks --------------------------------------------------------

def create_task(title: str, kind: str, prompt: str, workspace: str | None,
                teams_chat: str = "") -> dict:
    with _connect() as conn:
        cur = conn.execute(
            "INSERT INTO tasks (title, kind, prompt, workspace, teams_chat, created_at) "
            "VALUES (?,?,?,?,?,?)",
            (title, kind, prompt, workspace, teams_chat, time.time()),
        )
        tid = cur.lastrowid
    return get_task(tid)


def get_task(task_id: int) -> dict | None:
    with _connect() as conn:
        row = conn.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
    return dict(row) if row else None


def list_tasks(limit: int = 30) -> list[dict]:
    with _connect() as conn:
        rows = conn.execute("SELECT * FROM tasks ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
    return [dict(r) for r in rows]


def update_task(task_id: int, **fields) -> None:
    if not fields:
        return
    keys = ", ".join(f"{k}=?" for k in fields)
    with _connect() as conn:
        conn.execute(f"UPDATE tasks SET {keys} WHERE id=?", (*fields.values(), task_id))


# --- reminders ---------------------------------------------------------------

def create_reminder(text: str, due_at: float, repeat: str = "") -> dict:
    with _connect() as conn:
        cur = conn.execute(
            "INSERT INTO reminders (text, due_at, repeat, created_at) VALUES (?,?,?,?)",
            (text, due_at, repeat, time.time()),
        )
        rid = cur.lastrowid
    return get_reminder(rid)


def get_reminder(reminder_id: int) -> dict | None:
    with _connect() as conn:
        row = conn.execute("SELECT * FROM reminders WHERE id=?", (reminder_id,)).fetchone()
    return dict(row) if row else None


def list_reminders(pending_only: bool = True, limit: int = 50) -> list[dict]:
    q = "SELECT * FROM reminders"
    if pending_only:
        q += " WHERE status='pending'"
    q += " ORDER BY due_at LIMIT ?"
    with _connect() as conn:
        rows = conn.execute(q, (limit,)).fetchall()
    return [dict(r) for r in rows]


def due_reminders(now: float) -> list[dict]:
    with _connect() as conn:
        rows = conn.execute(
            "SELECT * FROM reminders WHERE status='pending' AND due_at <= ?", (now,)).fetchall()
    return [dict(r) for r in rows]


def update_reminder(reminder_id: int, **fields) -> None:
    if not fields:
        return
    keys = ", ".join(f"{k}=?" for k in fields)
    with _connect() as conn:
        conn.execute(f"UPDATE reminders SET {keys} WHERE id=?", (*fields.values(), reminder_id))


# --- notifications -----------------------------------------------------------

def add_notification(text: str, level: str = "info") -> int:
    with _connect() as conn:
        cur = conn.execute(
            "INSERT INTO notifications (text, level, created_at) VALUES (?,?,?)",
            (text, level, time.time()),
        )
        return cur.lastrowid


def list_notifications(limit: int = 30, unseen_only: bool = False) -> list[dict]:
    q = "SELECT * FROM notifications"
    if unseen_only:
        q += " WHERE seen=0"
    q += " ORDER BY id DESC LIMIT ?"
    with _connect() as conn:
        rows = conn.execute(q, (limit,)).fetchall()
    return [dict(r) for r in rows]


def mark_notifications_seen() -> None:
    with _connect() as conn:
        conn.execute("UPDATE notifications SET seen=1 WHERE seen=0")


# --- key/value (watermarks for watchers) -------------------------------------

def kv_get(key: str) -> str | None:
    with _connect() as conn:
        row = conn.execute("SELECT value FROM kv WHERE key=?", (key,)).fetchone()
    return row["value"] if row else None


def kv_set(key: str, value: str) -> None:
    with _connect() as conn:
        conn.execute(
            "INSERT INTO kv (key, value) VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value),
        )


def kv_del(key: str) -> None:
    with _connect() as conn:
        conn.execute("DELETE FROM kv WHERE key=?", (key,))


# --- memory FTS index --------------------------------------------------------

def memory_reindex(docs: list[dict]) -> None:
    """Replace the whole memory index. docs: [{path, title, mtype, body, date}]"""
    with _connect() as conn:
        # `date` was added for recency-weighted recall; an existing DB still has
        # the 4-column table, and fts5 cannot ALTER, so rebuild it. Costless:
        # this function repopulates every row anyway.
        cols = [r[1] for r in conn.execute("PRAGMA table_info(memory_fts)").fetchall()]
        if "date" not in cols:
            conn.execute("DROP TABLE IF EXISTS memory_fts")
            conn.execute("CREATE VIRTUAL TABLE memory_fts USING fts5("
                         "path, title, mtype, body, date UNINDEXED)")
        conn.execute("DELETE FROM memory_fts")
        conn.executemany(
            "INSERT INTO memory_fts (path, title, mtype, body, date) "
            "VALUES (:path, :title, :mtype, :body, :date)",
            [{**d, "date": d.get("date", "")} for d in docs],
        )


def memory_search(query: str, k: int = 4) -> list[dict]:
    # Sanitize into an OR query of bare words so user text can't break FTS syntax.
    words = [w for w in "".join(c if c.isalnum() else " " for c in query).split() if len(w) > 2]
    if not words:
        return []
    match = " OR ".join(dict.fromkeys(words[:12]))
    with _connect() as conn:
        rows = conn.execute(
            "SELECT path, title, mtype, date, "
            "snippet(memory_fts, 4, '', '', ' … ', 40) AS snippet "
            "FROM memory_fts WHERE memory_fts MATCH ? ORDER BY rank LIMIT ?",
            (match, k),
        ).fetchall()
    return [dict(r) for r in rows]
