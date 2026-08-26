"""Self-health check — ping Arun ONLY when something breaks (or recovers).

Many moving parts (WhatsApp bridge, Telegram, Teams session, LM Studio, Copilot,
disk) fail silently; without this you'd discover a dead channel days later.
Runs every 6h + on demand. Transition-based: a problem notifies once when it
appears and once when everything is healthy again — no repeat nagging.

Deliberately cheap: the Teams check reads the kv the session watcher maintains
instead of launching a browser.
"""

from __future__ import annotations

import asyncio
import json
import shutil
import time

from . import quiet, attention, copilot_cli, daemon, diagnostics, memory, msnotify, store, teams_bridge, telegram

CHECK_SECONDS = 6 * 3600
MIN_FREE_GB = 5


#: Days of neglect before stale context is worth naming in a health check. Below
#: this a workspace is simply being worked in — code moves faster than the notes
#: about it, and saying so daily would be the noise this exists to replace.
CONTEXT_STALE_DAYS = 14


def stale_contexts(now: float | None = None) -> dict[str, float]:
    """workspace -> days since its context was last enriched (-1 = never)."""
    from . import refresh, workspace as ws_mod
    out: dict[str, float] = {}
    try:
        names = ws_mod.available_workspaces()
    except Exception:
        return out
    for name in names:
        days = refresh.stale_days(name, now)
        # "Never" is reported only once a workspace HAS context to be stale —
        # an unbootstrapped one is not broken, it is simply not set up yet.
        if days < 0:
            try:
                if not ws_mod.get(name) or not ws_mod.get(name).exists():
                    continue
            except Exception:
                continue
            out[name] = -1.0
        elif days >= CONTEXT_STALE_DAYS:
            out[name] = days
    return out


async def checks() -> dict[str, str]:
    """key -> problem description; empty dict = all healthy."""
    from . import agent, notify
    problems: dict[str, str] = {}
    try:
        wa = await notify.wa_status()
        if not wa.get("up"):
            problems["whatsapp"] = "bridge not running"
        elif not wa.get("paired"):
            problems["whatsapp"] = "not paired — scan the QR in Settings"
        elif not wa.get("enabled"):
            problems["whatsapp"] = "channel disabled in config"
    except Exception as exc:
        problems["whatsapp"] = f"bridge unreachable: {str(exc)[:60]}"
    tg = telegram.status()
    if tg["enabled"] and not tg["bound"]:
        problems["telegram"] = "token set but chat not bound — send /start <ASTA_TOKEN> to the bot"
    if teams_bridge.enabled() and teams_bridge.logged_in_once() \
            and store.kv_get("teams_session_ok") == "0":
        problems["teams"] = "session expired — run: python -m app.teams_bridge login"
    ms = await asyncio.to_thread(msnotify.status)
    if ms.get("enabled") and not ms.get("ok"):
        problems["teams_watcher"] = ms.get("reason", "notification DB unreadable")
    # A watcher that has stopped reading is the one failure that LOOKS like good
    # news. Both loops swallow exceptions and continue, so a permanently broken
    # selector is silent — and silence is also what a calm morning looks like.
    for source, minutes in attention.stale_sources().items():
        if attention.never_succeeded(source):
            # The worse case: running since startup and has NEVER read anything.
            # Not "it went quiet" — it has never worked, and every quiet poll
            # since looked exactly like good news.
            head = f"running for {minutes} min and has never once read successfully"
        else:
            head = f"no successful read for {minutes} min"
        why = attention.last_error(source)
        problems[f"{source}_watcher"] = (
            f"{head} — treat quiet as unknown, not as nothing happening"
            + (f" (last error: {why})" if why else ""))
    # Context that is quietly out of date is the same shape of failure as a dead
    # watcher: everything answers, nothing complains, and the answers are wrong.
    # A number here is what turns "it's probably fine" into a decision.
    for name, days in stale_contexts().items():
        problems[f"context_{name}"] = (
            "never enriched — every answer about this workspace is guesswork"
            if days < 0 else
            f"context last enriched {int(days)} days ago — say yes to the refresh offer")

    # A supervised loop cannot vanish any more, but it can still be failing on
    # every single pass — restarting, dying, restarting. That reads as "running"
    # to anything that only asks whether the task exists, which is exactly how
    # the Teams watcher managed to be dead without anything saying so.
    for line in daemon.problems():
        name, _, detail = line.partition(" ")
        problems[f"daemon_{name}"] = f"background loop {detail or 'is not running'}"

    if not copilot_cli.available():
        problems["copilot"] = "Copilot CLI missing/unauthenticated (run: copilot login)"
    if not memory.local_llm_model():
        # Understated before. The local model is not only a cheap digest writer:
        # it is the EMBEDDER that re-ranks memory recall, and the second opinion
        # that decides whether Asta may answer aloud in a call. Without it,
        # recall degrades to keyword matching — "vessel eta not updating" returns
        # a memory titled "WhatsApp" — and Asta stays silent in calls by design.
        problems["lmstudio"] = (
            "LM Studio not running — memory recall is keyword-only (no semantic "
            "re-ranking), Asta will not answer aloud in calls, and digests fall "
            "back to heuristics")
    free_gb = shutil.disk_usage("/").free / 1e9
    if free_gb < MIN_FREE_GB:
        problems["disk"] = f"only {free_gb:.1f} GB free"
    # Errors this process handled and moved past. Ninety-two places deliberately
    # ignore a failure, and nearly all are right to — but a selector that quietly
    # stopped matching, or a store write that quietly failed, degrades Asta with
    # no record anywhere. One site failing repeatedly is a fault, not noise.
    # A credential the provider has refused. Presence of a key was being treated
    # as a working key, so this failed silently everywhere it was used.
    for brain in ("claude", "openai"):
        if agent.key_rejected(brain):
            problems[f"{brain}-key"] = (
                "the API key is set but the provider REFUSED it — every paid call "
                "fails. Replace or remove it in .env; this clears itself when the "
                "key changes.")
    # A Temporal cert that EXISTS and cannot be used. The proxy checks only that
    # the file is there, so an empty one passes and dies inside TLS with "failed
    # to find any PEM data" — a sentence that names PEM parsing and not the empty
    # file. Cheap and local, so it runs on every pass.
    #
    # Deliberately NOT reporting envs with no cert at all: for an env he never
    # touches that is a choice, and four permanent "problems" is how a health
    # report becomes something people scroll past.
    for cert in diagnostics.broken_certs():
        problems[f"temporal-{cert['env']}"] = (
            f"cert is present but unusable — {cert['why']}. Debugging on this env "
            f"will fail inside TLS with an error that does not mention the file. "
            f"Fix: vault login -method=oidc, then "
            f"{diagnostics.FETCH_SCRIPT} {cert['env']}")
    for bad in quiet.loud():
        problems[f"repeated:{bad['where']}"] = (
            f"failed {bad['count']}x and was ignored each time — {bad['error'][:70]}")
    return problems


# --- things he has told Asta to stop reporting ---------------------------------
#
# The check was transition-based, which handles "don't repeat yourself" but not
# "I know, and I am not fixing it". A key he has ruled out — an API key he has
# deliberately abandoned, a workspace he is not using this month — was reported
# again on every set change, and then chased. Saying "ignore it" had nowhere to go.
#
# A mute is scoped to the FAULT, not to the key, and that is the safety property:
# it is forgotten the moment the problem actually clears, so the same key
# breaking again next month is fresh news rather than something a stale mute
# swallows. Muting is never silent either — `report_text` still lists them, so
# "why didn't you tell me" always has an answer.

MUTED_KEY = "health_muted"


def muted() -> dict[str, str]:
    """key -> the problem text as it read when he silenced it."""
    try:
        raw = json.loads(store.kv_get(MUTED_KEY) or "{}")
    except ValueError:
        return {}
    return {str(k): str(v) for k, v in raw.items()} if isinstance(raw, dict) else {}


def resolve_key(name: str, keys) -> str:
    """Match what he typed to a real health key — exact, then unique substring.

    Loose about how he says it ("claude key", "the claude-key"), strict about
    what it names: an ambiguous or unknown name resolves to nothing, so a mute
    can never silence a key he did not mean.
    """
    want = (name or "").strip().lower().replace(" ", "-").strip("-.:")
    if not want:
        return ""
    keys = list(keys)
    if want in keys:
        return want
    hit = [k for k in keys if want in k.lower() or k.lower() in want]
    return hit[0] if len(hit) == 1 else ""


def mute(key: str, problem: str = "") -> None:
    store.kv_set(MUTED_KEY, json.dumps({**muted(), key: problem}))


def unmute(key: str) -> bool:
    current = muted()
    if key not in current:
        return False
    current.pop(key)
    store.kv_set(MUTED_KEY, json.dumps(current))
    return True


def drop_spent_mutes(problems: dict[str, str]) -> list[str]:
    """Forget mutes whose fault has gone. Returns what was forgotten.

    This is what stops "ignore it" from becoming a permanent blind spot: the
    silence lasts exactly as long as the thing he silenced.
    """
    spent = [k for k in muted() if k not in problems]
    for key in spent:
        unmute(key)
    return spent


def report_text(problems: dict[str, str]) -> str:
    if not problems:
        return "All systems healthy ✓ (WhatsApp, Telegram, Teams, Copilot, LM Studio, disk)"
    quiet_keys = muted()
    return "Health check — issues found:\n" + "\n".join(
        f"• {k}: {v}" + (" (muted — say 'unmute " + k + "' to hear about it again)"
                         if k in quiet_keys else "")
        for k, v in problems.items())


async def run_check(notify_transitions: bool = True) -> dict[str, str]:
    """One health pass; notifies on newly-broken / newly-recovered when asked."""
    from . import notify
    problems = await checks()
    # A mute is spent once its fault clears, so this runs against the REAL
    # problem set every pass — before anything is filtered out of the report.
    drop_spent_mutes(problems)
    prev = set(json.loads(store.kv_get("health_problems") or "[]"))
    cur = set(problems)
    store.kv_set("health_problems", json.dumps(sorted(cur)))
    # The wording too, not only the keys: "ignore <k>" records what the fault
    # SAID when he silenced it, and a bare key is not enough to answer "why is
    # this muted?" a month later.
    store.kv_set("health_problems_detail", json.dumps(problems))
    store.kv_set("health_last_run", str(time.time()))
    if notify_transitions:
        # Muted keys are still tracked in `cur` above, deliberately: they must
        # keep counting as problems, or a run where the only fault left is a
        # muted one would announce "all systems healthy again" — which is false,
        # and the exact kind of reassuring lie this module exists to prevent.
        new = cur - prev - set(muted())
        if new:
            await notify.notify(
                "🩺 " + report_text({k: problems[k] for k in sorted(new)}), "health")
        elif prev and not cur:
            await notify.notify("🩺 All systems healthy again ✓", "health")
    return problems


async def loop() -> None:
    await asyncio.sleep(120)  # let bridges/watchers settle after startup
    while True:
        try:
            await run_check()
        except Exception:
            pass
        await asyncio.sleep(CHECK_SECONDS)
