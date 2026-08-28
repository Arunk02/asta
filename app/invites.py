"""Composing a calendar invite — the half of `meetings` with no browser in it.

Split out to keep `meetings.py` under the size the review put a test on. The line
count is a proxy; the real argument is that building an invite URL and running a
live call have nothing to do with each other beyond both involving a calendar,
and only one of them can be tested without a browser.

Everything here is re-exported from `meetings`, so every existing caller and every
monkeypatch keeps working — the same way the `call_brain` extraction was done.
"""

from __future__ import annotations

import re
import urllib.parse
from datetime import datetime, timedelta

COMPOSE_URL = "https://outlook.office.com/calendar/deeplink/compose"

def _iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%S")


def compose_url(subject: str, start: datetime, end: datetime,
                attendees: list[str] | None = None, body: str = "",
                all_day: bool = False, online: bool = True) -> str:
    """The Outlook deeplink that opens a fully pre-filled invite.

    Every field is placed by Outlook's own parser rather than by us clicking
    around its UI, so either the whole invite is right or the link is obviously
    wrong — there is no half-filled middle state to notice too late.
    """
    params = {
        "subject": subject,
        "startdt": _iso(start),
        "enddt": _iso(end),
        "body": body,
        "path": "/calendar/action/compose",
        "rru": "addevent",
    }
    if all_day:
        params["allday"] = "true"
    if online:
        params["online"] = "1"
    if attendees:
        params["to"] = ";".join(a.strip() for a in attendees if a.strip())
    return COMPOSE_URL + "?" + urllib.parse.urlencode(
        {k: v for k, v in params.items() if v not in ("", None)})


def leave_invite(start_date: str, end_date: str = "", reason: str = "",
                 to: list[str] | None = None) -> dict:
    """An all-day leave/out-of-office invite. Returns the invite, unsent.

    End date is EXCLUSIVE in the calendar's own model, so a single day off runs
    to the next morning. Getting that wrong by one day is the classic off-by-one
    in leave booking, and the person who finds it is whoever needed him on the
    day he was actually there.
    """
    start = _parse_day(start_date)
    end = _parse_day(end_date) if end_date else start
    if end < start:
        raise RuntimeError(f"leave ends ({end_date}) before it starts ({start_date})")
    subject = "Leave — Arun" + (f" ({reason})" if reason else "")
    return {
        "subject": subject,
        "start": start,
        "end": end + timedelta(days=1),        # exclusive: the day itself is included
        "all_day": True,
        "attendees": to or [],
        "body": (reason or "Out of office."),
        "days": (end - start).days + 1,
        "url": compose_url(subject, start, end + timedelta(days=1),
                           to or [], reason or "Out of office.",
                           all_day=True, online=False),
    }


def meeting_invite(subject: str, when: str, minutes: int = 30,
                   attendees: list[str] | None = None, agenda: str = "") -> dict:
    """A normal meeting. Returns the invite, unsent."""
    start = _parse_when(when)
    end = start + timedelta(minutes=max(5, minutes))
    return {
        "subject": subject,
        "start": start,
        "end": end,
        "all_day": False,
        "attendees": attendees or [],
        "body": agenda,
        "url": compose_url(subject, start, end, attendees or [], agenda, online=True),
    }


_DAY = re.compile(r"^\s*(\d{4})-(\d{2})-(\d{2})\s*$")
_WHEN = re.compile(r"^\s*(\d{4})-(\d{2})-(\d{2})[ T](\d{1,2}):(\d{2})\s*$")


def _parse_day(s: str) -> datetime:
    m = _DAY.match(s or "")
    if not m:
        raise RuntimeError(f"date must be YYYY-MM-DD, got '{s}'")
    try:
        return datetime(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    except ValueError as exc:
        # Shaped right but impossible ("2026-13-01", "2026-02-30"). Raised as a
        # RuntimeError like every other rejection here, so the calling tool
        # answers with a sentence instead of dying on a ValueError it never
        # thought to catch.
        raise RuntimeError(f"'{s}' is not a real date — {exc}") from exc


def _parse_when(s: str) -> datetime:
    """Local wall-clock time, given explicitly.

    Deliberately NOT a natural-language parser. "Thursday at 3" resolved by a
    library that disagrees with him about which Thursday books a real meeting in
    other people's calendars on the wrong day — so whoever calls this resolves the
    words first and passes an unambiguous timestamp.
    """
    m = _WHEN.match(s or "")
    if not m:
        raise RuntimeError(f"time must be 'YYYY-MM-DD HH:MM' (local), got '{s}'")
    y, mo, d, h, mi = (int(g) for g in m.groups())
    try:
        return datetime(y, mo, d, h, mi)
    except ValueError as exc:
        raise RuntimeError(f"'{s}' is not a real date/time — {exc}") from exc


def describe(invite: dict) -> str:
    """What he reads before approving — the facts, in the order he checks them."""
    if invite.get("all_day"):
        days = invite.get("days", 1)
        when = (f"{invite['start']:%a %d %b}" if days == 1
                else f"{invite['start']:%a %d %b} → {invite['end'] - timedelta(days=1):%a %d %b}"
                     f" ({days} days)")
        when += ", all day"
    else:
        when = f"{invite['start']:%a %d %b, %H:%M}–{invite['end']:%H:%M}"
    who = ", ".join(invite.get("attendees") or []) or "no attendees"
    return f"{invite['subject']}\n{when}\nTo: {who}"

