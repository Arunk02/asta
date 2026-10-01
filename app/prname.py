"""A PR is named with its service: "booking PR 1459", never "PR 1459".

His suggestion, 1 Oct, after #1459 was reviewed in the wrong project — the same
number exists in empv3. A number alone names nothing; the service and the number
together name exactly one PR, for him reading it and for Asta finding it again.

One table of short names (the ones he uses), overridable with
ASTA_REPO_ALIASES="telikos-booking-service=booking,…". Anything not in it gets
its repo name without the "telikos-" and "-service" around it.
"""
from __future__ import annotations

import os
import re

_DEFAULT = {
    "telikos-booking-service": "booking",
    "telikos-activityplanworkflow-service": "AP",
    "telikos-email-service": "email",
}

_URL = re.compile(r"github\.com/([\w.-]+)/([\w.-]+)/pull/(\d+)", re.I)


def aliases() -> dict[str, str]:
    out = dict(_DEFAULT)
    for pair in (os.environ.get("ASTA_REPO_ALIASES") or "").split(","):
        repo, _, short_name = pair.partition("=")
        if repo.strip() and short_name.strip():
            out[repo.strip()] = short_name.strip()
    return out


def short(repo: str) -> str:
    """"telikos-booking-service" → "booking"; "Maersk-Global/x-service" → "x"."""
    name = (repo or "").strip().split("/")[-1]
    known = aliases()
    if name in known:
        return known[name]
    trimmed = re.sub(r"^telikos-|-service$", "", name)
    return trimmed or name


def label(repo: str, number: int | str) -> str:
    """"booking PR 1459"."""
    return f"{short(repo)} PR {str(number).lstrip('#')}"


def from_url(url: str) -> str:
    """"booking PR 1459" for a PR link, or "" when it is not one."""
    m = _URL.search(url or "")
    return label(m.group(2), m.group(3)) if m else ""


def name_links(text: str) -> str:
    """Put "booking PR 1459 — " in front of every bare PR link in a line of text."""
    def _one(m: re.Match) -> str:
        url = m.group(0)
        return f"{from_url(url)} — {url}"
    return re.sub(r"https?://github\.com/[\w.-]+/[\w.-]+/pull/\d+", _one, text or "")


def repo_for(alias: str) -> str:
    """"booking" / "AP" / "email" → the repo it stands for, or ""."""
    want = (alias or "").strip().lower()
    for repo, short_name in aliases().items():
        if short_name.lower() == want:
            return repo
    return ""


_NAMED = re.compile(r"\b([A-Za-z][\w-]{1,30})\s+PR\s*#?(\d{1,7})\b", re.I)


def parse(text: str) -> tuple[str, str]:
    """("telikos-booking-service", "1459") from "booking PR 1459", or ("", "")."""
    for m in _NAMED.finditer(text or ""):
        repo = repo_for(m.group(1))
        if repo:
            return repo, m.group(2)
    return "", ""
