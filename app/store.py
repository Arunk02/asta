"""SQLite persistence: conversations, UI messages, usage, and the memory FTS index."""

from __future__ import annotations

import json
import sqlite3
import threading
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
    finished_at REAL,
    -- Where the work ended up, and what has happened to it since.
    --
    -- A task used to end at "done" — diff written, nothing pushed — and that was
    -- the last Asta ever thought about it. The PR it later became was tracked
    -- nowhere, so CI going red on it, a review comment, or the merge itself all
    -- landed outside anything that knew which task they belonged to.
    pr_urls TEXT NOT NULL DEFAULT '',
    pr_state TEXT NOT NULL DEFAULT '',
    pr_checked_at REAL
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
-- Teams messages Asta has actually seen, kept so that "what did Vinish say last
-- night" is answerable at all.
--
-- Reading a chat used to mean one querySelectorAll over whatever Teams happened
-- to have rendered, returning "Sender: text" with no time attached. That cannot
-- answer a question with a WHEN in it: there was nothing to filter on, nothing
-- older than the live DOM could be reached, and nothing survived the read. Teams
-- virtualises the thread, so the messages simply stopped existing once they
-- scrolled out.
--
-- `sent_at` is nullable on purpose. Teams renders some rows without a machine
-- readable time, and storing a guessed timestamp would be worse than storing
-- none — a wrong time silently reassigns a message to the wrong night.
CREATE TABLE IF NOT EXISTS teams_messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    key TEXT NOT NULL UNIQUE,
    chat TEXT NOT NULL,
    sender TEXT NOT NULL DEFAULT '',
    text TEXT NOT NULL,
    sent_at REAL,
    stamp TEXT NOT NULL DEFAULT '',
    seen_at REAL NOT NULL
);
CREATE VIRTUAL TABLE IF NOT EXISTS teams_fts USING fts5(
    chat, sender, text, content=teams_messages, content_rowid=rowid
);
CREATE TRIGGER IF NOT EXISTS teams_fts_ins AFTER INSERT ON teams_messages BEGIN
    INSERT INTO teams_fts(rowid, chat, sender, text)
    VALUES (new.rowid, new.chat, new.sender, new.text);
END;
CREATE TRIGGER IF NOT EXISTS teams_fts_del AFTER DELETE ON teams_messages BEGIN
    INSERT INTO teams_fts(teams_fts, rowid, chat, sender, text)
    VALUES ('delete', old.rowid, old.chat, old.sender, old.text);
END;
CREATE INDEX IF NOT EXISTS idx_teams_messages_chat ON teams_messages(chat, sent_at);
-- One row per THING THAT WANTS ARUN, whatever channel carried it.
--
-- The `key` is deliberately UNIQUE across every source rather than per-source:
-- a ServiceNow incident that arrives as mail, as a Teams mention and as a CI
-- alert is ONE thing wanting him, and the previous design had no way to know
-- that. `goes_to_hold` in outlook.py exists purely because that collision was
-- found by hand, once; this is the general answer to it.
--
-- `state` is what the old `notifications` table never had: after a push, nothing
-- recorded whether the thing was ever dealt with, so an ask missed while he was
-- away was gone for good. notifications stays exactly as it is — that is the UI
-- bell, a log of what was SAID. This is the log of what is OWED.
CREATE TABLE IF NOT EXISTS attention (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    key TEXT NOT NULL UNIQUE,
    source TEXT NOT NULL,
    sources TEXT NOT NULL DEFAULT '',
    who TEXT NOT NULL DEFAULT '',
    what TEXT NOT NULL DEFAULT '',
    priority INTEGER NOT NULL DEFAULT 2,
    why TEXT NOT NULL DEFAULT '',
    due_at REAL,
    state TEXT NOT NULL DEFAULT 'new',
    seen_count INTEGER NOT NULL DEFAULT 1,
    first_seen REAL NOT NULL,
    last_seen REAL NOT NULL,
    notified_at REAL,
    acted_at REAL,
    chased_at REAL
);
CREATE INDEX IF NOT EXISTS idx_attention_state ON attention(state, priority);
-- What Asta has learned about a person, as counters rather than opinions.
--
-- `_BULK_SENDER` in outlook.py is a hand-written regex: every new noisy sender
-- is a code change, and every important human is anonymous to it — the manager
-- who needs an answer and a stranger's cold email score identically. A regex
-- list does not scale; counters do, and they cost arithmetic.
--
-- `met` is seeded from calendar co-attendance, which is already scraped and
-- is the one OBJECTIVE fact available before any learning has happened: a
-- person he sits in meetings with is not bulk mail, whatever the wording of
-- their subject line.
CREATE TABLE IF NOT EXISTS contacts (
    who TEXT PRIMARY KEY,
    engaged INTEGER NOT NULL DEFAULT 0,
    ignored INTEGER NOT NULL DEFAULT 0,
    muted INTEGER NOT NULL DEFAULT 0,
    met INTEGER NOT NULL DEFAULT 0,
    first_seen REAL NOT NULL,
    last_seen REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS questions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    text TEXT NOT NULL,
    source TEXT NOT NULL DEFAULT '',
    answer TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'open',
    created_at REAL NOT NULL,
    answered_at REAL
);
CREATE TABLE IF NOT EXISTS outcomes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    kind TEXT NOT NULL,
    subject TEXT NOT NULL DEFAULT '',
    outcome TEXT NOT NULL,
    detail TEXT NOT NULL DEFAULT '',
    created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_outcomes_kind ON outcomes(kind, created_at);
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
    -- Cache WRITES bill at 1.25x and were previously invisible; on a first turn
    -- they are the single largest line. Without this column a fat orientation
    -- block looks free, which is exactly the mistake that made traces useless.
    cache_write_tokens INTEGER NOT NULL DEFAULT 0,
    -- What the executor itself says the turn cost. CLI turns run on a
    -- subscription rather than per-token billing, so this is 0 there — the token
    -- columns are the comparable measure across brains.
    cost_usd REAL NOT NULL DEFAULT 0,
    -- 1 when the numbers came from the executor, 0 when they are a char-count
    -- estimate. Mixing the two silently is how a trend line starts lying.
    measured INTEGER NOT NULL DEFAULT 0,
    instructions_chars INTEGER NOT NULL DEFAULT 0,
    prompt_chars INTEGER NOT NULL DEFAULT 0,
    tools TEXT NOT NULL DEFAULT '[]',
    error TEXT NOT NULL DEFAULT '',
    created_at REAL NOT NULL
);
"""

# Columns added after the table shipped. CREATE TABLE IF NOT EXISTS leaves an
# existing table alone, so a plain schema edit reaches new installs only — the
# one machine that actually has the history would keep the old shape.
_ADDED_COLUMNS = {
    "traces": (("cache_write_tokens", "INTEGER NOT NULL DEFAULT 0"),
               ("cost_usd", "REAL NOT NULL DEFAULT 0"),
               ("measured", "INTEGER NOT NULL DEFAULT 0")),
    "usage": (("cache_write_tokens", "INTEGER NOT NULL DEFAULT 0"),),
    # Added after the ledger shipped. Any machine that already ran the server
    # once has the table without it, and CREATE TABLE IF NOT EXISTS would leave
    # that machine behind — which is the only one with real history on it.
    "attention": (("chased_at", "REAL"),),
    # Added when tasks stopped ending at the diff. The machine that matters is
    # the one with real task history on it, and CREATE TABLE IF NOT EXISTS would
    # be exactly the one to skip it.
    "tasks": (("pr_urls", "TEXT NOT NULL DEFAULT ''"),
              ("pr_state", "TEXT NOT NULL DEFAULT ''"),
              ("pr_checked_at", "REAL")),
}


def _migrate(conn: sqlite3.Connection) -> None:
    for table, columns in _ADDED_COLUMNS.items():
        have = {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
        for name, decl in columns:
            if name not in have:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {decl}")


#: One connection per thread, reused. Measured before this existed: a `kv_get`
#: took 0.231 ms, of which the QUERY was 0.002 ms — 99% of every store call in the
#: system was opening a connection and re-running `PRAGMA journal_mode=WAL`, then
#: throwing it away. Cheap when the system did one thing at a time; less so now
#: that three tasks run in parallel, the activity feed polls every sixty seconds,
#: and every stored message maintains an FTS index.
#:
#: Thread-local because a sqlite3 connection may not cross threads, and this
#: codebase uses `asyncio.to_thread` in several places. Keyed on the path as well,
#: so a test that repoints DB_PATH gets its own connection rather than the last
#: one — the isolation rule in conftest depends on that.
_local = threading.local()


def _connect() -> sqlite3.Connection:
    """The connection for this thread, opening one only if there isn't one.

    Callers use this as `with _connect() as conn:`, which in sqlite3 is a
    TRANSACTION context manager rather than a closing one — so reusing the
    connection changes nothing about how every existing call site behaves.
    """
    cached = getattr(_local, "conn", None)
    if cached is not None and getattr(_local, "path", None) == DB_PATH:
        return cached
    if cached is not None:
        try:
            cached.close()                 # DB_PATH moved (a test): drop the old one
        except sqlite3.Error:
            pass
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    # Wait rather than fail when another connection holds the write lock. With
    # parallel tasks there now IS another connection, and "database is locked"
    # would surface as a lost notification rather than a delay.
    conn.execute("PRAGMA busy_timeout=5000")
    _local.conn, _local.path = conn, DB_PATH
    return conn


def close_connection() -> None:
    """Drop this thread's connection. For shutdown and for tests."""
    conn = getattr(_local, "conn", None)
    if conn is not None:
        try:
            conn.close()
        except sqlite3.Error:
            pass
    _local.conn = _local.path = None


def init() -> None:
    with _connect() as conn:
        conn.executescript(_SCHEMA)
        _migrate(conn)


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

def add_usage(conv_id: str, model: str, input_tokens: int, output_tokens: int,
              cache_read: int, cache_write: int = 0) -> None:
    with _connect() as conn:
        conn.execute(
            "INSERT INTO usage (conv_id, model, input_tokens, output_tokens, cache_read_tokens, "
            "cache_write_tokens, created_at) VALUES (?,?,?,?,?,?,?)",
            (conv_id, model, input_tokens, output_tokens, cache_read, cache_write, time.time()),
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
              instructions_chars: int, prompt_chars: int, tools: list, error: str = "",
              cache_write_tokens: int = 0, cost_usd: float = 0.0,
              measured: bool = False) -> None:
    with _connect() as conn:
        conn.execute(
            "INSERT INTO traces (conv_id, model, channel, first_token_ms, total_ms, input_tokens, "
            "output_tokens, cached_tokens, cache_write_tokens, cost_usd, measured, "
            "instructions_chars, prompt_chars, tools, error, created_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (conv_id, model, channel, first_token_ms, total_ms, input_tokens, output_tokens,
             cached_tokens, cache_write_tokens, cost_usd, int(measured),
             instructions_chars, prompt_chars, json.dumps(tools), error[:500], time.time()),
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


# --- attention ledger ---------------------------------------------------------

def attention_upsert(key: str, source: str, who: str = "", what: str = "",
                     priority: int = 2, why: str = "", due_at: float | None = None,
                     now: float | None = None) -> dict:
    """Record that something wants Arun, or note that it wants him AGAIN.

    Re-seeing a thing must never reset it. That is the whole reason the ledger
    exists: `seen_count` climbing while `state` stays `notified` is precisely the
    signal that someone is chasing him, and overwriting the row on every poll
    would erase it every five minutes. So a second sighting bumps the counter and
    the timestamps, keeps the FIRST source, and leaves the lifecycle alone.

    A better priority is allowed to win, though — an alert that started as a
    warning and has since become an outage should not stay ranked at what it
    looked like the first time.
    """
    now = time.time() if now is None else now
    with _connect() as conn:
        row = conn.execute("SELECT * FROM attention WHERE key=?", (key,)).fetchone()
        if row is None:
            conn.execute(
                "INSERT INTO attention (key, source, sources, who, what, priority, why,"
                " due_at, state, seen_count, first_seen, last_seen)"
                " VALUES (?,?,?,?,?,?,?,?,'new',1,?,?)",
                (key, source, source, who, what, priority, why, due_at, now, now))
        else:
            sources = [s for s in (row["sources"] or "").split(",") if s]
            if source not in sources:
                sources.append(source)
            conn.execute(
                "UPDATE attention SET seen_count=seen_count+1, last_seen=?, sources=?,"
                " priority=MIN(priority,?), due_at=COALESCE(?,due_at),"
                " what=CASE WHEN ?<>'' THEN ? ELSE what END WHERE key=?",
                (now, ",".join(sources), priority, due_at, what, what, key))
        return dict(conn.execute("SELECT * FROM attention WHERE key=?", (key,)).fetchone())


def attention_get(key: str) -> dict | None:
    with _connect() as conn:
        row = conn.execute("SELECT * FROM attention WHERE key=?", (key,)).fetchone()
    return dict(row) if row else None


def attention_set(key: str, **fields) -> None:
    if not fields:
        return
    keys = ", ".join(f"{k}=?" for k in fields)
    with _connect() as conn:
        conn.execute(f"UPDATE attention SET {keys} WHERE key=?", (*fields.values(), key))


def attention_open(limit: int = 50, max_priority: int = 3) -> list[dict]:
    """What is still owed, most urgent first — the answer to "what's on my plate".

    Ordered by priority then age, so the oldest unanswered P1 outranks a P1 that
    arrived a minute ago. `acted` and `dropped` are settled and never returned.
    """
    with _connect() as conn:
        rows = conn.execute(
            "SELECT * FROM attention WHERE state IN ('new','notified') AND priority<=?"
            " ORDER BY priority ASC, first_seen ASC LIMIT ?", (max_priority, limit)).fetchall()
    return [dict(r) for r in rows]


def attention_purge(before: float) -> int:
    """Drop settled rows older than `before`, so the ledger cannot grow forever."""
    with _connect() as conn:
        cur = conn.execute(
            "DELETE FROM attention WHERE state IN ('acted','dropped') AND last_seen < ?",
            (before,))
        return cur.rowcount


# --- contacts (what Asta has learned about a person) --------------------------

_CONTACT_FIELDS = ("engaged", "ignored", "muted", "met")


def contact_bump(who: str, field: str, n: int = 1, now: float | None = None) -> None:
    if not who or field not in _CONTACT_FIELDS:
        return
    now = time.time() if now is None else now
    with _connect() as conn:
        conn.execute(
            "INSERT INTO contacts (who, first_seen, last_seen) VALUES (?,?,?)"
            " ON CONFLICT(who) DO NOTHING", (who, now, now))
        conn.execute(
            f"UPDATE contacts SET {field}={field}+?, last_seen=? WHERE who=?", (n, now, who))


def contact_get(who: str) -> dict | None:
    with _connect() as conn:
        row = conn.execute("SELECT * FROM contacts WHERE who=?", (who,)).fetchone()
    return dict(row) if row else None


def contacts_list(limit: int = 200) -> list[dict]:
    with _connect() as conn:
        rows = conn.execute(
            "SELECT * FROM contacts ORDER BY (engaged+ignored+muted) DESC LIMIT ?",
            (limit,)).fetchall()
    return [dict(r) for r in rows]


# --- key/value (watermarks for watchers) -------------------------------------

# --- questions (ask_user) ----------------------------------------------------

def create_question(text: str, source: str = "") -> dict:
    with _connect() as conn:
        cur = conn.execute(
            "INSERT INTO questions (text, source, created_at) VALUES (?,?,?)",
            (text, source, time.time()))
        qid = cur.lastrowid
    return get_question(qid)


def get_question(qid: int) -> dict | None:
    with _connect() as conn:
        row = conn.execute("SELECT * FROM questions WHERE id=?", (qid,)).fetchone()
    return dict(row) if row else None


def open_questions() -> list[dict]:
    with _connect() as conn:
        rows = conn.execute(
            "SELECT * FROM questions WHERE status='open' ORDER BY id").fetchall()
    return [dict(r) for r in rows]


def close_question(qid: int, answer: str, status: str = "answered") -> None:
    with _connect() as conn:
        conn.execute(
            "UPDATE questions SET answer=?, status=?, answered_at=? WHERE id=?",
            (answer, status, time.time(), qid))


def expire_open_questions() -> int:
    """Called at startup: a question whose waiter died with the process can never
    be answered, and a stale one would swallow the next message as its answer."""
    with _connect() as conn:
        cur = conn.execute(
            "UPDATE questions SET status='expired', answered_at=? WHERE status='open'",
            (time.time(),))
    return cur.rowcount or 0


# --- outcomes (self-evaluation) ----------------------------------------------

def record_outcome(kind: str, outcome: str, subject: str = "", detail: str = "") -> None:
    """Measurement must never break the thing being measured — a long-lived
    process on a pre-outcomes database would otherwise fail a finished task."""
    try:
        with _connect() as conn:
            conn.execute(
                "INSERT INTO outcomes (kind, subject, outcome, detail, created_at) VALUES (?,?,?,?,?)",
                (kind, subject, outcome, detail[:1000], time.time()))
    except sqlite3.Error:
        init()


def outcome_counts(since: float = 0.0) -> list[dict]:
    with _connect() as conn:
        rows = conn.execute(
            "SELECT kind, outcome, COUNT(*) AS n FROM outcomes WHERE created_at >= ? "
            "GROUP BY kind, outcome ORDER BY kind, n DESC", (since,)).fetchall()
    return [dict(r) for r in rows]


def recent_outcomes(limit: int = 30) -> list[dict]:
    with _connect() as conn:
        rows = conn.execute(
            "SELECT * FROM outcomes ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
    return [dict(r) for r in rows]


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


# --- Teams message history ---------------------------------------------------

def save_teams_messages(rows: list[dict]) -> int:
    """Remember messages read out of a Teams thread. Returns how many were new.

    Idempotent by `key`, because every read of a chat re-sees the same recent
    messages — without that, asking twice would double the thread.
    """
    if not rows:
        return 0
    now = time.time()
    with _connect() as conn:
        # Counted by rows in the TABLE, not by `total_changes`: the FTS index has
        # an insert trigger, so every stored message now counts as several
        # changes and "how many were new" became three times the truth.
        before = conn.execute("SELECT COUNT(*) FROM teams_messages").fetchone()[0]
        conn.executemany(
            "INSERT INTO teams_messages (key, chat, sender, text, sent_at, stamp, seen_at) "
            "VALUES (:key, :chat, :sender, :text, :sent_at, :stamp, :seen_at) "
            "ON CONFLICT(key) DO NOTHING",
            [{"key": r["key"], "chat": r.get("chat", ""), "sender": r.get("sender", ""),
              "text": r.get("text", ""), "sent_at": r.get("sent_at"),
              "stamp": r.get("stamp", ""), "seen_at": now} for r in rows],
        )
        after = conn.execute("SELECT COUNT(*) FROM teams_messages").fetchone()[0]
        return after - before


#: How stale a read may be and still count as covering a window that is still
#: open. Anything sent in the last few minutes may not have been seen yet; beyond
#: that the cached answer starts being a guess about the present.
HISTORY_FRESH_SECONDS = 300.0


def teams_search(query: str, limit: int = 12) -> list[dict]:
    """Messages that are ABOUT something, rather than containing a substring.

    History was reachable two ways: by chat name, or by time window. Neither
    answers "what did we decide about the ATA fallback" — the question he
    actually asks — and `LIKE '%...%'` cannot, because it matches letters rather
    than words: it finds "ata" inside "data", ranks nothing, and misses a thread
    that said "transport order" instead.

    FTS5 is already in this file for memory, so this is the same mechanism
    pointed at the other corpus rather than a new one.
    """
    words = [w for w in "".join(c if c.isalnum() else " " for c in (query or "").lower()).split()
             if len(w) > 2 and w not in _RECALL_STOPWORDS]
    if not words:
        return []
    match = " OR ".join(dict.fromkeys(words[:12]))
    with _connect() as conn:
        try:
            rows = conn.execute(
                "SELECT m.*, bm25(teams_fts) AS score FROM teams_fts "
                "JOIN teams_messages m ON m.rowid = teams_fts.rowid "
                "WHERE teams_fts MATCH ? ORDER BY score LIMIT ?",
                (match, limit)).fetchall()
        except sqlite3.OperationalError:
            return []
    return [dict(r) for r in rows]


def teams_history_covers(chat: str, since: float, until: float) -> bool:
    """Whether stored history demonstrably covers the whole window.

    The bug this exists for: the caller used to accept ANY stored row as
    "history", so one message already seen from last night short-circuited the
    live scrollback and a partial thread was reported as complete. Having a row
    in a window says nothing about holding all of it.

    Coverage needs both edges to hold:

      back  — the oldest message stored for this chat sits at or before `since`,
              which is only true if a read once scrolled back past the window.
              A cache that starts inside the window is missing its beginning.
      front — the chat was last READ at or after `until`, so anything sent
              before the window closed had already arrived by then.

    Either edge open means fetch. Answering "nothing was sent" from a cache that
    simply never looked is the failure being closed here.
    """
    if not chat or since is None or until is None:
        return False
    with _connect() as conn:
        row = conn.execute(
            "SELECT MIN(sent_at) AS oldest, MAX(seen_at) AS last_read "
            "FROM teams_messages WHERE lower(chat) LIKE ? AND sent_at IS NOT NULL",
            (f"%{chat.strip().lower()}%",)).fetchone()
    if not row or row["oldest"] is None or row["last_read"] is None:
        return False
    # A window can still be OPEN. "Last night" runs to six this morning, so at
    # half past midnight `until` is in the future — and nobody can have read a
    # chat after a moment that has not happened. For an open window the honest
    # requirement is that the thread was read RECENTLY, not that it was read after
    # a moment still to come; demanding the latter re-opens a browser for every
    # such question, and demanding "read this instant" is the same thing with
    # extra steps.
    # Never demand a read more recent than the freshness window. A window that
    # ended long ago must have been read after it closed; one that ends now — or
    # has not ended at all, like "last night" at half past midnight — only needs a
    # recent read. Without the floor this asked for a read in the same instant the
    # question was asked, which nothing can satisfy.
    front = min(float(until), time.time() - HISTORY_FRESH_SECONDS)
    return float(row["oldest"]) <= float(since) and float(row["last_read"]) >= front


def teams_messages(chat: str = "", since: float | None = None,
                   until: float | None = None, limit: int = 200) -> list[dict]:
    """Stored messages, oldest first, optionally windowed by time.

    `chat` matches loosely: he asks for "Vinish" and the thread was stored under
    the full header Teams renders, "Vinish Kumar".

    Rows with no `sent_at` are excluded once a window is asked for — an untimed
    message cannot be honestly claimed to fall inside "last night".
    """
    sql = ["SELECT * FROM teams_messages WHERE 1=1"]
    args: list = []
    if chat:
        sql.append("AND lower(chat) LIKE ?")
        args.append(f"%{chat.strip().lower()}%")
    if since is not None:
        sql.append("AND sent_at IS NOT NULL AND sent_at >= ?")
        args.append(since)
    if until is not None:
        sql.append("AND sent_at IS NOT NULL AND sent_at <= ?")
        args.append(until)
    # NULL sent_at sorts last rather than first, so an untimed row never
    # masquerades as the oldest thing in the thread.
    sql.append("ORDER BY sent_at IS NULL, sent_at ASC, id ASC LIMIT ?")
    args.append(max(1, limit))
    with _connect() as conn:
        rows = conn.execute(" ".join(sql), args).fetchall()
    return [dict(r) for r in rows]


def teams_chats_known() -> list[str]:
    """Thread names history has been stored under."""
    with _connect() as conn:
        rows = conn.execute(
            "SELECT chat, COUNT(*) n, MAX(sent_at) last FROM teams_messages "
            "GROUP BY chat ORDER BY n DESC").fetchall()
    return [r["chat"] for r in rows]


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


# Words too generic to recall on. The match is an OR of every token, so ONE of
# these matching pulls an unrelated memory in by coincidence: "what is this error"
# matched a ten-day-old "IAM token error" note on the word "error" alone, and the
# model — handed it as a "relevant memory" — answered about that instead of the
# question actually asked. These carry no topic, so dropping them removes the
# false positives at no cost. A real query keeps its nouns ("grafana proxy error"
# still matches on grafana/proxy); only a pure meta-question ("what is this
# error") empties out — and then there genuinely is nothing to recall.
_RECALL_STOPWORDS = {
    "what", "why", "how", "when", "where", "who", "which", "whom", "whose",
    "this", "that", "these", "those", "there", "here", "then",
    "the", "and", "for", "are", "was", "were", "been", "being", "with", "from",
    "does", "did", "can", "could", "would", "should", "will", "shall", "have",
    "has", "had", "you", "your", "yours", "our", "ours", "its", "not",
    "please", "tell", "show", "explain", "mean", "means", "meaning", "say",
    "thing", "things", "something", "anything", "some", "any", "about", "into",
    "error", "errors", "issue", "issues", "bug", "bugs", "problem", "problems",
    "wrong", "fix", "fixing", "fixed", "help", "again", "now", "still", "just",
    "happening", "going", "getting", "like", "want", "need", "give", "make",
}


def memory_search(query: str, k: int = 4) -> list[dict]:
    """FTS candidate memories for `query`, best bm25 first, each carrying `score`.

    Generic words are stripped before matching (see _RECALL_STOPWORDS). If nothing
    distinctive survives, there is nothing to recall on — return empty rather than
    matching on noise. `score` is the FTS5 bm25 rank (more negative = stronger),
    exposed so the semantic layer can gate on it when no embedder is available.
    """
    words = [w for w in "".join(c if c.isalnum() else " " for c in query.lower()).split()
             if len(w) > 2 and w not in _RECALL_STOPWORDS]
    if not words:
        return []
    match = " OR ".join(dict.fromkeys(words[:12]))
    with _connect() as conn:
        rows = conn.execute(
            "SELECT path, title, mtype, date, rank AS score, "
            "snippet(memory_fts, 4, '', '', ' … ', 40) AS snippet "
            "FROM memory_fts WHERE memory_fts MATCH ? ORDER BY rank LIMIT ?",
            (match, k),
        ).fetchall()
    return [dict(r) for r in rows]
