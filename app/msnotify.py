"""Teams/Outlook mention watcher — the OPTIONAL low-latency ping trigger.

Reads the macOS Notification Center database (the same banners that pop up in the
corner of your screen) and raises an Asta notification when a Teams or Outlook
banner mentions you. Pure Python — notification text never reaches an LLM, so the
token cost is zero — and near-instant, since a new row appears the moment a banner
fires.

NOT the primary path, and DISABLED BY DEFAULT. The default ping trigger is
`teams_bridge.activity_watch_loop()`, which reads the Teams Activity feed in
Playwright — the same browser session that does the reading, replying and
sending. That one needs no special OS permission and sees muted/DND chats too, so
it covers the same mentions this does. This watcher only buys you *latency*:
sub-poll-interval alerts, at the cost of Full Disk Access.

Turn it on only if you want that:
  1. Grant Full Disk Access to the Python that launchd runs (deploy/install.sh —
     a shell-launched server is attributed to Terminal.app instead, so the grant
     silently does nothing; System Settings → Privacy & Security → Full Disk
     Access).
  2. Set TEAMS_WATCHER=1 in .env (and optionally TEAMS_WATCH_KEYWORDS).
  3. Make sure Teams/Outlook banners are ON in System Settings → Notifications.

Limitations that are exactly why it is not the default:
  - only sees what macOS shows as a banner (muted chats / DND = invisible);
  - notification text is short (title + preview), not the full message;
  - read-only: it alerts you and can seed a mission, it can't reply in Teams —
    the Playwright path is the one that can.
"""

from __future__ import annotations

import os
import plistlib
import shutil
import sqlite3
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import store

DB_PATH = (
    Path.home()
    / "Library/Group Containers/group.com.apple.usernoted/db2/db"
)

# Bundle ids for the apps we care about (old + new Teams clients).
WATCHED_APPS = {
    "com.microsoft.teams2": "Teams",
    "com.microsoft.teams": "Teams",
    "com.microsoft.teams2.launcher": "Teams",
    "com.microsoft.Outlook": "Outlook",
}

APPLE_EPOCH = datetime(2001, 1, 1, tzinfo=timezone.utc)
WATERMARK_KEY = "msnotify_watermark"
POLL_SECONDS = 120


def enabled() -> bool:
    return os.environ.get("TEAMS_WATCHER", "0").strip() in ("1", "true", "yes")


def keywords() -> list[str]:
    """Lowercase keywords that mark a notification as 'about me'.

    Default: first token of the macOS username + 'mention'. Override with
    TEAMS_WATCH_KEYWORDS=arun,@arun,mentioned you  (comma-separated).
    """
    raw = os.environ.get("TEAMS_WATCH_KEYWORDS", "")
    if raw.strip():
        return [k.strip().lower() for k in raw.split(",") if k.strip()]
    user = os.environ.get("USER", "").split(".")[0]
    kws = ["mention"]
    if user:
        kws.append(user.lower())
    return kws


def status() -> dict:
    if not enabled():
        return {"enabled": False, "reason": "set TEAMS_WATCHER=1 in .env to enable"}
    if not DB_PATH.is_file():
        return {"enabled": True, "ok": False,
                "reason": "notification DB not readable — grant Full Disk Access"}
    # prove we can actually read it — is_file() passes even when copy/read would
    # be denied, which otherwise fails silently forever in check()
    try:
        items, newest = _read_new_notifications(0)
    except Exception as exc:
        return {"enabled": True, "ok": False,
                "reason": f"DB read failed ({type(exc).__name__}: {exc}) — grant Full Disk Access to the process running Asta"}
    return {"enabled": True, "ok": True, "keywords": keywords(),
            "banners_from_watched_apps": len(items)}


def _read_new_notifications(since_apple: float) -> tuple[list[dict], float]:
    """Copy the NC database (it's WAL-locked live) and pull rows newer than the watermark."""
    with tempfile.TemporaryDirectory() as tmp:
        dst = Path(tmp) / "db"
        shutil.copy2(DB_PATH, dst)
        for suffix in ("-wal", "-shm"):
            side = DB_PATH.parent / (DB_PATH.name + suffix)
            if side.is_file():
                shutil.copy2(side, Path(tmp) / (dst.name + suffix))
        con = sqlite3.connect(f"file:{dst}?mode=ro", uri=True)
        try:
            rows = con.execute(
                """
                SELECT a.identifier, r.data, r.delivered_date
                FROM record r JOIN app a ON a.app_id = r.app_id
                WHERE r.delivered_date > ?
                ORDER BY r.delivered_date ASC
                """,
                (since_apple,),
            ).fetchall()
        finally:
            con.close()

    out: list[dict] = []
    newest = since_apple
    for identifier, blob, delivered in rows:
        newest = max(newest, delivered or 0)
        app_label = WATCHED_APPS.get(identifier)
        if not app_label or not blob:
            continue
        try:
            plist = plistlib.loads(blob)
        except Exception:
            continue
        req = plist.get("req") or {}
        out.append({
            "app": app_label,
            "title": str(req.get("titl") or ""),
            "subtitle": str(req.get("subt") or ""),
            "body": str(req.get("body") or ""),
            "when": (APPLE_EPOCH + timedelta(seconds=delivered or 0)).astimezone(),
        })
    return out, newest


def check() -> list[str]:
    """One poll: returns human-readable lines for new Teams/Outlook mentions.

    Runs entirely locally (sqlite + plistlib) — no LLM, no network.
    """
    if not enabled() or not DB_PATH.is_file():
        return []
    watermark = float(store.kv_get(WATERMARK_KEY) or 0)
    first_run = watermark == 0
    try:
        items, newest = _read_new_notifications(watermark)
    except (sqlite3.Error, PermissionError, OSError):
        return []  # no Full Disk Access / DB busy — silently skip this cycle
    if newest > watermark:
        store.kv_set(WATERMARK_KEY, str(newest))
    if first_run:
        return []  # establish the watermark; don't replay history as "new"

    kws = keywords()
    lines: list[str] = []
    for it in items:
        text = f"{it['title']} {it['subtitle']} {it['body']}".lower()
        if not any(k in text for k in kws):
            continue
        preview = " — ".join(p for p in (it["title"], it["subtitle"], it["body"]) if p)
        lines.append(f"[{it['app']}] {preview[:300]} ({it['when']:%H:%M})")
    return lines
