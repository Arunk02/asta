"""The Solar door — signing in, and what Asta is allowed to touch.

Round 3. From the plan: *"Solar has a duplicate page for any booking. Last week
Asta found it for a colleague's request, then had to stop because it has no way
to drive the page. A Solar door: duplicate, submit, read back the new reference,
then trace it through logs and Temporal. Test environments only."*

This module is the part that has to exist before any of that: getting signed in,
and fencing where it may go. Driving the duplicate page comes after, and it will
be written against the real page rather than guessed at — a form filled from
imagination is the kind of automation that submits something wrong confidently.

**Signing in is his, once.** Same shape as the Teams bridge, and the same
persistent Chromium profile, so if Solar sits behind the same corporate SSO the
session may already be there and there is nothing for him to do. He completes
any SSO himself, in a visible window; Asta never types a password and never
handles an MFA prompt.

**The fence is on what may be DONE, not on which environments exist.** The first
version refused production outright. That was the wrong shape: he reads
production to debug — his own account has no write access there — and blocking
it took away something real to prevent something that was never possible.

So every environment he names may be READ, and only environments he marks
writable may be written to. `ASTA_SOLAR_WRITE` is that list, production can
never be in it whatever it is set to, and `duplicate`/`submit` ask `writable()`
rather than `allowed()`. An assistant that can duplicate a booking is an
assistant that can duplicate a real customer's booking; the answer to that is a
read-only production, not an invisible one.

**His URLs are not in this repo.** They live in `.env`, which is gitignored, and
nothing here logs or echoes them — they are not in `.env.example`, not in a
test, and not in any notification.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import re
import sys
from urllib.parse import urlparse

#: Where Solar lives, per environment: "dev=https://…,sit=https://…". Asta has
#: never been told these — they come from him, and nothing is guessed.
#: Read as a literal, not through the constant: the test that checks every
#: documented setting is actually used scans for `os.environ.get("NAME")`, and a
#: setting reached through a variable looks to it like one nothing reads.
ENVS_SETTING = "ASTA_SOLAR_ENVS"

#: Environments that may be WRITTEN to: "sit,uat". Never production, whatever
#: this says — see `writable`.
WRITE_SETTING = "ASTA_SOLAR_WRITE"

#: Production, however it is spelled. It may be read; it may never be written.
_PRODUCTION = re.compile(r"\b(prod|production|live)\b", re.I)


def envs() -> dict[str, str]:
    """{env: base url} Asta may open — every environment he named, production
    included, because reading production is how a live problem gets debugged."""
    out: dict[str, str] = {}
    for part in (os.environ.get("ASTA_SOLAR_ENVS", "") or "").split(","):
        name, _, url = part.partition("=")
        name, url = name.strip().lower(), url.strip()
        if name and url:
            out[name] = url.rstrip("/")
    return out


def allowed(url: str) -> bool:
    """May Asta OPEN this URL? True for any environment he named."""
    try:
        host = (urlparse(url).netloc or "").lower()
    except Exception:                                           # noqa: BLE001
        return False
    if not host:
        return False
    return any(host == urlparse(base).netloc.lower() for base in envs().values())


def writable(env_or_url: str) -> bool:
    """May Asta CHANGE anything here? Only where he said so, and never in prod.

    Production is excluded in code rather than by leaving it out of a list,
    because the list is his to edit and this rule is not: a duplicate submitted
    against production is a real booking for a real customer, and no setting
    should be able to authorise that by accident.
    """
    named = (env_or_url or "").strip().lower()
    if not named:
        return False
    env = named
    if "://" in named:
        host = (urlparse(named).netloc or "").lower()
        env = next((e for e, base in envs().items()
                    if urlparse(base).netloc.lower() == host), "")
        if not env:
            return False
    if _PRODUCTION.search(env) or _PRODUCTION.search(envs().get(env, "")):
        return False
    wanted = {x.strip().lower() for x in
              (os.environ.get("ASTA_SOLAR_WRITE", "") or "").split(",") if x.strip()}
    return env in wanted and env in envs()


def base_for(env: str) -> str:
    """The base URL for an environment name, or '' if he did not allow it."""
    return envs().get((env or "").strip().lower(), "")


def configured() -> str:
    """'' when Solar is usable, otherwise what is missing, in his words."""
    if not envs():
        return (f"Solar is not configured — set {ENVS_SETTING} in .env as "
                f"name=url pairs. Any environment there can be READ; only those "
                f"named in {WRITE_SETTING} can be changed, and production never "
                f"can whatever it says.")
    return ""


async def login(env: str = "") -> str:
    """Open Solar in a visible window so he can complete the SSO himself.

    Reuses the Teams bridge's persistent profile on purpose: one browser
    identity, one place his corporate cookies live, and if Solar is behind the
    same SSO he may find he is already signed in and has nothing to do.
    """
    missing = configured()
    if missing:
        return missing
    known = envs()
    name = (env or next(iter(known))).lower()
    url = known.get(name, "")
    if not url:
        return f"'{name}' is not in {ENVS_SETTING} — allowed: {', '.join(known) or '(none)'}"
    from . import store, teams_bridge
    print(f"Opening Solar {name} at {url} — complete any sign-in in the window.")
    print(f"Profile: {teams_bridge.PROFILE_DIR}")
    pw, ctx = await teams_bridge._launch(headless=False)
    try:
        page = ctx.pages[0] if ctx.pages else await ctx.new_page()
        await page.goto(url, wait_until="domcontentloaded", timeout=60000)
        print("Waiting up to 5 minutes. Close nothing — press Ctrl-C when you are done.")
        with contextlib.suppress(Exception):
            await page.wait_for_timeout(300000)
        landed = page.url
        signed = "microsoftonline" not in landed and "/login" not in landed.lower()
        store.kv_set(f"solar_session:{name}", "1" if signed else "")
        return (f"Solar {name}: signed in, session saved." if signed
                else f"Solar {name}: still on a sign-in page ({landed[:80]}) — not saved.")
    finally:
        await ctx.close()
        await pw.stop()


def health() -> dict:
    """What Solar can and cannot do right now."""
    from . import store
    known = envs()
    # Names only. His URLs stay in .env and are not echoed into a health check,
    # a log line or a notification.
    return {"configured": bool(known), "environments": sorted(known),
            "writable": sorted(e for e in known if writable(e)),
            "read_only": sorted(e for e in known if not writable(e)),
            "signed_in": sorted(e for e in known if store.kv_get(f"solar_session:{e}")),
            "hint": configured() or "ready"}


if __name__ == "__main__":
    from . import store
    store.init()
    cmd = sys.argv[1] if len(sys.argv) > 1 else "health"
    if cmd == "login":
        print(asyncio.run(login(sys.argv[2] if len(sys.argv) > 2 else "")))
    else:
        import json
        print(json.dumps(health(), indent=1))
