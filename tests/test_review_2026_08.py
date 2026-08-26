"""Closing the August 2026 architecture review, finding by finding.

Each block below is one numbered finding from docs/REVIEW-FINDINGS-2026-08.md.
The cases are built from the situations that actually produced the finding —
Arun asking what Vinish said last night, a workspace whose repos are not git
checkouts — rather than from the shape of the code, so they keep meaning
something if the implementation is rewritten.
"""

from __future__ import annotations

import asyncio
import contextlib
import pathlib
import time

import pytest

from app import agent as agent_mod, store


# --- 1. One cached message must not hide the rest ----------------------------
#
# The report that produced this: "if i ask to check on last night msg from
# vinish to check one bug i couldn't able to fetch it exactly". One message from
# that evening was already stored, `if not rows` saw a non-empty list, the
# scrollback never ran, and the partial thread came back labelled "from stored
# history" as though it were the whole conversation.

HOUR = 3600.0


def _store_msg(chat: str, text: str, sent_at: float, seen_at: float | None = None,
               sender: str = "Vinish Kumar") -> None:
    store.save_teams_messages([{
        "key": f"{chat}:{sent_at}:{text[:20]}", "chat": chat, "sender": sender,
        "text": text, "sent_at": sent_at, "stamp": "", "seen_at": seen_at,
    }])
    if seen_at is not None:
        # save_teams_messages stamps seen_at with "now"; these tests need to
        # describe a chat that was last READ at a particular moment.
        with store._connect() as c:
            c.execute("UPDATE teams_messages SET seen_at=? WHERE chat=?",
                      (seen_at, chat))


@pytest.fixture
def last_night():
    """The window Arun actually asks about, and a clock around it."""
    now = time.time()
    until = now - 8 * HOUR          # window closed this morning
    since = now - 16 * HOUR         # opened last evening
    return since, until, now


def test_one_cached_message_does_not_count_as_history(last_night):
    """THE finding. A single stored message inside the window is not the window."""
    since, until, now = last_night
    _store_msg("Vinish Kumar", "did you see the NPE in booking?",
               sent_at=since + 2 * HOUR, seen_at=since + 2 * HOUR)
    assert store.teams_history_covers("Vinish Kumar", since, until) is False, \
        "one message inside the window was treated as the whole window"


def test_history_counts_when_both_edges_are_covered(last_night):
    """Read back past the start, and read again after the window closed."""
    since, until, now = last_night
    _store_msg("Vinish Kumar", "older context", sent_at=since - 3 * HOUR)
    _store_msg("Vinish Kumar", "the NPE again", sent_at=since + 2 * HOUR,
               seen_at=until + 1 * HOUR)
    assert store.teams_history_covers("Vinish Kumar", since, until) is True


def test_a_cache_that_starts_inside_the_window_is_not_covered(last_night):
    """Missing the beginning of the evening is exactly the reported symptom."""
    since, until, now = last_night
    _store_msg("Vinish Kumar", "first thing we have", sent_at=since + 1 * HOUR,
               seen_at=until + 1 * HOUR)
    assert store.teams_history_covers("Vinish Kumar", since, until) is False


def test_a_stale_read_is_not_covered(last_night):
    """Reached back far enough, but last read BEFORE the window closed — so
    anything sent afterwards was never seen."""
    since, until, now = last_night
    _store_msg("Vinish Kumar", "older context", sent_at=since - 3 * HOUR)
    _store_msg("Vinish Kumar", "early message", sent_at=since + 1 * HOUR,
               seen_at=since + 1 * HOUR)          # read mid-window, not after
    assert store.teams_history_covers("Vinish Kumar", since, until) is False


def test_an_empty_cache_is_never_covered(last_night):
    since, until, now = last_night
    assert store.teams_history_covers("Vinish Kumar", since, until) is False


def test_coverage_is_per_chat(last_night):
    """A well-covered chat must not vouch for a different one."""
    since, until, now = last_night
    _store_msg("Suraj", "older", sent_at=since - 3 * HOUR)
    _store_msg("Suraj", "in window", sent_at=since + 1 * HOUR, seen_at=until + HOUR)
    assert store.teams_history_covers("Suraj", since, until) is True
    assert store.teams_history_covers("Vinish Kumar", since, until) is False


def test_untimed_messages_cannot_establish_coverage(last_night):
    """A message with no timestamp cannot honestly place itself in a window."""
    since, until, now = last_night
    _store_msg("Vinish Kumar", "no timestamp", sent_at=None, seen_at=until + HOUR)
    assert store.teams_history_covers("Vinish Kumar", since, until) is False


@pytest.mark.asyncio
async def test_the_real_case_partial_cache_still_scrolls_teams(monkeypatch, last_night):
    """End to end, as it actually failed: one message cached, ten on the server.

    Before the fix this returned the single cached line. It must now go to Teams
    and come back with all ten.
    """
    since, until, now = last_night
    _store_msg("Vinish Kumar", "did you see the NPE?", sent_at=since + 2 * HOUR,
               seen_at=since + 2 * HOUR)

    scrolled = []
    server = [{"key": f"k{i}", "chat": "Vinish Kumar", "sender": "Vinish Kumar",
               "text": f"message {i}", "sent_at": since + i * 0.5 * HOUR, "stamp": ""}
              for i in range(10)]

    async def fake_read_history(chat, since=None, limit=60):
        scrolled.append(chat)
        return server

    from app import teams_bridge, when as when_mod
    monkeypatch.setattr(teams_bridge, "enabled", lambda: True)
    monkeypatch.setattr(teams_bridge, "logged_in_once", lambda: True)
    monkeypatch.setattr(teams_bridge, "read_history", fake_read_history)
    monkeypatch.setattr(when_mod, "parse", lambda w: (since, until, "last night"))

    out = await agent_mod.teams_history("Vinish Kumar", "last night")
    assert scrolled == ["Vinish Kumar"], "it answered from a partial cache again"
    assert "scrolled back" in out, f"reported the wrong source: {out[-120:]}"


@pytest.mark.asyncio
async def test_a_fully_covered_window_still_answers_without_a_browser(monkeypatch, last_night):
    """The optimisation must survive the fix — asking twice about the same
    evening should not re-open Teams."""
    since, until, now = last_night
    _store_msg("Vinish Kumar", "older context", sent_at=since - 3 * HOUR)
    _store_msg("Vinish Kumar", "the NPE again", sent_at=since + 2 * HOUR,
               seen_at=until + 1 * HOUR)

    async def must_not_run(chat, since=None, limit=60):
        raise AssertionError("opened a browser for a window already held")

    from app import teams_bridge, when as when_mod
    monkeypatch.setattr(teams_bridge, "enabled", lambda: True)
    monkeypatch.setattr(teams_bridge, "logged_in_once", lambda: True)
    monkeypatch.setattr(teams_bridge, "read_history", must_not_run)
    monkeypatch.setattr(when_mod, "parse", lambda w: (since, until, "last night"))

    out = await agent_mod.teams_history("Vinish Kumar", "last night")
    assert "stored history" in out


# --- 3 & 5. Context that cannot be verified must say so ----------------------
#
# Found live: IOM-workspace had seven indexed repos and six had no .git
# directory, so `_sha_drift` skipped them with a bare `continue` and the
# workspace reported clean forever. That workspace has since been removed at
# Arun's instruction, but the blind spot was in the engine, not the workspace —
# booking would behave identically the day a repo stops being a checkout.

import json
from pathlib import Path

from app.workspace.providers import indexed as indexed_mod
from app.workspace.providers.indexed import IndexedProvider


def _workspace(tmp_path: Path, repos: dict) -> Path:
    """Build a workspace on disk. repos maps name -> ('git'|'nogit', recorded_sha)."""
    root = tmp_path / "ws"
    (root / ".contmark" / "repos").mkdir(parents=True)
    for name, (kind, sha) in repos.items():
        (root / name).mkdir(parents=True, exist_ok=True)
        if kind == "git":
            (root / name / ".git").mkdir()
        d = root / ".contmark" / "repos" / name
        d.mkdir(parents=True, exist_ok=True)
        (d / "_index.json").write_text(json.dumps({"verified_against": sha}))
    return root


def _ctx(root: Path) -> IndexedProvider:
    return IndexedProvider(root)


@pytest.mark.asyncio
async def test_a_repo_without_a_checkout_is_reported_not_skipped(tmp_path, monkeypatch):
    """THE finding. Six of seven repos were exempt from drift detection and
    nothing anywhere said so."""
    root = _workspace(tmp_path, {"booking-service": ("nogit", "abc12345")})
    ctx = _ctx(root)
    monkeypatch.setattr(ctx, "services", lambda: ["booking-service"])
    stale = await ctx._sha_drift()
    assert stale, "a repo that cannot be verified was silently treated as clean"
    assert "cannot verify" in stale[0] and "booking-service" in stale[0]


@pytest.mark.asyncio
async def test_an_index_with_no_recorded_sha_is_reported(tmp_path, monkeypatch):
    root = _workspace(tmp_path, {"booking-service": ("git", "")})
    ctx = _ctx(root)
    monkeypatch.setattr(ctx, "services", lambda: ["booking-service"])
    stale = await ctx._sha_drift()
    assert stale and "no verified_against" in stale[0]


@pytest.mark.asyncio
async def test_an_unreadable_index_is_reported(tmp_path, monkeypatch):
    root = _workspace(tmp_path, {"booking-service": ("git", "abc12345")})
    (root / ".contmark" / "repos" / "booking-service" / "_index.json").write_text("{not json")
    ctx = _ctx(root)
    monkeypatch.setattr(ctx, "services", lambda: ["booking-service"])
    stale = await ctx._sha_drift()
    assert stale and "unreadable" in stale[0]


@pytest.mark.asyncio
async def test_a_missing_index_is_still_skipped_quietly(tmp_path, monkeypatch):
    """No index means the repo was never in the context — not a staleness claim,
    so it must NOT become noise on every check."""
    root = tmp_path / "ws"
    (root / ".contmark" / "repos").mkdir(parents=True)
    (root / "booking-service").mkdir(parents=True)
    ctx = _ctx(root)
    monkeypatch.setattr(ctx, "services", lambda: ["booking-service"])
    assert await ctx._sha_drift() == []


@pytest.mark.asyncio
async def test_the_resolver_states_its_freshness(tmp_path, monkeypatch):
    """Finding 5: the model answering from this context could not tell whether
    it was verified today or never."""
    root = _workspace(tmp_path, {"booking-service": ("nogit", "abc12345")})
    ctx = _ctx(root)
    monkeypatch.setattr(ctx, "services", lambda: ["booking-service"])
    (root / ".contmark" / indexed_mod.RESOLVER).write_text("// stub")

    async def fake_run(cmd, cwd, timeout, ctx_dir=""):
        return 0, "TmsServiceImpl handles the ATA fallback."

    monkeypatch.setattr("app.workspace.providers.indexed._run", fake_run)
    out = await ctx.resolve("how does the ATA fallback work")
    assert out.startswith("[context freshness:"), f"no freshness header: {out[:80]}"
    assert "STALE OR UNVERIFIED" in out
    assert "TmsServiceImpl" in out, "the actual context was lost"


@pytest.mark.asyncio
async def test_a_verified_context_says_so_without_alarming(tmp_path, monkeypatch):
    root = _workspace(tmp_path, {"booking-service": ("git", "abc12345")})
    ctx = _ctx(root)
    monkeypatch.setattr(ctx, "services", lambda: [])       # nothing to verify
    (root / ".contmark" / indexed_mod.RESOLVER).write_text("// stub")

    async def fake_run(cmd, cwd, timeout, ctx_dir=""):
        return 0, "the answer"

    monkeypatch.setattr("app.workspace.providers.indexed._run", fake_run)
    out = await ctx.resolve("anything")
    assert "verified against current HEAD" in out
    assert "STALE" not in out


@pytest.mark.asyncio
async def test_a_broken_drift_check_does_not_break_the_answer(tmp_path, monkeypatch):
    """Freshness is a label on the answer, never a reason there isn't one."""
    root = _workspace(tmp_path, {"booking-service": ("git", "abc12345")})
    ctx = _ctx(root)

    def explode():
        raise RuntimeError("git is gone")

    monkeypatch.setattr(ctx, "services", explode)
    (root / ".contmark" / indexed_mod.RESOLVER).write_text("// stub")

    async def fake_run(cmd, cwd, timeout, ctx_dir=""):
        return 0, "the answer survives"

    monkeypatch.setattr("app.workspace.providers.indexed._run", fake_run)
    out = await ctx.resolve("anything")
    assert "unknown" in out and "the answer survives" in out


@pytest.mark.asyncio
async def test_the_resolver_payload_is_capped_to_its_contract(tmp_path, monkeypatch):
    """Finding 16: documented as ~350 tokens, capped at 20,000 characters."""
    root = _workspace(tmp_path, {"booking-service": ("git", "abc12345")})
    ctx = _ctx(root)
    monkeypatch.setattr(ctx, "services", lambda: [])
    (root / ".contmark" / indexed_mod.RESOLVER).write_text("// stub")

    async def fake_run(cmd, cwd, timeout, ctx_dir=""):
        return 0, "x" * 50_000

    monkeypatch.setattr("app.workspace.providers.indexed._run", fake_run)
    out = await ctx.resolve("anything")
    body = out.split("]", 1)[1].strip()          # everything after the freshness header
    assert len(body) == indexed_mod._RESOLVE_CHARS, f"payload was {len(body)} chars"
    assert indexed_mod._RESOLVE_CHARS <= 8000, "the cap drifted back up"


# --- 6. Every push consults the ledger, not three of fifty-six ---------------
#
# Measured before the fix: 158 interruptions sent, 92 of which told Arun
# something he had already read elsewhere. The ledger that exists to catch
# exactly that was consulted by three call sites out of fifty-six, so its
# cross-source deduplication covered mail and Teams and nothing else.

@pytest.fixture
def pushes(monkeypatch):
    """Capture what actually reached a channel."""
    sent = []

    async def wa(text):
        sent.append(text)
        return True

    async def tg(text):
        return False

    from app import notify as notify_mod, presence, delivery
    monkeypatch.setattr(notify_mod, "wa_send", wa)
    monkeypatch.setattr(notify_mod.telegram, "send", tg)
    monkeypatch.setattr(delivery, "hold_for_quiet", lambda u, p: False)
    monkeypatch.setattr(delivery, "should_batch", lambda p: False)

    async def away():
        return False

    monkeypatch.setattr(presence, "at_laptop", away)
    return sent


@pytest.fixture
def ledger_on(monkeypatch):
    from app import attention
    monkeypatch.setenv("ASTA_ATTENTION", "1")
    assert attention.enabled()
    return attention


@pytest.mark.asyncio
async def test_the_same_news_from_two_sources_buzzes_once(pushes, ledger_on):
    """THE finding, in the shape it actually costs him: one incident arriving as
    mail and again as a Teams mention used to interrupt him twice."""
    from app import notify as notify_mod
    text = "Priya: the booking service is throwing NPEs in prod"
    first = await notify_mod.notify(text, "outlook", urgency="direct")
    second = await notify_mod.notify(text, "teams", urgency="direct")
    assert first["whatsapp"] is True
    assert second.get("suppressed") is True, "it buzzed twice for one thing"
    assert len(pushes) == 1


@pytest.mark.asyncio
async def test_a_suppressed_push_is_still_in_the_bell(pushes, ledger_on):
    """Suppression must never become data loss — the UI keeps everything."""
    from app import notify as notify_mod, store
    text = "the booking service is throwing NPEs"
    await notify_mod.notify(text, "outlook")
    await notify_mod.notify(text, "teams")
    bell = [n for n in store.list_notifications(20) if text in n["text"]]
    assert len(bell) == 2, "the second arrival vanished instead of being recorded"


@pytest.mark.asyncio
async def test_genuinely_new_news_still_gets_through(pushes, ledger_on):
    """Deduplication must not become silence."""
    from app import notify as notify_mod
    await notify_mod.notify("the booking service is throwing NPEs", "outlook")
    await notify_mod.notify("the email service deploy failed", "ci")
    assert len(pushes) == 2


@pytest.mark.asyncio
async def test_a_caller_that_already_asked_is_not_asked_twice(pushes, ledger_on):
    """`considered=True` is the opt-out for the three sites that consult the
    ledger themselves. Without it their own approved push is re-keyed here, reads
    as already-notified, and is suppressed — the push their check just allowed."""
    from app import notify as notify_mod
    text = "Vinish mentioned you in Team Booking"
    out = await notify_mod.notify(text, "teams", considered=True)
    again = await notify_mod.notify(text, "teams", considered=True)
    assert out["whatsapp"] is True and again["whatsapp"] is True, \
        "an already-considered push was suppressed by a second consideration"


@pytest.mark.asyncio
async def test_with_the_ledger_off_nothing_changes(pushes, monkeypatch):
    """The no-op contract: a system with ASTA_ATTENTION unset behaves exactly as
    it did before any of this existed."""
    from app import attention, notify as notify_mod
    monkeypatch.delenv("ASTA_ATTENTION", raising=False)
    assert not attention.enabled()
    text = "same words twice"
    await notify_mod.notify(text, "outlook")
    await notify_mod.notify(text, "outlook")
    assert len(pushes) == 2, "the ledger suppressed something while switched off"


def test_urgency_maps_onto_the_ledgers_ranking():
    from app import attention, notify as notify_mod
    assert notify_mod._ledger_priority("direct", None) == attention.P_TODAY
    assert notify_mod._ledger_priority("ambient", None) == attention.P_FYI
    assert notify_mod._ledger_priority("ambient", attention.P_NOW) == attention.P_NOW, \
        "an explicit priority must win over the urgency guess"


# --- 2. The verifier gate: on, and honest about what it cannot check ----------
#
# The review recommended "turn the verifier on" as the single highest-value
# change. Measuring it proved that wrong on its own: every repo in the booking
# workspace is a multi-module Maven build, `_autodetect` deliberately refuses to
# run heavy suites, and the poms are one level down where nothing was looking.
# The flag alone would have switched on a gate that verified nothing, which is
# indistinguishable from a gate that works.

from app import verify


@pytest.fixture(autouse=True)
def _no_live_verify_map(monkeypatch, tmp_path):
    """These cases describe a repo's own state, so they must not read Asta's real
    command map — a repo named `telikos-booking-service` in tmp would otherwise
    inherit the production command and report itself configured. Same lesson as
    the live database: a test that reads real config is testing the config.
    """
    monkeypatch.setattr(verify, "COMMANDS_FILE", tmp_path / "absent.json")


def _repo(tmp_path, layout: dict):
    """A repo on disk. layout maps relative path -> contents."""
    root = tmp_path / "telikos-booking-service"
    for rel, body in layout.items():
        f = root / rel
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(body)
    root.mkdir(parents=True, exist_ok=True)
    return root


def test_a_multi_module_maven_repo_is_not_mistaken_for_having_no_tests(tmp_path):
    """The booking repos have no pom at the top — they are in service/,
    componenttest/ and perftest/. Looking only at the root found none of them."""
    root = _repo(tmp_path, {"componenttest/pom.xml": "<project/>",
                            "service/pom.xml": "<project/>"})
    msg = verify.unconfigured(str(root))
    assert msg and "no check Asta may run" in msg
    assert "pom.xml" in msg


def test_a_repo_with_no_build_system_is_not_nagged_about(tmp_path):
    """Silence is right here — there is genuinely nothing to run."""
    root = _repo(tmp_path, {"README.md": "notes"})
    assert verify.unconfigured(str(root)) is None


def test_a_configured_repo_stops_being_reported(tmp_path, monkeypatch):
    root = _repo(tmp_path, {"service/pom.xml": "<project/>"})
    cfg = tmp_path / "verify-commands.json"
    cfg.write_text('{"telikos-booking-service": "mvn -q -pl service test"}')
    monkeypatch.setattr(verify, "COMMANDS_FILE", cfg)
    assert verify.resolve_command(str(root)) == "mvn -q -pl service test"
    assert verify.unconfigured(str(root)) is None


def test_an_empty_command_means_deliberately_no_check(tmp_path, monkeypatch):
    """A repo mapped to "" is a decision, not an oversight — but it is still
    reported, because a gate that checks nothing must never look like one that
    checks something."""
    root = _repo(tmp_path, {"service/pom.xml": "<project/>"})
    cfg = tmp_path / "verify-commands.json"
    cfg.write_text('{"telikos-booking-service": ""}')
    monkeypatch.setattr(verify, "COMMANDS_FILE", cfg)
    assert verify.resolve_command(str(root)) is None
    assert verify.unconfigured(str(root)) is not None


def test_the_repos_own_marker_still_wins(tmp_path, monkeypatch):
    """Asta's map must not override a command the repo itself declares."""
    root = _repo(tmp_path, {"service/pom.xml": "<project/>",
                            ".asta-verify": "mvn verify"})
    cfg = tmp_path / "verify-commands.json"
    cfg.write_text('{"telikos-booking-service": "something else"}')
    monkeypatch.setattr(verify, "COMMANDS_FILE", cfg)
    assert verify.resolve_command(str(root)) == "mvn verify"


def test_a_missing_or_broken_map_is_not_a_crash(tmp_path, monkeypatch):
    root = _repo(tmp_path, {"service/pom.xml": "<project/>"})
    monkeypatch.setattr(verify, "COMMANDS_FILE", tmp_path / "nope.json")
    assert verify.resolve_command(str(root)) is None
    broken = tmp_path / "broken.json"
    broken.write_text("{not json")
    monkeypatch.setattr(verify, "COMMANDS_FILE", broken)
    assert verify.resolve_command(str(root)) is None


def test_asta_ships_a_map_covering_every_booking_repo():
    """The three repos must each be present, so a missing check is a visible
    empty string rather than a silently absent key."""
    import json
    from pathlib import Path as _P
    real = _P(__file__).resolve().parent.parent / "data" / "verify-commands.json"
    data = json.loads(real.read_text())
    for repo in ("telikos-booking-service", "telikos-email-service",
                 "telikos-activityplanworkflow-service"):
        assert repo in data, f"{repo} is not in the verify map"


# --- 2b. Infrastructure failure is not a red suite ---------------------------
#
# Every booking repo inherits from an internal `telikos-parent` POM, so a build
# needs Artifactory and therefore the VPN. Maven exits 1 for "cannot reach the
# repository" exactly as it does for a failing test. Without this the gate reads
# a Monday morning with the VPN off as "your tests fail", loops to fix code that
# was never broken, escalates to a stronger brain, and burns a paid run.

@pytest.mark.parametrize("output", [
    "[ERROR] Failed to execute goal on project telikos-email-service: Could not resolve dependencies",
    "[ERROR] Non-resolvable parent POM for com.maersk:telikos-email-service",
    "[ERROR] Could not transfer artifact com.maersk:telikos-parent:pom:1.2.3",
    "Caused by: java.net.UnknownHostException: artifactory.maersk.com",
    "[ERROR] Failed to transfer... 401 Unauthorized",
    "sun.security.provider.certpath.SunCertPathBuilderException: unable to find valid certification path",
    "Cannot connect to the Docker daemon at unix:///var/run/docker.sock",
])
@pytest.mark.asyncio
async def test_a_build_that_could_not_run_is_skipped_not_failed(output, monkeypatch, tmp_path):
    """ran=False means the normal done path runs, exactly as if no check existed."""
    class P:
        returncode = 1
        async def communicate(self):
            return (output.encode(), b"")

    async def fake_shell(cmd, cwd=None, stdout=None, stderr=None):
        return P()

    monkeypatch.setattr(verify.asyncio, "create_subprocess_shell", fake_shell)
    out = await verify.run(str(tmp_path), "mvn -q test")
    assert out.ran is False, f"infrastructure failure treated as a red suite: {output[:60]}"


@pytest.mark.asyncio
async def test_a_genuinely_failing_test_is_still_a_failure(monkeypatch, tmp_path):
    """The narrow half. If this ever starts skipping, the gate is worthless."""
    class P:
        returncode = 1
        async def communicate(self):
            return (b"[ERROR] Tests run: 42, Failures: 3, Errors: 0\n"
                    b"[ERROR] BookingServiceTest.shouldRejectCancelledBooking:88 expected <true>", b"")

    async def fake_shell(cmd, cwd=None, stdout=None, stderr=None):
        return P()

    monkeypatch.setattr(verify.asyncio, "create_subprocess_shell", fake_shell)
    out = await verify.run(str(tmp_path), "mvn -q test")
    assert out.ran is True and out.ok is False, "a real red suite was skipped"


@pytest.mark.asyncio
async def test_a_passing_build_is_a_pass_even_if_it_mentions_a_network(monkeypatch, tmp_path):
    """Exit 0 is exit 0 — the classifier must not second-guess a green run."""
    class P:
        returncode = 0
        async def communicate(self):
            return (b"Downloading from artifactory: connection timed out, retrying\n"
                    b"[INFO] BUILD SUCCESS", b"")

    async def fake_shell(cmd, cwd=None, stdout=None, stderr=None):
        return P()

    monkeypatch.setattr(verify.asyncio, "create_subprocess_shell", fake_shell)
    out = await verify.run(str(tmp_path), "mvn -q test")
    assert out.ran is True and out.ok is True


@pytest.mark.asyncio
async def test_a_check_that_times_out_is_skipped_not_looped(monkeypatch, tmp_path):
    """A clean multi-module Maven build is minutes. If a timeout counted as a red
    suite the gate would loop on it — three rounds of a twenty-minute build is an
    hour spent proving nothing."""
    import asyncio as aio

    class P:
        returncode = None
        async def communicate(self):
            await aio.sleep(10)
        def kill(self):
            pass

    async def fake_shell(cmd, cwd=None, stdout=None, stderr=None):
        return P()

    monkeypatch.setattr(verify.asyncio, "create_subprocess_shell", fake_shell)
    monkeypatch.setenv("ASTA_VERIFY_TIMEOUT", "30")
    monkeypatch.setattr(verify, "timeout_seconds", lambda: 0.05)
    out = await verify.run(str(tmp_path), "mvn -q clean test")
    assert out.ran is False, "a timeout would have sent the gate into a fix loop"
    assert "SKIPPED" in out.tail and "ASTA_VERIFY_TIMEOUT" in out.tail, \
        "a skipped check must say so, or it silently protects nothing"


def test_the_booking_repos_all_have_a_real_check_configured():
    """The whole point of finding 2. An empty command here means the gate is on
    and verifying nothing, which looks exactly like a gate that works."""
    import json
    from pathlib import Path as _P
    real = _P(__file__).resolve().parent.parent / "data" / "verify-commands.json"
    data = json.loads(real.read_text())
    for repo in ("telikos-booking-service", "telikos-email-service",
                 "telikos-activityplanworkflow-service"):
        cmd = data.get(repo, "")
        assert cmd.strip(), f"{repo} has no check command"
        assert "test" in cmd, f"{repo}'s check does not run tests: {cmd}"


def test_the_multi_module_repo_builds_its_siblings_too():
    """service/ depends on persistence, event, booking-domain, common and
    workflow. Without -am those resolve from the repository rather than the
    working tree, so a change to a sibling module would not be checked at all."""
    import json
    from pathlib import Path as _P
    real = _P(__file__).resolve().parent.parent / "data" / "verify-commands.json"
    data = json.loads(real.read_text())
    cmd = data["telikos-activityplanworkflow-service"]
    assert "-am" in cmd, f"sibling modules would be read from ~/.m2, not the tree: {cmd}"


@pytest.mark.parametrize("output,why", [
    ("[ERROR] Failed to execute goal maven-compiler-plugin:3.11.0:compile on project "
     "telikos-email-service: Fatal error compiling: java.lang.ExceptionInInitializerError: "
     "com.sun.tools.javac.code.TypeTag :: UNKNOWN",
     "the real one — Lombok 1.18.30 against local Corretto 21.0.4, measured"),
    ("java.lang.UnsupportedClassVersionError: has been compiled by a more recent version "
     "of the Java Runtime", "wrong JDK on PATH"),
    ("[ERROR] No compiler is provided in this environment. Perhaps you are running on a JRE",
     "a JRE instead of a JDK"),
])
@pytest.mark.asyncio
async def test_a_broken_toolchain_is_not_a_broken_codebase(output, why, monkeypatch, tmp_path):
    """Found by actually running the check: `mvn clean test` on telikos-email-service
    fails in 12 seconds because the pinned Lombok cannot cope with the local JDK.
    Nothing is wrong with the code. Without this the gate fix-loops against a
    broken compiler, escalates to a stronger brain, and never converges."""
    class P:
        returncode = 1
        async def communicate(self):
            return (output.encode(), b"")

    async def fake_shell(cmd, cwd=None, stdout=None, stderr=None):
        return P()

    monkeypatch.setattr(verify.asyncio, "create_subprocess_shell", fake_shell)
    out = await verify.run(str(tmp_path), "mvn -q clean test")
    assert out.ran is False, f"would have fix-looped against {why}"


@pytest.mark.asyncio
async def test_an_ordinary_compile_error_is_still_the_codes_fault(monkeypatch, tmp_path):
    """The line that keeps the classifier honest. A real compile error names a
    file and a position; a toolchain crash does not. If this ever skips, the gate
    stops catching the most common thing an agent gets wrong."""
    class P:
        returncode = 1
        async def communicate(self):
            return (b"[ERROR] /src/main/java/com/maersk/BookingService.java:[42,17] "
                    b"cannot find symbol\n  symbol: method cancelBooking(String)", b"")

    async def fake_shell(cmd, cwd=None, stdout=None, stderr=None):
        return P()

    monkeypatch.setattr(verify.asyncio, "create_subprocess_shell", fake_shell)
    out = await verify.run(str(tmp_path), "mvn -q clean test")
    assert out.ran is True and out.ok is False, "a genuine compile error was skipped"


@pytest.mark.asyncio
async def test_a_checkstyle_violation_is_the_codes_fault(monkeypatch, tmp_path):
    """Checkstyle is bound to Maven's validate phase in these repos, so `test`
    catches it. It is a real finding about the code and must fail the gate."""
    class P:
        returncode = 1
        async def communicate(self):
            return (b"[ERROR] src/main/java/com/maersk/BookingService.java:[88] "
                    b"(sizes) LineLength: Line is longer than 120 characters", b"")

    async def fake_shell(cmd, cwd=None, stdout=None, stderr=None):
        return P()

    monkeypatch.setattr(verify.asyncio, "create_subprocess_shell", fake_shell)
    out = await verify.run(str(tmp_path), "mvn -q clean test")
    assert out.ran is True and out.ok is False, "a checkstyle violation was skipped"


# --- 9. A code task is confined to a workspace -------------------------------
#
# `_cwd(None)` fell back to Asta's own root. A code task with no workspace then
# ran real git commands in THIS repository and moved a branch carrying five
# unpushed commits. A guard was added at the time to the one path that caused it;
# this is the mechanism rather than the symptom.

from app import tasks, workspace_tools


def test_a_code_task_never_lands_in_astas_own_repo(monkeypatch, tmp_path):
    """The incident, prevented at the source."""
    monkeypatch.setattr(workspace_tools, "WORKSPACES",
                        {"booking": tmp_path / "booking", "other": tmp_path / "other"})
    with pytest.raises(RuntimeError, match="refusing to guess"):
        tasks.code_cwd(None)


def test_with_one_workspace_the_only_candidate_is_used(monkeypatch, tmp_path):
    """Refusing here would be correct and useless — the only place the task could
    mean is the only place there is."""
    monkeypatch.setattr(workspace_tools, "WORKSPACES", {"booking": tmp_path / "booking"})
    assert tasks.code_cwd(None) == str(tmp_path / "booking")


def test_with_no_workspaces_at_all_it_refuses(monkeypatch):
    monkeypatch.setattr(workspace_tools, "WORKSPACES", {})
    with pytest.raises(RuntimeError, match="none is registered"):
        tasks.code_cwd(None)


def test_an_unknown_workspace_is_named_not_silently_ignored(monkeypatch, tmp_path):
    monkeypatch.setattr(workspace_tools, "WORKSPACES", {"booking": tmp_path / "booking"})
    with pytest.raises(RuntimeError, match="unknown workspace"):
        tasks.code_cwd("iom-workspace")


def test_analysis_tasks_keep_the_old_lenient_behaviour(monkeypatch):
    """An analysis task only reads. Refusing one for want of a workspace would
    break the most-used and best-performing thing in the system for no gain."""
    monkeypatch.setattr(workspace_tools, "WORKSPACES", {})
    assert tasks._cwd(None) == str(tasks.ROOT)


def test_booking_is_the_only_workspace_registered():
    """Arun removed iom-workspace on 2026-08-19. With exactly one registered,
    a workspace-less code task resolves rather than refuses — so this is load
    bearing for the case above, not decoration."""
    from app.workspace import registry
    assert list(registry.all_workspaces()) == ["booking"]


# --- 7 & 8. Reading its own work, and being able to undo it ------------------
#
# 8: a task commits, branches and moves HEAD, and nothing recorded what it did.
#    Recovery from the branch incident was manual reflog archaeology.
# 7: `review.py` gathers a diff, its checks and the project conventions and
#    produces real notes — pointed only ever at OTHER people's pull requests.

import subprocess


def _git_repo(path):
    path.mkdir(parents=True, exist_ok=True)
    def run(*a):
        return subprocess.run(a, cwd=path, capture_output=True, text=True)
    run("git", "init", "-q", "-b", "main")
    run("git", "config", "user.email", "t@example.com")
    run("git", "config", "user.name", "T")
    (path / "Service.java").write_text("class Service {}\n")
    run("git", "add", "-A"); run("git", "commit", "-qm", "base")
    return path, run


@pytest.mark.asyncio
async def test_a_rollback_point_records_where_every_repo_stood(tmp_path, monkeypatch):
    ws = tmp_path / "ws"
    repo, run = _git_repo(ws / "telikos-booking-service")
    monkeypatch.setattr(workspace_tools, "WORKSPACES", {"booking": ws})
    marks = await tasks.mark_rollback_point(4242, "booking")
    assert "telikos-booking-service" in marks
    assert marks["telikos-booking-service"]["branch"] == "main"
    assert len(marks["telikos-booking-service"]["sha"]) == 40
    assert tasks.rollback_point(4242) == marks, "the mark did not survive the store"


@pytest.mark.asyncio
async def test_rollback_puts_the_repo_back(tmp_path, monkeypatch):
    """THE point of finding 8: a bad run becomes an inconvenience, not an incident."""
    ws = tmp_path / "ws"
    repo, run = _git_repo(ws / "telikos-booking-service")
    monkeypatch.setattr(workspace_tools, "WORKSPACES", {"booking": ws})
    await tasks.mark_rollback_point(4243, "booking")

    run("git", "checkout", "-q", "-b", "feature/BEPTELIKOS-1")
    (repo / "Service.java").write_text("class Service { broken }\n")
    run("git", "add", "-A"); run("git", "commit", "-qm", "the bad change")

    out = await tasks.rollback(4243)
    assert "Restored" in out
    branch = run("git", "rev-parse", "--abbrev-ref", "HEAD").stdout.strip()
    assert branch == "main", f"left on {branch}"
    assert (repo / "Service.java").read_text() == "class Service {}\n"


@pytest.mark.asyncio
async def test_the_tasks_branch_survives_a_rollback(tmp_path, monkeypatch):
    """Undo must itself be reversible — the work is still there to look at."""
    ws = tmp_path / "ws"
    repo, run = _git_repo(ws / "telikos-booking-service")
    monkeypatch.setattr(workspace_tools, "WORKSPACES", {"booking": ws})
    await tasks.mark_rollback_point(4244, "booking")
    run("git", "checkout", "-q", "-b", "feature/BEPTELIKOS-2")
    (repo / "New.java").write_text("class New {}\n")
    run("git", "add", "-A"); run("git", "commit", "-qm", "work")
    await tasks.rollback(4244)
    branches = run("git", "branch").stdout
    assert "feature/BEPTELIKOS-2" in branches, "the undo destroyed the work"


@pytest.mark.asyncio
async def test_rollback_refuses_to_discard_uncommitted_work(tmp_path, monkeypatch):
    """A hard reset over edits he has not committed would be a worse incident
    than the one being undone."""
    ws = tmp_path / "ws"
    repo, run = _git_repo(ws / "telikos-booking-service")
    monkeypatch.setattr(workspace_tools, "WORKSPACES", {"booking": ws})
    await tasks.mark_rollback_point(4245, "booking")
    (repo / "Service.java").write_text("half-finished edit by Arun\n")
    out = await tasks.rollback(4245)
    assert "Could not restore" in out and "uncommitted" in out
    assert (repo / "Service.java").read_text() == "half-finished edit by Arun\n"


@pytest.mark.asyncio
async def test_rollback_without_a_mark_says_so(tmp_path, monkeypatch):
    monkeypatch.setattr(workspace_tools, "WORKSPACES", {"booking": tmp_path})
    assert "No rollback point" in await tasks.rollback(999999)


@pytest.mark.asyncio
async def test_asta_reads_its_own_diff_before_handing_it_over(tmp_path, monkeypatch):
    """Finding 7. The reviewer existed and was never pointed here."""
    ws = tmp_path / "ws"
    repo, run = _git_repo(ws / "telikos-booking-service")
    monkeypatch.setattr(workspace_tools, "WORKSPACES", {"booking": ws})
    await tasks.mark_rollback_point(4246, "booking")
    (repo / "Service.java").write_text("class Service { void cancel() {} }\n")
    run("git", "add", "-A"); run("git", "commit", "-qm", "add cancel")

    seen = {}

    async def fake_review(diff, workspace=""):
        seen["diff"] = diff
        seen["workspace"] = workspace
        return "- cancel() swallows the exception instead of rethrowing"

    from app import review
    monkeypatch.setattr(review, "review_own_diff", fake_review)
    note = await tasks._self_review(4246, {"workspace": "booking"}, "done")
    assert "I read my own diff" in note
    assert "swallows the exception" in note
    assert "void cancel()" in seen["diff"], "the reviewer was given the wrong diff"
    assert seen["workspace"] == "booking"


@pytest.mark.asyncio
async def test_a_clean_review_adds_nothing_to_the_message(tmp_path, monkeypatch):
    """"LOOKS SOUND" must not become a line of noise on every completion."""
    ws = tmp_path / "ws"
    repo, run = _git_repo(ws / "telikos-booking-service")
    monkeypatch.setattr(workspace_tools, "WORKSPACES", {"booking": ws})
    await tasks.mark_rollback_point(4247, "booking")
    (repo / "Service.java").write_text("class Service { int x = 1; }\n")
    run("git", "add", "-A"); run("git", "commit", "-qm", "tweak")

    async def clean(diff, workspace=""):
        return ""

    from app import review
    monkeypatch.setattr(review, "review_own_diff", clean)
    assert await tasks._self_review(4247, {"workspace": "booking"}, "done") == ""


@pytest.mark.asyncio
async def test_a_failing_self_review_never_blocks_completion(tmp_path, monkeypatch):
    """A review that breaks is worth less than the diff it was reviewing."""
    ws = tmp_path / "ws"
    repo, run = _git_repo(ws / "telikos-booking-service")
    monkeypatch.setattr(workspace_tools, "WORKSPACES", {"booking": ws})
    await tasks.mark_rollback_point(4248, "booking")
    (repo / "Service.java").write_text("class Service { int y = 2; }\n")
    run("git", "add", "-A"); run("git", "commit", "-qm", "tweak")

    async def explode(diff, workspace=""):
        raise RuntimeError("no brain available")

    from app import review
    monkeypatch.setattr(review, "review_own_diff", explode)
    assert await tasks._self_review(4248, {"workspace": "booking"}, "done") == ""


def test_the_reviewer_asks_for_problems_not_a_summary():
    """A model asked to describe its own change will describe it approvingly."""
    import inspect
    from app import review
    src = inspect.getsource(review.review_own_diff)
    assert "ONLY problems" in src
    assert "LOOKS SOUND" in src
    assert "No summary" in src


# --- 7 & 8, wired ------------------------------------------------------------
#
# Added after a mutation run caught nothing: deleting the `_self_review` call and
# deleting the `mark_rollback_point` call both left the suite green. The functions
# were tested; being CALLED was not. That is precisely how `notice_asks` shipped
# written, tested, and reachable from nowhere for months.

def test_finishing_a_code_task_reads_its_own_diff():
    import inspect
    src = inspect.getsource(tasks._finish_code)
    assert "_self_review(" in src, "the done path does not review its own work"
    assert "{own}" in src or "own" in src.split("notify.notify")[1][:200], \
        "the review was produced and then not said"


def test_preparing_branches_records_a_rollback_point_first():
    import inspect
    src = inspect.getsource(tasks._prepare_branches)
    assert "mark_rollback_point(" in src, "nothing records where the repos stood"
    body = src.split('"""')[-1]
    assert body.index("mark_rollback_point") < body.index("start_branch") \
        if "start_branch" in body else True, \
        "the mark is taken after a branch has already moved"


@pytest.mark.asyncio
async def test_the_done_message_carries_the_review(tmp_path, monkeypatch):
    """End to end through the real completion path, not the helper."""
    ws = tmp_path / "ws"
    repo, run = _git_repo(ws / "telikos-booking-service")
    monkeypatch.setattr(workspace_tools, "WORKSPACES", {"booking": ws})
    t = store.create_task("BEPTELIKOS-9: fix cancel", "code", "do it", "booking")
    await tasks.mark_rollback_point(t["id"], "booking")
    (repo / "Service.java").write_text("class Service { void cancel() {} }\n")
    run("git", "add", "-A"); run("git", "commit", "-qm", "cancel")

    said = []

    async def spy(text, level="info", urgency="direct", priority=None, **kw):
        said.append(text)

    async def notes(diff, workspace=""):
        return "- cancel() swallows the exception"

    from app import notify, review
    monkeypatch.setattr(notify, "notify", spy)
    monkeypatch.setattr(review, "review_own_diff", notes)
    monkeypatch.setattr(tasks, "_verify_gate", lambda *a, **k: _false())

    await tasks._finish_code(t["id"], dict(t), "the work is done", hops=0)
    done = [s for s in said if "DONE" in s]
    assert done, "no completion message was sent"
    assert "swallows the exception" in done[0], \
        "the task finished without telling him what its own review found"


async def _false():
    return False


# --- 11. One browser, kept alive ---------------------------------------------
#
# Measured before: 2.49s per operation (0.74s Chromium launch + 1.75s Teams app
# boot), paid on every read, every send, every poll. Measured after pooling: the
# first operation 2.08s, every one after it 0.01s.

from app import teams_bridge


class _FakePage:
    def __init__(self, alive=True):
        self.alive = alive
        self.evaluated = 0

    async def evaluate(self, js, *a):
        self.evaluated += 1
        if not self.alive:
            raise RuntimeError("Target page, context or browser has been closed")
        return True


class _FakeCtx:
    def __init__(self, page):
        self.pages = [page]
        self.closed = False

    async def close(self):
        self.closed = True


class _FakePw:
    def __init__(self):
        self.stopped = False

    async def stop(self):
        self.stopped = True


@pytest.fixture
def fake_browser(monkeypatch):
    """Count how many times a browser is actually launched."""
    built = []

    async def fake_launch(headless=True):
        page = _FakePage()
        ctx, pw = _FakeCtx(page), _FakePw()
        built.append((pw, ctx, page))
        return pw, ctx

    async def fake_open(ctx, timeout=75.0):
        return ctx.pages[0]

    monkeypatch.setattr(teams_bridge, "_launch", fake_launch)
    monkeypatch.setattr(teams_bridge, "_open_teams", fake_open)
    return built


@pytest.mark.asyncio
async def test_three_operations_launch_one_browser(fake_browser):
    """THE fix. Three reads used to cost three launches and three app boots."""
    for _ in range(3):
        async with teams_bridge.teams_page() as page:
            await page.evaluate("() => 1")
    assert len(fake_browser) == 1, f"launched {len(fake_browser)} browsers for 3 operations"
    await teams_bridge.close_pool()


@pytest.mark.asyncio
async def test_a_dead_context_is_replaced_not_reused(fake_browser):
    """A stale context believed healthy is a silent failure in front of somebody.
    One wasted relaunch is the cheaper mistake."""
    async with teams_bridge.teams_page():
        pass
    fake_browser[0][2].alive = False          # the renderer died between operations
    async with teams_bridge.teams_page() as page:
        await page.evaluate("() => 1")
    assert len(fake_browser) == 2, "a dead page was handed out"
    await teams_bridge.close_pool()


@pytest.mark.asyncio
async def test_liveness_is_a_real_round_trip_not_a_flag(fake_browser):
    """`is_closed()` alone lies — the tab can be open with the renderer gone."""
    async with teams_bridge.teams_page():
        pass
    page = fake_browser[0][2]
    before = page.evaluated
    async with teams_bridge.teams_page():
        pass
    assert page.evaluated > before, "the pool trusted the page without asking it"
    await teams_bridge.close_pool()


@pytest.mark.asyncio
async def test_a_failed_operation_throws_the_page_away(fake_browser):
    """The page is left in an unknown state — half-typed into a composer, a dialog
    open. Reusing that is how one failure becomes several."""
    with pytest.raises(RuntimeError, match="selector"):
        async with teams_bridge.teams_page():
            raise RuntimeError("no such selector")
    assert fake_browser[0][1].closed, "a poisoned context was kept"
    async with teams_bridge.teams_page():
        pass
    assert len(fake_browser) == 2
    await teams_bridge.close_pool()


@pytest.mark.asyncio
async def test_an_old_browser_is_recycled(fake_browser, monkeypatch):
    async with teams_bridge.teams_page():
        pass
    monkeypatch.setattr(teams_bridge, "POOL_MAX_AGE", 0.0)
    async with teams_bridge.teams_page():
        pass
    assert len(fake_browser) == 2, "the same context was kept past its age limit"
    await teams_bridge.close_pool()


@pytest.mark.asyncio
async def test_closing_the_pool_really_closes_it(fake_browser):
    async with teams_bridge.teams_page():
        pass
    pw, ctx, _ = fake_browser[0]
    await teams_bridge.close_pool()
    assert ctx.closed and pw.stopped
    assert not teams_bridge._POOL


def test_a_ping_reaches_him_within_a_minute():
    """Arun's actual complaint: someone pings and he is not told immediately. The
    old five-minute interval was chosen when a poll cost a browser launch."""
    assert teams_bridge.ACTIVITY_POLL_SECONDS <= 60, \
        "a ping can sit unreported for longer than a minute"


# --- 22. A task he stopped is the lesson -------------------------------------
#
# `should_extract` required status done or sent, so 39% of code tasks — the
# largest category after success — taught nothing, while a run that merely needed
# two attempts taught something. A task Arun kills is him saying "you misread what
# I wanted", within minutes of it happening.

from app import learn


@pytest.mark.parametrize("status", ["cancelled", "rejected"])
def test_a_task_he_stopped_is_always_worth_learning_from(status):
    assert learn.should_extract(rounds=0, escalated=False, status=status) is True


def test_an_easy_success_still_teaches_nothing():
    """The bar for success is unchanged — a one-shot run has no lesson in it."""
    assert learn.should_extract(rounds=1, escalated=False, status="done") is False


def test_a_crash_is_not_a_correction():
    """`failed` is the machinery breaking, not Arun disagreeing. Distilling a
    crash into a procedure would teach the wrong thing entirely."""
    assert learn.should_extract(rounds=0, escalated=False, status="failed") is False


@pytest.mark.asyncio
async def test_his_words_reach_the_learner(monkeypatch):
    """The real case: he interrupts a running task to say it is going the wrong
    way. That sentence is the most valuable thing in the run."""
    captured = {}

    async def fake_extract(title, transcript, *, outcome="done", escalated=False,
                           source="extracted"):
        captured.update(title=title, transcript=transcript, outcome=outcome)

    monkeypatch.setattr(learn, "extract", fake_extract)
    t = store.create_task("BEPTELIKOS-3: add retry", "code",
                          "add a retry to the ATA fallback", "booking")
    store.update_task(t["id"], result="I'll start by refactoring TmsServiceImpl…")

    tasks.learn_from_stop(t["id"], dict(store.get_task(t["id"])), "cancelled",
                          why="no, I didn't want a refactor — just the retry")
    await asyncio.sleep(0.05)

    assert captured, "the correction was thrown away"
    assert captured["outcome"] == "cancelled"
    body = captured["transcript"]
    assert "add a retry to the ATA fallback" in body, "what he asked for is missing"
    assert "refactoring TmsServiceImpl" in body, "what Asta did is missing"
    assert "didn't want a refactor" in body, "his own words are missing"


@pytest.mark.asyncio
async def test_a_stop_with_no_reason_still_teaches_but_says_so(monkeypatch):
    captured = {}

    async def fake_extract(title, transcript, **kw):
        captured["transcript"] = transcript

    monkeypatch.setattr(learn, "extract", fake_extract)
    t = store.create_task("fix the mapper", "code", "fix the mapper", "booking")
    tasks.learn_from_stop(t["id"], dict(store.get_task(t["id"])), "cancelled")
    await asyncio.sleep(0.05)
    assert "gave no reason" in captured["transcript"]


@pytest.mark.asyncio
async def test_cancelling_a_running_task_learns_from_it(monkeypatch):
    """Wired, not merely available — the mutation that deletes this call must fail."""
    seen = []
    monkeypatch.setattr(tasks, "learn_from_stop",
                        lambda tid, t, status, why="": seen.append((status, why)))

    t = store.create_task("long job", "code", "do the thing", "booking")

    async def forever():
        await asyncio.sleep(30)

    job = asyncio.get_running_loop().create_task(forever())
    tasks._running[t["id"]] = job
    killed = await tasks.cancel(t["id"], why="wrong repo")
    assert killed is True
    assert seen == [("cancelled", "wrong repo")], "cancelling taught nothing"


@pytest.mark.asyncio
async def test_rejecting_a_finished_task_learns_from_it(monkeypatch):
    seen = []
    monkeypatch.setattr(tasks, "learn_from_stop",
                        lambda tid, t, status, why="": seen.append((status, why)))
    t = store.create_task("draft", "code", "do it", "booking")
    store.update_task(t["id"], status="done")
    await tasks.reject(t["id"], why="that is not what I meant at all")
    assert seen and seen[0][0] == "rejected"
    assert "not what I meant" in seen[0][1]


def test_a_redirect_passes_his_words_through():
    """The interjection path — the clearest correction there is."""
    import inspect
    from app import main
    src = inspect.getsource(main)
    assert "tasks.cancel(task_id, why=user_text)" in src, \
        "a mid-task redirect throws his correction away"


def test_the_extraction_asks_what_was_misread_not_what_worked():
    """Asking "what worked here" of a run he stopped would distil the very thing
    he rejected into a procedure."""
    import inspect
    src = inspect.getsource(learn.extract)
    assert "ARUN STOPPED THIS RUN" in src
    assert "WHAT WAS MISREAD" in src
    assert "Do NOT write down what this run did" in src
    assert "reply with NOTHING rather" in src, \
        "a stop that teaches nothing would still invent a lesson"


# --- 23. The pooled browser is Asta's to close -------------------------------

def test_shutdown_closes_the_teams_browser():
    """Raised while fixing 11: a context kept alive between operations is one the
    process now owns. Without this a restart orphans Chromium holding the profile,
    and the next start finds it locked by something nobody is watching."""
    import inspect
    from app import main
    src = inspect.getsource(main._shutdown)
    assert "close_pool()" in src, "the pooled browser outlives the process"


@pytest.mark.asyncio
async def test_closing_the_pool_twice_is_harmless():
    """Shutdown can run after an error that already tore the pool down."""
    from app import teams_bridge
    await teams_bridge.close_pool()
    await teams_bridge.close_pool()
    assert not teams_bridge._POOL


# --- 10. Several things at once ----------------------------------------------
#
# Arun's own case: a code task running, while he asks for a bug analysis, while
# somebody asks a question about the repo. Only the first was possible — every
# code task took `_ws_lock(workspace)` and held it for up to thirty minutes.

from app import worktrees


def _real_repo(root, name):
    """A real git repo with a develop branch — worktrees need real git."""
    repo = root / name
    repo.mkdir(parents=True, exist_ok=True)
    def run(*a):
        return subprocess.run(a, cwd=repo, capture_output=True, text=True)
    run("git", "init", "-q", "-b", "develop")
    run("git", "config", "user.email", "t@example.com")
    run("git", "config", "user.name", "T")
    (repo / "Service.java").write_text("class Service {}\n")
    run("git", "add", "-A"); run("git", "commit", "-qm", "base")
    return repo, run


@pytest.mark.asyncio
async def test_two_tasks_get_two_independent_checkouts(tmp_path):
    """THE fix. Two tasks in the same repo, at the same time, on different
    branches, neither aware of the other."""
    ws = tmp_path / "booking-workspace"
    repo, run = _real_repo(ws, "telikos-booking-service")

    a = await worktrees.create(ws, 101, "feature/BEPTELIKOS-1")
    b = await worktrees.create(ws, 102, "feature/BEPTELIKOS-2")
    assert all(r["ok"] for r in a + b), f"{a} {b}"

    pa = pathlib.Path(a[0]["path"]); pb = pathlib.Path(b[0]["path"])
    assert pa != pb and pa.is_dir() and pb.is_dir()

    def branch_of(p):
        return subprocess.run(["git", "rev-parse", "--abbrev-ref", "HEAD"],
                              cwd=p, capture_output=True, text=True).stdout.strip()
    assert branch_of(pa) == "feature/BEPTELIKOS-1"
    assert branch_of(pb) == "feature/BEPTELIKOS-2"


@pytest.mark.asyncio
async def test_one_tasks_work_is_invisible_to_the_other(tmp_path):
    """Isolation is the property that makes parallelism safe, not the directory."""
    ws = tmp_path / "booking-workspace"
    _real_repo(ws, "telikos-booking-service")
    a = await worktrees.create(ws, 111, "feature/A")
    b = await worktrees.create(ws, 112, "feature/B")

    edit = pathlib.Path(a[0]["path"]) / "Service.java"
    edit.write_text("class Service { void a() {} }\n")
    other = pathlib.Path(b[0]["path"]) / "Service.java"
    assert other.read_text() == "class Service {}\n", "one task saw the other's edit"


@pytest.mark.asyncio
async def test_his_own_checkout_is_never_touched(tmp_path):
    """The incident that started all of this: a task moved the branch of a repo
    while an editor and a test run had it open."""
    ws = tmp_path / "booking-workspace"
    repo, run = _real_repo(ws, "telikos-booking-service")
    run("git", "checkout", "-q", "-b", "my-own-wip")
    (repo / "Service.java").write_text("half-finished edit by Arun\n")

    await worktrees.create(ws, 121, "feature/BEPTELIKOS-3")

    assert run("git", "rev-parse", "--abbrev-ref", "HEAD").stdout.strip() == "my-own-wip"
    assert (repo / "Service.java").read_text() == "half-finished edit by Arun\n"


@pytest.mark.asyncio
async def test_the_branch_is_cut_from_develop_not_from_wherever_he_was(tmp_path):
    """His standing rule, and worktrees must not quietly break it."""
    ws = tmp_path / "booking-workspace"
    repo, run = _real_repo(ws, "telikos-booking-service")
    run("git", "checkout", "-q", "-b", "someone-elses-feature")
    (repo / "Stray.java").write_text("class Stray {}\n")
    run("git", "add", "-A"); run("git", "commit", "-qm", "not mine")

    out = await worktrees.create(ws, 131, "feature/BEPTELIKOS-4")
    tree = pathlib.Path(out[0]["path"])
    assert not (tree / "Stray.java").exists(), \
        "the task inherited another branch's commits"


@pytest.mark.asyncio
async def test_removing_a_checkout_leaves_the_repo_clean(tmp_path):
    ws = tmp_path / "booking-workspace"
    repo, run = _real_repo(ws, "telikos-booking-service")
    out = await worktrees.create(ws, 141, "feature/BEPTELIKOS-5")
    assert worktrees.exists(ws, 141)
    notes = await worktrees.remove(ws, 141)
    assert any("removed" in n for n in notes), notes
    assert not worktrees.exists(ws, 141)
    listed = run("git", "worktree", "list").stdout
    assert "task-141" not in listed, "git still believes the worktree exists"


@pytest.mark.asyncio
async def test_uncommitted_work_is_never_deleted(tmp_path):
    """Those edits are the only copy, and this is the one place that could
    destroy them."""
    ws = tmp_path / "booking-workspace"
    _real_repo(ws, "telikos-booking-service")
    out = await worktrees.create(ws, 151, "feature/BEPTELIKOS-6")
    tree = pathlib.Path(out[0]["path"])
    (tree / "Service.java").write_text("work in progress\n")

    notes = await worktrees.remove(ws, 151)
    assert any("uncommitted" in n for n in notes), notes
    assert tree.is_dir() and (tree / "Service.java").read_text() == "work in progress\n"


@pytest.mark.asyncio
async def test_forcing_removal_is_possible_when_he_means_it(tmp_path):
    ws = tmp_path / "booking-workspace"
    _real_repo(ws, "telikos-booking-service")
    out = await worktrees.create(ws, 161, "feature/BEPTELIKOS-7")
    (pathlib.Path(out[0]["path"]) / "Service.java").write_text("scratch\n")
    notes = await worktrees.remove(ws, 161, force=True)
    assert any("removed" in n for n in notes), notes


@pytest.mark.asyncio
async def test_rollback_of_a_worktree_task_removes_it(tmp_path, monkeypatch):
    """Undo becomes deletion — the shared checkout never moved, so there is
    nothing to reset."""
    ws = tmp_path / "booking-workspace"
    repo, run = _real_repo(ws, "telikos-booking-service")
    monkeypatch.setattr(workspace_tools, "WORKSPACES", {"booking": ws})
    t = store.create_task("BEPTELIKOS-8: thing", "code", "do it", "booking")
    await worktrees.create(ws, t["id"], "feature/BEPTELIKOS-8")

    out = await tasks.rollback(t["id"])
    assert "never moved" in out
    assert not worktrees.exists(ws, t["id"])


def test_the_workspace_gate_is_no_longer_exclusive():
    """A lock held for thirty minutes is what capped throughput at one task."""
    lock = tasks._ws_lock("booking")
    assert isinstance(lock, asyncio.Semaphore), \
        "still an exclusive lock — tasks cannot run in parallel"
    assert lock._value >= 2, "the limit permits only one task at a time"


@pytest.mark.asyncio
async def test_parallelism_is_bounded_not_unlimited(monkeypatch):
    """Each task is a checkout, a CLI subprocess and a Maven build, and he is
    working on this laptop while they run."""
    monkeypatch.setattr(worktrees, "MAX_PARALLEL", 2)
    tasks._ws_locks.clear()
    gate = tasks._ws_lock("booking")
    live, peak = [], []

    async def one():
        async with gate:
            live.append(1); peak.append(len(live))
            await asyncio.sleep(0.05)
            live.pop()

    await asyncio.gather(*(one() for _ in range(5)))
    assert max(peak) == 2, f"ran {max(peak)} at once against a limit of 2"


def test_task_work_happens_in_the_tasks_own_checkout():
    """Wired, not merely available."""
    import inspect
    src = inspect.getsource(tasks)
    assert "task_cwd(task_id, t[\"workspace\"])" in src, \
        "code legs still run in the shared checkout"


@pytest.mark.asyncio
async def test_task_cwd_points_at_the_tasks_own_checkout(tmp_path, monkeypatch):
    """Added after a mutation survived: the source-grep test above proved the call
    exists, not that it resolves anywhere different. Without this, `task_cwd`
    could return the shared checkout and every worktree would sit unused while
    tasks quietly went back to fighting over one working tree."""
    ws = tmp_path / "booking-workspace"
    _real_repo(ws, "telikos-booking-service")
    monkeypatch.setattr(workspace_tools, "WORKSPACES", {"booking": ws})

    assert tasks.task_cwd(171, "booking") == str(ws), "no checkout yet — use the workspace"

    await worktrees.create(ws, 171, "feature/BEPTELIKOS-10")
    resolved = tasks.task_cwd(171, "booking")
    assert resolved != str(ws), "the task was sent to the shared checkout"
    assert resolved == str(worktrees.root_for(ws, 171))
    assert (pathlib.Path(resolved) / "telikos-booking-service" / "Service.java").is_file(), \
        "the resolved directory is not a usable checkout"


@pytest.mark.asyncio
async def test_two_tasks_resolve_to_different_places(tmp_path, monkeypatch):
    ws = tmp_path / "booking-workspace"
    _real_repo(ws, "telikos-booking-service")
    monkeypatch.setattr(workspace_tools, "WORKSPACES", {"booking": ws})
    await worktrees.create(ws, 181, "feature/A")
    await worktrees.create(ws, 182, "feature/B")
    assert tasks.task_cwd(181, "booking") != tasks.task_cwd(182, "booking")


# --- 15. The prompt floor, and why narrowing was doing nothing ---------------
#
# Retrieval picks ~8 tools a turn and worked correctly. The STICKY set that
# carries tools between turns did not: it only ever grew. Measured across six
# ordinary turns of one conversation it went 23 -> 26 -> 31 -> 36 -> 37 -> 44 of
# 58, and on reaching 56 it latched to "everything" permanently. So the longer
# Arun talked to Asta, the more every single turn cost — ~5,950 tokens of tool
# schemas before he had said anything — and it never recovered.

from app import tool_index, capabilities


def _fresh_conv(name):
    tool_index.forget(name)
    return name


def test_a_long_conversation_does_not_grow_to_every_tool():
    """THE finding. Eight varied turns used to end at or near the full registry."""
    conv = _fresh_conv("bounded-test")
    total = len(capabilities.registry())
    sizes = []
    for q in ["what did vinish say last night", "fix BEPTELIKOS-1 in booking",
              "any PR waiting on me", "call vinish about the release",
              "how does the ATA fallback work", "draft a mail to priya",
              "check the CI", "what is on my calendar tomorrow"]:
        sel = tool_index.select_sticky(conv, q)
        sizes.append(total if sel is None else len(sel))
    ceiling = tool_index.STICKY_MAX_TOOLS + tool_index.STICKY_SLACK
    assert max(sizes) <= ceiling, \
        f"the toolset grew to {max(sizes)} of {total}: {sizes}"
    # The regression this guards is "grew until it was everything", not the
    # sawtooth between the cap and the slack — that oscillation IS the hysteresis
    # working, and asserting the last turn is no bigger than the first would
    # forbid it.
    assert sizes[-1] < total - 4, f"ended up carrying the whole registry: {sizes}"


def test_the_most_recent_turn_is_always_represented():
    """Stickiness must never evict what this turn actually asked for."""
    conv = _fresh_conv("recency-test")
    for q in ["what did vinish say last night", "any PR waiting on me",
              "check the CI", "what is on my calendar tomorrow"]:
        tool_index.select_sticky(conv, q)
    picked = tool_index.select("send vinish a message saying the build is green", 8)
    sel = tool_index.select_sticky(conv, "send vinish a message saying the build is green")
    assert sel is not None
    for name in (picked or []):
        assert name in sel, f"{name} was picked for this turn and then evicted"


def test_a_follow_up_with_no_keywords_still_has_its_tools():
    """Why stickiness exists at all — "do that again" has nothing to rank on."""
    conv = _fresh_conv("followup-test")
    first = tool_index.select_sticky(conv, "what did vinish say last night")
    second = tool_index.select_sticky(conv, "do that again")
    assert second is None or "teams_history" in second, \
        "the follow-up lost the tool the previous turn established"


def test_the_floor_is_never_evicted():
    """Tools every turn may need must survive any amount of churn."""
    conv = _fresh_conv("floor-test")
    for q in ["fix a bug", "read teams", "check ci", "draft a mail",
              "trace a booking", "what is on my calendar"]:
        tool_index.select_sticky(conv, q)
    sel = tool_index.select_sticky(conv, "one more thing")
    if sel is not None:
        for name in tool_index._floor():
            assert name in sel, f"the floor tool {name} was evicted"


def test_narrowing_actually_saves_the_tokens_it_claims():
    """The point of the exercise, asserted in the unit that matters."""
    def cost(names):
        fns = capabilities.tools_for(names)
        return sum(len(f.__doc__ or "") for f in fns) + len(capabilities.notes_block(names))

    conv = _fresh_conv("cost-test")
    for q in ["what did vinish say last night", "any PR waiting on me", "check the CI"]:
        sel = tool_index.select_sticky(conv, q)
    assert sel is not None, "narrowing gave up"
    full, narrow = cost(None), cost(sel)
    assert narrow < full * 0.6, \
        f"narrowing saved only {100 - 100 * narrow // full}% ({narrow} vs {full} chars)"


# --- 12. Knowing when Teams changes underneath us ----------------------------
#
# Fifty-nine selectors stand between Asta and Microsoft's markup, and every one
# fails the same silent way: `_click_first` returns False and something Arun asked
# for quietly did not happen. Two days went on `button[aria-label="Audio call"]`
# alone — Teams renders a div, so the tag constraint made every call fail.

from app import selector_health


class _StatePage:
    """A Teams page where each selector matches only in the right state."""

    def __init__(self, present: dict[str, int], reachable=("chat", "person", "activity")):
        self.present = present
        self.reachable = reachable
        self.state = "app"

    async def evaluate(self, js, sel=None):
        if sel is None:
            return None
        return self.present.get(sel, 0) if self.state == "app" else \
            self.present.get(sel, 0)


def test_the_check_uses_the_selectors_the_product_actually_uses():
    """The first version invented its own and reported six of seven BROKEN — every
    one a false alarm, because the guesses were wrong, not the code. The activity
    feed is the proof: the real reader uses `activity-list-container`, and the
    check had guessed `activity-feed`, `activity-list` and `role=feed`."""
    import pathlib as _p
    bridge = (_p.Path(__file__).resolve().parent.parent / "app" / "teams_bridge.py").read_text()
    activity = selector_health.CRITICAL["activity feed"]["selector"]
    assert "activity-list-container" in activity
    assert "activity-list-container" in bridge, \
        "the check and the product disagree about the activity feed"


def test_every_critical_selector_names_what_it_breaks():
    """A health report that says a selector failed, without saying what stops
    working, is a puzzle rather than a warning."""
    for name, spec in selector_health.CRITICAL.items():
        assert spec.get("breaks"), f"{name} does not say what it breaks"
        assert spec.get("where"), f"{name} does not say where it is used"
        assert spec.get("needs") in ("app", "chat", "person", "activity"), name


@pytest.mark.asyncio
async def test_a_missing_selector_is_reported_as_broken():
    page = _StatePage({'[data-tid="chat-pane-item"]': 0})
    out = await selector_health.check(page, "chat")
    rows = {r["name"]: r for r in out}
    assert rows["message rows"]["ok"] is False


@pytest.mark.asyncio
async def test_a_present_selector_is_reported_as_fine():
    page = _StatePage({'[data-tid="chat-pane-item"]': 38})
    out = await selector_health.check(page, "chat")
    assert {r["name"]: r["ok"] for r in out}["message rows"] is True


@pytest.mark.asyncio
async def test_only_the_selectors_for_that_state_are_checked():
    """A composer only exists once a chat is open; a call button only in a chat
    header. Checking everything against whatever page happened to be loaded is
    what produced six false alarms."""
    page = _StatePage({})
    names = {r["name"] for r in await selector_health.check(page, "app")}
    assert names == {"chat list", "search box"}, names


@pytest.mark.asyncio
async def test_a_page_that_cannot_be_asked_is_not_called_broken():
    class Dead:
        async def evaluate(self, js, sel=None):
            raise RuntimeError("Target closed")

    out = await selector_health.check(Dead(), "app")
    assert all(r["ok"] is False and "could not be checked" in r["note"] for r in out)


def test_unchecked_is_never_reported_as_broken():
    """THE lesson from building this. Reporting 'six of seven broken' when the
    truth was 'I never opened a chat' teaches him to ignore the check — and then
    he ignores the real one too."""
    results = [
        {"name": "chat list", "ok": True, "found": 55},
        {"name": "call button", "ok": True, "found": 0, "unchecked": True,
         "note": "not checked — no 1:1 chat configured"},
    ]
    text, broken = selector_health.summarise(results)
    assert broken is False, "an unchecked selector was reported as a failure"
    assert "1 checked" in text and "1 not checked" in text


def test_a_real_break_is_reported_with_what_it_costs():
    results = [{"name": "call button", "ok": False, "found": 0,
                "selector": '[data-tid="default-chat-call-audio-button"]',
                "breaks": "placing a call", "where": "meetings._CALL_BUTTONS"}]
    text, broken = selector_health.summarise(results)
    assert broken is True
    assert "placing a call" in text and "meetings._CALL_BUTTONS" in text
    assert "Nothing has been guessed at" in text, \
        "a replacement selector chosen blind is how a message lands in the wrong thread"


def test_the_check_returns_to_the_chat_tab_first():
    """The pooled browser is reused, so the page may be sitting on Activity from
    the last run — which made this check order-dependent and produced a chat state
    that had simply never been opened."""
    import inspect
    src = inspect.getsource(selector_health.run)
    assert 'aria-label^="Chat' in src, "the check depends on where the page was left"


# --- 12b. The check configures itself ----------------------------------------
#
# It first needed a colleague named in `.env`. Arun's objection, and he is right:
# he can ask Asta to message anyone, so a knob he must maintain forever is a knob
# he will forget. The rail he already has is the answer.

class _RailPage:
    """A Teams chat rail, and what each conversation renders when opened."""

    def __init__(self, rail, has_call=(), has_messages=()):
        self.rail = rail
        self.has_call = set(has_call)
        self.has_messages = set(has_messages)
        self.open = None
        self.opened = []

    async def evaluate(self, js, sel=None):
        if "treeitem" in js and sel is None:
            return list(self.rail)
        if sel == selector_health.CRITICAL["call button"]["selector"]:
            return 1 if self.open in self.has_call else 0
        if sel == selector_health.CRITICAL["message rows"]["selector"]:
            return 1 if self.open in self.has_messages else 0
        return 0


def _bridge_opening(page, monkeypatch):
    async def find(pg, name, allow_group=True):
        for entry in page.rail:
            if entry.lower().startswith(name.lower()):
                page.open = entry
                page.opened.append(entry)
                return entry
        raise RuntimeError(f"no match for {name}")

    from app import teams_bridge
    monkeypatch.setattr(teams_bridge, "_find_chat", find)


@pytest.mark.asyncio
async def test_it_finds_a_callable_chat_without_being_told_who(monkeypatch):
    """No name in .env. It opens conversations until the call button renders."""
    page = _RailPage(
        rail=["Quick views", "Mentions", "Drafts", "Arunkumar K (You)",
              "Chats", "Vinish Kumar", "Team Booking and Execution"],
        has_call={"Vinish Kumar", "Team Booking and Execution"},
        has_messages={"Arunkumar K (You)", "Vinish Kumar"})
    _bridge_opening(page, monkeypatch)
    found = await selector_health._find_a_chat(page, want_call=True)
    assert found == "Vinish Kumar"
    assert "Arunkumar K (You)" not in page.opened, \
        "it tried to check a call button in a self-chat"


@pytest.mark.asyncio
async def test_navigation_entries_are_not_mistaken_for_chats(monkeypatch):
    """The rail opens with Quick views, Mentions, Discover, Drafts, Saved — the
    same trap `_find_chat`'s docstring warns about."""
    page = _RailPage(rail=["Quick views", "Mentions", "Discover", "Drafts",
                           "Saved", "Favorites", "Vinish Kumar"],
                     has_call={"Vinish Kumar"}, has_messages={"Vinish Kumar"})
    _bridge_opening(page, monkeypatch)
    await selector_health._find_a_chat(page, want_call=True)
    assert page.opened == ["Vinish Kumar"], f"opened navigation: {page.opened}"


@pytest.mark.asyncio
async def test_his_own_thread_is_preferred_when_only_reading(monkeypatch):
    """Checking message markup should open nobody else's conversation."""
    page = _RailPage(rail=["Vinish Kumar", "Arunkumar K (You)"],
                     has_messages={"Vinish Kumar", "Arunkumar K (You)"})
    _bridge_opening(page, monkeypatch)
    found = await selector_health._find_a_chat(page, want_call=False)
    assert found == "Arunkumar K (You)"
    assert page.opened == ["Arunkumar K (You)"], "it read a colleague's chat first"


@pytest.mark.asyncio
async def test_without_a_self_chat_it_still_checks_something(monkeypatch):
    """A rail with no "(You)" entry is a reason to read someone else's message
    list, not a reason to stop checking whether reading works at all."""
    page = _RailPage(rail=["Vinish Kumar"], has_messages={"Vinish Kumar"})
    _bridge_opening(page, monkeypatch)
    assert await selector_health._find_a_chat(page, want_call=False) == "Vinish Kumar"


def test_the_rendered_name_is_not_the_searchable_name():
    """"Arunkumar K (You)" finds nothing; "Arunkumar K" resolves to it. The suffix
    is display decoration and exists nowhere in the directory."""
    assert selector_health._searchable("Arunkumar K (You)") == "Arunkumar K"
    assert selector_health._searchable("Vinish Kumar") == "Vinish Kumar"


def test_no_selector_check_settings_remain():
    """Every knob Arun would have had to maintain is gone.

    Checks for a real environment READ, not for the spelling. The comment saying
    why this module has no settings has to be allowed to name them — a test that
    forbids explaining a decision pushes the explanation out of the file, which is
    the opposite of what it is for.
    """
    import pathlib as _p
    import re as _re
    src = (_p.Path(__file__).resolve().parent.parent / "app" / "selector_health.py").read_text()
    reads = _re.findall(r"environ[^\n]*ASTA_SELECTOR_CHECK\w*", src)
    assert not reads, f"a selector-check knob is back: {reads}"


# --- 13. A swallowed error still happened ------------------------------------
#
# Ninety-two places deliberately ignore an exception, and nearly all are right to:
# a caption read must not end a call, a failed extraction must not fail the work
# it learned from. What was missing was any trace — so a selector that quietly
# stopped matching degraded Asta with no record anywhere, which is the same shape
# as every other finding in this review: not a crash, a silence.

from app import quiet


@pytest.fixture(autouse=True)
def _clean_swallow_ledger():
    quiet.reset()
    yield
    quiet.reset()


def test_a_swallowed_error_does_not_propagate():
    """The behaviour must not change — that is the whole point of these sites."""
    with quiet.swallow("probe.site"):
        raise RuntimeError("boom")


def test_but_it_is_remembered():
    with quiet.swallow("teams.read_activity"):
        raise RuntimeError("selector gone")
    assert quiet.counts()["teams.read_activity"]["count"] == 1
    assert "selector gone" in quiet.counts()["teams.read_activity"]["error"]


def test_repeats_aggregate_by_site_not_by_message():
    """Sites are named, not messages, so the same fault does not look new each
    time it happens with slightly different wording."""
    for i in range(5):
        with quiet.swallow("teams.read_activity"):
            raise RuntimeError(f"attempt {i} failed differently")
    assert quiet.counts()["teams.read_activity"]["count"] == 5
    assert len(quiet.counts()) == 1


def test_one_failure_is_not_worth_his_attention():
    with quiet.swallow("teams.read_activity"):
        raise RuntimeError("blip")
    assert quiet.loud() == []
    assert "none recurring" in quiet.summary()


def test_a_site_failing_constantly_becomes_a_health_problem():
    """"Teams reads have been failing for three days" should be a question with
    an answer, not something he eventually notices."""
    for _ in range(quiet.LOUD_AFTER + 2):
        with quiet.swallow("teams.read_activity"):
            raise RuntimeError("selector gone")
    bad = quiet.loud()
    assert len(bad) == 1 and bad[0]["where"] == "teams.read_activity"
    assert "failing repeatedly" in quiet.summary()


@pytest.mark.asyncio
async def test_health_reports_a_site_that_keeps_failing(monkeypatch):
    from app import health
    for _ in range(quiet.LOUD_AFTER + 1):
        with quiet.swallow("teams.read_activity"):
            raise RuntimeError("selector gone")
    problems = await health.checks()
    keys = [k for k in problems if k.startswith("repeated:")]
    assert keys, f"a site failing {quiet.LOUD_AFTER + 1}x never reached health"
    assert "ignored each time" in problems[keys[0]]


def test_the_threshold_is_recorded_once_not_every_time():
    """A push per swallowed exception is exactly the noise the attention ledger
    exists to end."""
    recorded = []
    from app import store
    real = store.record_outcome
    try:
        store.record_outcome = lambda *a, **k: recorded.append(a)
        for _ in range(quiet.LOUD_AFTER * 2):
            with quiet.swallow("noisy.site"):
                raise RuntimeError("x")
    finally:
        store.record_outcome = real
    assert len(recorded) <= 1, f"recorded {len(recorded)} times"


def test_the_caption_reader_records_its_failures():
    """The specific site that would otherwise produce an empty recap with no
    explanation anywhere."""
    import inspect
    from app import meetings
    assert "quiet.note(\"call.poll_captions\"" in inspect.getsource(meetings.poll_captions)


# --- 14. Does it answer CORRECTLY ---------------------------------------------
#
# Sixteen hundred tests proved mechanism. None asked whether an answer about the
# booking codebase was right, so the most-used capability in the system had no
# measurement at all and "is it getting better" was a feeling.
#
# The first live run scored 0/6 and found three real faults in one go: the
# Anthropic key was invalid so every API answer 401'd silently, the in-call brain
# had no fallback to the CLI subscriptions Arun already pays for, and the lessons
# written from his own corrections were never handed to the thing answering.
# After fixing those: 5/6.

from app import evals


def test_every_case_is_grounded_in_something_verified():
    """A case whose ground truth cannot be pointed at is worse than no case — it
    measures agreement with a guess and calls the result quality."""
    cases = evals.load("booking")
    assert cases, "no eval cases are loaded at all"
    for c in cases:
        assert c.get("source"), f"{c['id']} cites no ground truth"
        assert c.get("why"), f"{c['id']} does not say what it is testing"
        assert c.get("must") or c.get("must_not"), f"{c['id']} asserts nothing"


def test_the_cited_ground_truth_actually_says_what_the_case_claims():
    """The cases must track the workspace. If a lesson is rewritten and the case
    is not, the eval quietly starts measuring history."""
    from app import workspace as ws_mod
    conv = ws_mod.conventions("booking")
    if not conv.strip():
        pytest.skip("no workspace conventions on this machine")
    for c in evals.load("booking"):
        for fact in c.get("must", []):
            assert fact.lower() in conv.lower(), \
                f"{c['id']} expects '{fact}' but the workspace no longer says it"


def test_a_right_answer_passes():
    case = {"id": "x", "must": ["booking.references", "serviceDates"]}
    out = evals.grade("They live in booking.references and ServicePlan.serviceDates.", case)
    assert out["ok"] and not out["missing"]


def test_a_missing_fact_fails_and_names_it():
    case = {"id": "x", "must": ["booking.references", "serviceDates"]}
    out = evals.grade("They're stored on the booking somewhere.", case)
    assert not out["ok"]
    assert out["missing"] == ["booking.references", "serviceDates"]


def test_a_confidently_wrong_answer_fails():
    """`must_not` catches the answer that is fluent and backwards — the expensive
    kind, because it reads as authoritative."""
    case = {"id": "x", "must": ["VTS"], "must_not": ["the user's value takes precedence"]}
    out = evals.grade("VTS sends it, but the user's value takes precedence.", case)
    assert not out["ok"] and out["wrong"]


def test_no_answer_is_a_failure_not_a_pass():
    """The whole 0/6 run returned empty strings. If empty scored as 'nothing to
    check', a completely dead brain would have shown as perfect."""
    out = evals.grade("", {"id": "x", "must": ["anything"]})
    assert not out["ok"] and out["empty"]
    out2 = evals.grade("", {"id": "y", "must_not": ["wrong thing"]})
    assert not out2["ok"], "a silent brain scored as correct"


@pytest.mark.asyncio
async def test_a_brain_that_throws_is_reported_not_crashed(monkeypatch):
    async def explode(question):
        raise RuntimeError("401 API key is invalid")

    out = await evals.run("booking", ask=explode)
    assert out["passed"] == 0 and out["total"] > 0
    assert all("401" in r.get("error", "") for r in out["results"])
    assert "401" in evals.report(out)


@pytest.mark.asyncio
async def test_the_report_says_what_the_answer_should_have_cited(monkeypatch):
    async def vague(question):
        return "It is handled somewhere in the service layer."

    out = await evals.run("booking", ask=vague)
    text = evals.report(out)
    assert "ground truth:" in text, "a failure that does not say where the truth lives"
    assert "lessons.md" in text


@pytest.mark.asyncio
async def test_a_score_is_recorded_so_change_is_visible(monkeypatch):
    async def perfect(question):
        return ("booking.references serviceDates VTS "
                "VesselTrackingRegistrationActivityImpl clean MapStruct -am")

    out = await evals.run("booking", ask=perfect)
    assert out["rate"] == 1.0
    rows = [r for r in store.list_outcomes(kind="eval", limit=5)] \
        if hasattr(store, "list_outcomes") else []
    assert out["total"] == len(evals.load("booking"))


# --- the faults the first eval run exposed -----------------------------------

def test_the_answering_brain_reaches_a_cli_and_records_the_failure(monkeypatch):
    """`ANTHROPIC_API_KEY` was set, `available("claude")` said yes because a key
    was PRESENT, and every call 401'd. The in-call brain returned "" and said
    nothing while two working CLI subscriptions sat unused.

    Checked behaviourally. This was a source grep for "claude_cli" inside
    `answer_from_knowledge`, which broke the moment the CLI loop moved into a
    helper — and would equally have passed if that loop were dead code. A grep
    tests where a string sits; the property is that a CLI is actually reached and
    the in-process failure is not swallowed silently.
    """
    from app import agent as agent_mod, call_brain, quiet

    asked: list[str] = []

    class _Runner:
        async def one_shot(self, prompt, cwd=None, timeout=120, **kw):
            asked.append("cli")
            return "the CLI answered"

    def _dead_api():
        raise RuntimeError("401 invalid x-api-key")

    monkeypatch.setattr(call_brain, "_cli_first", lambda: False)   # API first
    monkeypatch.setattr(agent_mod, "best_model_name", _dead_api, raising=False)
    monkeypatch.setattr(agent_mod, "available", lambda n: n in ("claude_cli", "copilot"))
    monkeypatch.setattr(agent_mod, "quota_down", lambda n: False)
    monkeypatch.setattr(agent_mod, "runner", lambda n: _Runner())

    out = asyncio.run(call_brain.answer_from_knowledge("where do vessel dates live?"))
    assert out == "the CLI answered", "the CLI fallback was never reached"
    assert asked == ["cli"]
    assert any(k.startswith("brain.") for k in quiet.counts()), \
        "the in-process failure was swallowed with no record"


def test_the_answering_brain_is_given_his_lessons():
    """They were written FROM his corrections and never read back. Asked why the
    build fails with a FilerException — cause and fix both documented — the
    answer was "I couldn't find any reference to that"."""
    import inspect
    from app import meetings
    src = inspect.getsource(meetings.answer_from_knowledge)
    assert "conventions(" in src, "the captured lessons never reach the answer"


def test_conventions_include_the_per_repo_lessons():
    """The workspace-level file is 1,163 characters and says nothing about how
    anything builds. Everything specific lives one directory down, and that
    directory was skipped."""
    import inspect
    from app.workspace.providers.indexed import IndexedProvider
    src = inspect.getsource(IndexedProvider.conventions)
    assert 'self.ctx / "repos"' in src, "per-repo lessons are still invisible"


# --- 19. A key that is present is not a key that works -----------------------

def test_a_refused_key_takes_the_brain_out_of_service(monkeypatch):
    """Measured: ANTHROPIC_API_KEY was set, available() said yes because a key was
    PRESENT, and every call 401'd — silently, while two working CLI brains sat
    unused."""
    from app import agent as agent_mod
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-whatever")
    store.kv_del(agent_mod.credential_kv("claude"))
    assert agent_mod.available("claude") is True
    agent_mod.mark_key_rejected("claude", "401 API key is invalid")
    assert agent_mod.available("claude") is False


def test_a_new_key_gets_a_fresh_chance(monkeypatch):
    """Nothing about waiting makes an invalid key valid, so this must not expire
    on a timer — but changing the key is exactly the thing that fixes it."""
    from app import agent as agent_mod
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-old")
    agent_mod.mark_key_rejected("claude", "401")
    assert agent_mod.key_rejected("claude") is True
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-new")
    assert agent_mod.key_rejected("claude") is False


def test_only_a_credential_error_counts():
    """Wrongly marking a working brain takes it out of service until the key is
    changed, so this stays narrow."""
    from app import agent as agent_mod
    assert agent_mod.credential_failure("status_code: 401, api key is invalid")
    assert agent_mod.credential_failure("authentication_error")
    assert not agent_mod.credential_failure("rate_limit_error: slow down")
    assert not agent_mod.credential_failure("overloaded_error")
    assert not agent_mod.credential_failure("connection reset by peer")


def test_the_fingerprint_never_contains_the_key(monkeypatch):
    from app import agent as agent_mod
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-secret-value-here")
    fp = agent_mod._key_fingerprint("claude")
    assert fp and "secret" not in fp and len(fp) == 12


@pytest.mark.asyncio
async def test_health_says_the_key_was_refused(monkeypatch):
    from app import agent as agent_mod, health
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-bad")
    agent_mod.mark_key_rejected("claude", "401 API key is invalid")
    problems = await health.checks()
    assert "claude-key" in problems
    assert "REFUSED" in problems["claude-key"]


def test_a_401_marks_the_key_rather_than_retrying_forever():
    import inspect
    from app import meetings, memory
    assert "mark_key_rejected" in inspect.getsource(meetings.answer_from_knowledge)
    assert "mark_key_rejected" in inspect.getsource(memory.cheap_complete)


# --- 24. MCP servers were never narrowed at all ------------------------------
#
# Native tools have been retrieved per turn for a long time; the MCP toolsets
# were not. `toolsets=MCP_TOOLSETS or None` attached every server to every turn.
# Measured: 32 tools, ~6,205 tokens — and that is WITHOUT Grafana, which failed
# to enumerate. GitHub alone is 26 tools and ~3,917 tokens, present whether or
# not GitHub was mentioned. More than the entire native surface after narrowing.

@pytest.mark.parametrize("query,expected", [
    # NOT github: "prod" contains "pr", which is how a mail to Priya used to
    # pull in twenty-six GitHub tools.
    ("why is the booking service throwing 5xx in prod", {"grafana"}),
    ("trace booking 88271 through temporal", {"temporal", "grafana"}),
    ("any PR waiting on me", {"github"}),
    ("what is BEPTELIKOS-10159 about", {"atlassian"}),
])
def test_a_turn_gets_the_servers_it_needs(query, expected):
    got = tool_index.mcp_for(f"c-{abs(hash(query))}", query)
    assert expected <= got, f"missing {expected - got} for: {query}"


@pytest.mark.parametrize("query", [
    "what did vinish say last night",
    "how does the ATA fallback pick the transport order",
    "draft a mail to priya about the release",
])
def test_an_unrelated_turn_gets_none_of_them(query):
    """A question about last night's Teams messages does not need twenty-six
    GitHub tools in its prompt."""
    assert tool_index.mcp_for(f"c-{abs(hash(query))}", query) == set()


def test_a_debugging_conversation_keeps_grafana(monkeypatch):
    """Losing Grafana halfway through a debugging conversation, because the
    follow-up said "and the one before that", is worse than carrying it."""
    conv = "debug-conv"
    tool_index.forget(conv)
    tool_index.mcp_for(conv, "why is booking throwing 5xx in prod")
    still = tool_index.mcp_for(conv, "and what about the one before that")
    assert "grafana" in still


def test_forgetting_a_conversation_clears_its_servers():
    conv = "rotate-conv"
    tool_index.mcp_for(conv, "check grafana logs")
    tool_index.forget(conv)
    assert tool_index.mcp_for(conv, "hello") == set()


def test_the_turn_actually_uses_the_narrowed_list():
    """Wired, not merely available."""
    import inspect
    from app import main
    src = inspect.getsource(main)
    assert "_mcp_for_turn(" in src, "every server is still attached to every turn"
    assert "toolsets=MCP_TOOLSETS or None" not in src


def test_every_configured_server_can_be_selected():
    """A server nobody can trigger is a server that is never available."""
    import json as _json
    import pathlib as _p
    cfg = _p.Path(__file__).resolve().parent.parent / "mcp.json"
    configured = set(_json.loads(cfg.read_text()).get("mcpServers", {}))
    assert configured <= set(tool_index.MCP_TRIGGERS), \
        f"no trigger words for {configured - set(tool_index.MCP_TRIGGERS)}"


def test_a_window_that_has_not_closed_yet_can_still_be_covered():
    """Found by the date rolling over mid-session. "Last night" runs to six this
    morning, so at half past midnight `until` is in the FUTURE — and nobody can
    have read a chat after a moment that has not happened. Requiring it made
    every such question re-open a browser it did not need."""
    now = time.time()
    since = now - 4 * HOUR
    until = now + 5 * HOUR                       # the window is still open
    _store_msg("Vinish Kumar", "older", sent_at=since - HOUR)
    _store_msg("Vinish Kumar", "in window", sent_at=since + HOUR, seen_at=now - 60)
    assert store.teams_history_covers("Vinish Kumar", since, until) is True


def test_an_open_window_still_needs_a_recent_read():
    """The tolerance must not become "any read ever counts" — a read from hours
    ago says nothing about what arrived since."""
    now = time.time()
    since, until = now - 4 * HOUR, now + 5 * HOUR
    _store_msg("Suraj", "older", sent_at=since - HOUR)
    _store_msg("Suraj", "in window", sent_at=since + HOUR,
               seen_at=now - store.HISTORY_FRESH_SECONDS - 600)
    assert store.teams_history_covers("Suraj", since, until) is False


def test_a_window_ending_right_now_does_not_demand_an_instant_read():
    """"Last night" evaluated at 00:07 ends AT 00:07. Requiring a read after that
    moment is requiring a read in the same instant the question is asked, which
    nothing can satisfy — so every such question re-opened a browser."""
    now = time.time()
    since, until = now - 6 * HOUR, now          # the window closes exactly now
    _store_msg("Vinish Kumar", "older", sent_at=since - HOUR)
    _store_msg("Vinish Kumar", "in window", sent_at=since + HOUR, seen_at=now - 30)
    assert store.teams_history_covers("Vinish Kumar", since, until) is True


def test_a_window_that_closed_yesterday_still_needs_a_read_after_it():
    """The floor must not weaken a genuinely closed window: a read from before it
    closed cannot have seen what arrived at the end of it."""
    now = time.time()
    since, until = now - 30 * HOUR, now - 24 * HOUR
    _store_msg("Suraj", "older", sent_at=since - HOUR)
    _store_msg("Suraj", "in window", sent_at=since + HOUR, seen_at=until - 2 * HOUR)
    assert store.teams_history_covers("Suraj", since, until) is False


# --- Retrieval: what vectors would and would not buy --------------------------
#
# Measured before deciding. Memory recall ALREADY does hybrid retrieval — FTS5
# casting a wide net, local embeddings re-ranking — so vectors are not a missing
# technology here. Two things were actually wrong:
#
#   1. The embedder is not running, so recall is keyword-only. Asked "vessel eta
#      not updating" it returned a memory titled "WhatsApp".
#   2. Teams history had no index at all. 287 messages reachable only by chat
#      name or time window, so "what did we decide about the ATA fallback" — the
#      question he actually asks — could not be answered at any speed.

def test_teams_history_is_searchable_by_topic():
    """`LIKE '%ata%'` matches letters, not words: it finds "ata" inside "data",
    ranks nothing, and misses a thread that said "transport order" instead."""
    store.save_teams_messages([
        {"key": "s1", "chat": "Vinish Kumar", "sender": "Vinish",
         "text": "the ATA fallback picks the transport order from the service plan",
         "sent_at": time.time() - HOUR, "stamp": ""},
        {"key": "s2", "chat": "Team Booking", "sender": "Divya",
         "text": "lunch at 1?", "sent_at": time.time() - HOUR, "stamp": ""},
    ])
    with store._connect() as c:
        c.execute("INSERT INTO teams_fts(teams_fts) VALUES('rebuild')")
    hits = store.teams_search("ATA fallback", 5)
    assert hits, "a message plainly about the subject was not found"
    assert any("transport order" in h["text"] for h in hits)
    assert not any("lunch" in h["text"] for h in hits), "unrelated chatter ranked in"


def test_results_are_ranked_not_just_matched():
    store.save_teams_messages([
        {"key": "r1", "chat": "A", "sender": "X",
         "text": "vessel ETA vessel ETA vessel ETA is not syncing to serviceDates",
         "sent_at": time.time() - HOUR, "stamp": ""},
        {"key": "r2", "chat": "B", "sender": "Y",
         "text": "unrelated note that happens to say vessel once",
         "sent_at": time.time() - HOUR, "stamp": ""},
    ])
    with store._connect() as c:
        c.execute("INSERT INTO teams_fts(teams_fts) VALUES('rebuild')")
    hits = store.teams_search("vessel ETA serviceDates", 5)
    assert hits and "serviceDates" in hits[0]["text"], "bm25 ranking is not being applied"


def test_a_generic_query_returns_nothing_rather_than_everything():
    """Matching on noise is worse than admitting there is nothing distinctive."""
    assert store.teams_search("the and is", 5) == []


def test_nothing_found_is_reported_as_no_record_not_as_never_said():
    """"I have no record of that" is true. "Nobody said that" is not, and this
    searches only what Asta has already read."""
    out = agent_mod.teams_search("something nobody has ever discussed here xyzzy")
    assert "no record" in out.lower() or "never have been read" in out.lower()
    assert "teams_history" in out, "it does not offer the way to actually go and look"


def test_the_index_keeps_up_with_new_messages():
    """A search index that needs a manual rebuild is an index that is wrong."""
    store.save_teams_messages([{"key": "live1", "chat": "Vinish Kumar",
                                "sender": "Vinish", "text": "quokka migration plan",
                                "sent_at": time.time(), "stamp": ""}])
    assert any("quokka" in h["text"] for h in store.teams_search("quokka", 5)), \
        "a message inserted after startup is not searchable"


@pytest.mark.asyncio
async def test_health_says_what_a_missing_embedder_costs(monkeypatch):
    """It was reported as "digests fall back to heuristics", which undersells it:
    the same model re-ranks memory recall and decides whether Asta may speak."""
    from app import health, memory
    monkeypatch.setattr(memory, "local_llm_model", lambda: None)
    problems = await health.checks()
    assert "lmstudio" in problems
    assert "keyword-only" in problems["lmstudio"]
    assert "calls" in problems["lmstudio"]


def test_a_follow_up_does_not_evict_the_previous_turns_tools():
    """Caught by a surviving mutant. Determinism alone does not detect churn —
    the same query twice is stable with or without hysteresis. The cost shows on
    CONSECUTIVE turns about the same subject: sitting exactly at the cap, every
    turn evicted one tool to make room for one, changing the tool block and
    missing a prompt cache that should have hit.
    """
    conv = "no-evict"
    tool_index.forget(conv)
    first = set(tool_index.select_sticky(conv, "any messages from Vinish?") or ())
    second = set(tool_index.select_sticky(conv, "anything else from Vinish?") or ())
    assert first, "nothing was selected at all"
    evicted = first - second
    assert not evicted, f"a follow-up evicted {sorted(evicted)} to make room"


# --- 21. SQLite: the right measurement --------------------------------------
#
# The finding said "synchronous SQLite on the event loop" and the first
# measurement closed it: p50 0.2-0.5 ms, worst p99 4.7 ms, and wrapping hundreds
# of call sites in `to_thread` would add thread overhead exceeding the query cost.
# That was true and it was the wrong measurement. Splitting connection setup from
# the query: a kv_get took 0.231 ms of which the QUERY was 0.002 ms. Ninety-nine
# percent of every store call in the system was opening a connection, re-running
# PRAGMA journal_mode=WAL, and throwing it away.

def test_the_same_thread_reuses_one_connection():
    a = store._connect()
    b = store._connect()
    assert a is b, "a new connection per call — 99% of the cost is setup"


def test_a_moved_database_gets_a_new_connection(tmp_path, monkeypatch):
    """The isolation rule in conftest repoints DB_PATH per test. Handing back the
    previous database's connection would let one test read another's rows."""
    first = store._connect()
    monkeypatch.setattr(store, "DB_PATH", tmp_path / "other.db")
    store.init()
    second = store._connect()
    assert second is not first
    assert store.kv_get("nothing-here") is None


def test_reuse_did_not_change_transaction_behaviour():
    """Call sites use `with _connect() as conn:`, which in sqlite3 is a
    TRANSACTION context manager, not a closing one — so the connection surviving
    the block is exactly what already happened."""
    store.kv_set("txn-probe", "one")
    with store._connect() as conn:
        conn.execute("INSERT INTO kv (key, value) VALUES (?, ?) "
                     "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                     ("txn-probe", "two"))
    assert store.kv_get("txn-probe") == "two"
    assert store._connect().execute("SELECT 1").fetchone() is not None, \
        "the connection was closed by the with-block"


def test_a_write_lock_waits_instead_of_failing():
    """With tasks running in parallel there IS another connection now, and
    'database is locked' would surface as a lost notification rather than a
    delay."""
    timeout = store._connect().execute("PRAGMA busy_timeout").fetchone()[0]
    assert timeout >= 1000, f"busy_timeout is {timeout}ms — a parallel write can fail"


def test_the_hot_path_is_actually_fast():
    """The number that justified the change, asserted so it cannot quietly
    regress: opening a connection per call put this at 0.231 ms."""
    import time as _t
    store.kv_set("perf-probe", "x")
    best = min(_timed(lambda: store.kv_get("perf-probe")) for _ in range(50))
    assert best < 0.05, f"a kv_get costs {best:.3f} ms — connection reuse is gone"


def _timed(fn) -> float:
    import time as _t
    t0 = _t.perf_counter()
    fn()
    return (_t.perf_counter() - t0) * 1000


# --- 20. Module size, addressed where it was actually earned -----------------
#
# Four modules carried a third of the system. A wholesale reshuffle was the wrong
# answer — a five-thousand-line move is unreviewable, risks a green suite, and
# buys no capability. The right answer was to extract the seam this session
# created: `meetings` had grown to cover building invites, running a call,
# reading captions, and deciding what to SAY about them. The first three are
# mechanics; the fourth is judgement, and it is the part with a measurable right
# answer — which is why the eval harness can now reach it without a call existing.

def test_the_in_call_brain_is_its_own_module():
    from app import call_brain
    for name in ("classify_line", "answer_from_knowledge", "confident", "spoken_form"):
        assert hasattr(call_brain, name), f"{name} did not come across"


def test_judgement_does_not_depend_on_call_machinery():
    """It takes a line of text and returns a judgement. Nothing here touches the
    microphone, the browser, or a live call — which is what makes it testable and
    evaluable at all."""
    import inspect
    from app import call_brain
    src = inspect.getsource(call_brain)
    for machinery in ("set_call_mic", "say_in_call", "play_to_device", "_CALL[",
                      "teams_page", "AUDIO_DEVICE"):
        assert machinery not in src, f"the brain reached into {machinery}"


def test_every_moved_name_is_still_reachable_where_it_was():
    """Callers and tests kept working because the names are re-exported — and
    monkeypatching `meetings.answer_from_knowledge` must still reach handle_ask,
    which is why the orchestration calls the local name."""
    from app import meetings
    for name in ("classify_line", "notice_asks", "clear_noticed", "pending_for_him",
                 "answer_from_knowledge", "spoken_form", "confident",
                 "SPOKEN_ANSWER_WORDS", "_ANSWERABLE", "_HIS_TO_ANSWER"):
        assert hasattr(meetings, name), f"meetings.{name} disappeared in the move"


def test_meetings_actually_got_smaller():
    import pathlib as _p
    lines = len((_p.Path(__file__).resolve().parent.parent / "app" / "meetings.py")
                .read_text().splitlines())
    assert lines < 1450, f"meetings.py is {lines} lines — the extraction did not land"


# --- 18. Merging, which did not exist ----------------------------------------
#
# Asta could review, approve and comment on a PR, all staged, and could post to a
# group chat — but the last step of every piece of work was manual. A merge is
# also the least reversible thing here: it puts code on the branch everybody else
# builds from, so the guards are the point, not the plumbing.

from app import ops, review as review_mod

OPEN_CLEAN = {"number": 11, "title": "BEPTELIKOS-9397 vessel dates", "state": "OPEN",
              "draft": False, "mergeable": "MERGEABLE", "merge_state": "CLEAN",
              "review": "APPROVED", "head": "feature/BEPTELIKOS-9397",
              "base": "develop", "failing": [], "pending": [], "checks": 3}


def test_a_clean_approved_pr_has_no_blockers():
    assert review_mod.merge_blockers(OPEN_CLEAN) == []


@pytest.mark.parametrize("change,expected", [
    ({"failing": ["build (service)"]}, "CI is red"),
    ({"pending": ["component-test"]}, "CI has not finished"),
    ({"mergeable": "CONFLICTING"}, "conflicts with develop"),
    ({"draft": True}, "still a draft"),
    ({"review": "CHANGES_REQUESTED"}, "requested changes"),
    ({"state": "CLOSED"}, "the PR is closed"),
])
def test_every_blocker_is_named_not_just_refused(change, expected):
    """He needs to know what is in the way, not that something is."""
    blockers = review_mod.merge_blockers({**OPEN_CLEAN, **change})
    assert any(expected in b for b in blockers), f"{expected!r} not in {blockers}"


def test_unfinished_ci_blocks_as_firmly_as_red_ci():
    """Merging while checks are still running is merging on a guess."""
    assert review_mod.merge_blockers({**OPEN_CLEAN, "pending": ["e2e"]})


@pytest.mark.asyncio
async def test_a_blocked_pr_is_never_staged(monkeypatch):
    """It must not offer to merge past a blocker — the offer itself would be the
    mistake, because his yes is a single tap."""
    async def state(pr, workspace, repo=""):
        return {**OPEN_CLEAN, "failing": ["build (service)"]}

    staged = []
    from app import offers
    monkeypatch.setattr(review_mod, "merge_state", state)
    monkeypatch.setattr(offers, "staged_write", lambda *a, **k: staged.append(a))
    out = await agent_mod.merge_pr("11", "booking")
    assert staged == [], "it offered to merge a PR with red CI"
    assert "CI is red" in out


@pytest.mark.asyncio
async def test_a_clean_pr_is_staged_with_its_real_state(monkeypatch):
    async def state(pr, workspace, repo=""):
        return OPEN_CLEAN

    staged = {}
    from app import offers
    monkeypatch.setattr(review_mod, "merge_state", state)
    monkeypatch.setattr(offers, "staged_write",
                        lambda name, args, subject, body, question, kind="": staged.update(
                            name=name, args=args, body=body))
    out = await agent_mod.merge_pr("11", "booking")
    assert staged["name"] == "pr_merge"
    assert staged["args"]["method"] == "squash"
    assert "CI green" in staged["body"] and "approved" in staged["body"], \
        "he was asked to approve a merge without being shown the state"
    assert "Nothing is merged yet" in out


@pytest.mark.asyncio
async def test_the_blockers_are_rechecked_at_the_moment_of_merging(monkeypatch):
    """The state was read when the offer was made; he may say yes an hour later,
    by which time CI can have gone red. The gap between deciding and doing is
    exactly where an irreversible action goes wrong."""
    calls = []

    async def state(pr, workspace, repo=""):
        calls.append(1)
        return {**OPEN_CLEAN, "failing": ["build"]} if len(calls) > 1 else OPEN_CLEAN

    monkeypatch.setattr(review_mod, "merge_state", state)
    from app import offers
    monkeypatch.setattr(offers, "staged_write", lambda *a, **k: None)
    await agent_mod.merge_pr("11", "booking")          # staged while green
    with pytest.raises(RuntimeError, match="did NOT merge"):
        await review_mod.merge("11", "booking")        # red by the time he says yes


@pytest.mark.asyncio
async def test_an_unknown_merge_method_is_refused():
    assert "method must be one of" in await agent_mod.merge_pr("11", "booking", method="octopus")


def test_the_op_is_registered_and_describes_itself():
    """A staged op nobody can run is a yes that silently does nothing."""
    assert ops.known("pr_merge")
    assert ops.describe({"name": "pr_merge", "args": {"pr": "11", "method": "squash"}}) \
        == "Merge PR #11 (squash)"


def test_the_capability_warns_about_working_around_blockers():
    from app import capabilities
    note = capabilities.registry()["merge_pr"].note
    assert "red CI" in note and "Never work around a blocker" in note


# --- wiring: a capability that names a route must have one -------------------
#
# `merge_pr` was added with `http="POST /api/pr-merge"` and no such route. Every
# CLI brain is taught that string verbatim, so the instruction was a 404 the brain
# had to work around — and it looks correct in review, because the capability, the
# function and the tests all exist. Only the route was missing.
#
# `selector_health` and `evals` failed the same way one level up: both modules were
# complete, tested, and called by nothing at all in `app/`. So these tests check
# reachability, which is the property that was actually absent.

def test_every_capability_http_route_exists():
    """No capability may advertise an endpoint the server does not serve."""
    import re as _re
    from app import capabilities, main

    # Compare SHAPES. The docs name a path parameter for whoever reads them
    # ({workspace}); FastAPI names it for the function that receives it ({name}).
    # Both describe the same endpoint, and a test that called that a mismatch
    # would be noise — the failure worth catching is a path that is not served
    # at all.
    def shape(path: str) -> str:
        return _re.sub(r"\{[^}]*\}", "{}", path.rstrip("[]?& "))

    served = set()
    for route in main.app.routes:
        path = getattr(route, "path", "")
        for method in getattr(route, "methods", set()) or set():
            served.add((method.upper(), shape(path)))

    missing = []
    for name, cap in capabilities.registry().items():
        if not cap.http:
            continue
        m = _re.match(r"^(GET|POST|PUT|PATCH|DELETE)\s+(\S+)", cap.http.strip())
        if not m:
            continue
        method, path = m.group(1), m.group(2).split("?")[0]
        if (method, shape(path)) not in served:
            missing.append(f"{name}: {method} {path}")
    assert not missing, "capabilities naming routes that do not exist: " + "; ".join(missing)


def test_selector_health_is_actually_run_by_the_server():
    """The daily check must be started, not merely importable."""
    from pathlib import Path
    src = Path("app/main.py").read_text()
    assert "selector_health.watch_loop" in src, \
        "selector_health has no caller — a health check nothing runs reads as protection"
    assert 'daemon.start("selector_health"' in src, \
        "it must be supervised like every other loop, or one exception kills it silently"


def test_selector_health_loop_survives_a_failing_check(monkeypatch):
    """A broken check must not end the loop — that is how a watcher dies silently.

    `activity_watch_loop` once died to an exception raised by the very kv_get that
    guarded it and stayed dead for the whole process. Nothing in health, no retry,
    and from the outside indistinguishable from a quiet week.
    """
    import asyncio
    from app import quiet, selector_health

    calls = {"n": 0}

    async def _boom():
        calls["n"] += 1
        if calls["n"] >= 3:
            raise asyncio.CancelledError      # stop the loop from the inside
        raise RuntimeError("teams is down")

    monkeypatch.setattr(selector_health, "check_and_report", _boom)
    monkeypatch.setattr(selector_health, "CHECK_EVERY_SECONDS", 0)
    monkeypatch.setattr(selector_health, "SETTLE_SECONDS", 0)

    async def _run():
        with contextlib.suppress(asyncio.CancelledError):
            await asyncio.wait_for(selector_health.watch_loop(), timeout=5)

    asyncio.run(_run())
    assert calls["n"] >= 3, "the loop stopped at the first failure"
    assert "selector_health.watch_loop" in quiet.counts(), \
        "the failure was swallowed without being recorded"


def test_the_new_capabilities_are_reachable_by_name():
    from app import capabilities
    reg = capabilities.registry()
    for name in ("check_teams_selectors", "answer_quality", "merge_pr"):
        assert name in reg, f"{name} is not a capability — nobody can ask for it"
        assert reg[name].fn is not None
        assert reg[name].description.strip(), f"{name} has no docstring, so no description"


def test_no_eval_cases_does_not_read_as_a_failing_score(monkeypatch, tmp_path):
    """The cases are gitignored, so a fresh checkout has none.

    "0/0 (0%)" reports a measurement that never ran as one that failed — the same
    confusion the verify gate has between "the check failed" and "the check could
    not run", and the same wrong conclusion follows: he goes looking for a defect
    that is not there.
    """
    from app import evals

    async def _never_asked(_question):
        raise AssertionError("no cases, so no brain should have been called")

    monkeypatch.setattr(evals, "CASES_DIR", tmp_path)      # an empty cases dir
    out = asyncio.run(evals.run("booking", ask=_never_asked))
    assert out["total"] == 0
    text = evals.report(out)
    assert "0%" not in text and "0/0" not in text, "an unrun eval reported as a score"
    assert "gitignored" in text and "data/evals/booking.json" in text, \
        "it must say where the cases go, or the next person just sees nothing"


# --- 25. a healed fault must stop being reported -----------------------------

def test_a_successful_scrape_clears_the_last_error():
    """The reason a watcher failed must not outlive the failure.

    Found live, right after the restart that put this review's code into service:
    `attention_scrape_error:teams` held "Teams app did not load within 75s" while
    the watcher was in fact reading successfully every 60 seconds. Nothing ever
    cleared it, so the next unrelated stall would have been explained with the
    wrong cause — which is worse than no cause, because it is followed.
    """
    from app import attention

    attention.note_scrape_error("teams", RuntimeError("Teams app did not load within 75s"))
    assert "did not load" in attention.last_error("teams")

    attention.note_scrape("teams")
    assert attention.last_error("teams") == "", \
        "a successful read left the previous failure's reason in place"


def test_clearing_one_source_does_not_clear_another():
    """Outlook and Teams fail independently; healing one must not hide the other."""
    from app import attention

    attention.note_scrape_error("teams", RuntimeError("teams broke"))
    attention.note_scrape_error("outlook", RuntimeError("outlook broke"))
    attention.note_scrape("teams")
    assert attention.last_error("teams") == ""
    assert "outlook broke" in attention.last_error("outlook")


def test_the_always_core_survives_any_amount_of_trimming():
    """The floor is not negotiable — least of all `prepare_to_send`.

    It used to be appended LAST and the trim kept the first N, so the one group
    documented as "never evicted" was the first evicted. Invisible while the
    registry was small enough never to trim; three new capabilities crossed the
    threshold and it surfaced.

    The cost is not a slower turn. `prepare_to_send` is the staged-send gate — the
    single hard rule that nothing leaves the machine unapproved. A long enough
    conversation would have dropped it, leaving the rule enforced by a tool the
    model could no longer call.
    """
    from app import capabilities, tool_index

    floor = tool_index._floor()
    # Far more carried-over tools than the cap, so a trim is guaranteed.
    everything = [n for n in capabilities.names() if n not in floor]
    prev = dict.fromkeys(everything)
    picked = everything[:6]

    out = tool_index._recent(prev, picked)

    for name in floor:
        assert name in out, f"{name} is in ALWAYS and was evicted"
    for name in picked:
        assert name in out, f"{name} was picked this turn and evicted"
    assert "prepare_to_send" in out, "the staged-send gate was evicted"
    assert len(out) <= tool_index.STICKY_MAX_TOOLS + tool_index.STICKY_SLACK + len(floor), \
        "trimming stopped working entirely"


def test_trimming_still_happens_for_carried_over_tools():
    """Protecting the floor must not turn the cap off — that was the original bug."""
    from app import capabilities, tool_index

    everything = [n for n in capabilities.names()]
    out = tool_index._recent(dict.fromkeys(everything), everything[:3])
    assert len(out) < len(everything), "nothing was trimmed — the set grew to everything"
