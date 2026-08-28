"""No test ever touches the live database.

Most test files already pointed `store.DB_PATH` at a tmp file themselves, but that
is a rule enforced by everyone remembering it — and the one file that forgets does
not fail, it silently writes rows into `data/asta.db`. A stray `pending_offer`
left there is not a test problem: it is a question Asta will ask Arun on his phone
about work that never happened.

So the isolation is global and automatic. Files that set DB_PATH themselves still
work — they just override an already-safe default.
"""

from __future__ import annotations

import contextlib
import os

import pytest

from app import store


@pytest.fixture(autouse=True)
def _isolated_db(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "DB_PATH", tmp_path / "asta-test.db", raising=False)
    store.init()
    yield


#: Settings whose value depends on WHEN the suite runs, or on how this particular
#: machine is configured. Same argument as the database above: a rule everybody has
#: to remember is a rule that gets forgotten, and forgetting is silent.
#:
#: This one was found the hard way. `ASTA_QUIET_HOURS=22:00-07:00` is set in the
#: real .env, tests load it, and `flush_held` correctly refuses to push during quiet
#: hours — so three hold-window tests passed all day and failed at 23:42 with an
#: IndexError that says nothing about the clock. A suite that is green at noon and
#: red at midnight teaches people to re-run it rather than read it.
#:
#: Cleared for every test. A test that is ABOUT quiet hours sets the window itself
#: with monkeypatch.setenv, which still works and now states its intent out loud.
_TIME_DEPENDENT_ENV = ("ASTA_QUIET_HOURS",)

#: Settings that decide what a test's FIXTURES mean. Same argument as the clock
#: above, found the same way — by something failing where the real .env is absent.
#:
#: `ASTA_CONTEXT_DIRNAMES=.contmark` is set in Arun's .env, and the context tests
#: build workspaces containing a `.contmark/` directory. So they passed on his
#: laptop and, on the first CI run ever, failed on seven assertions that all said
#: some version of "the context is fine" — because with the default dirname the
#: provider looked for `.asta-context`, found nothing, and reported nothing stale.
#: A green suite that depends on one developer's .env is not a green suite.
#:
#: Cleared for every test. A test that is ABOUT a non-default context directory
#: sets it itself, which states the assumption instead of inheriting it.
_FIXTURE_SHAPING_ENV = ("ASTA_CONTEXT_DIRNAME", "ASTA_CONTEXT_DIRNAMES")

#: Settings this machine PINS that the code under test falls back to. Third
#: member of the same family, and the one that would have bitten next: the model
#: tier is "his stored choice, else the environment", and Arun's .env pins
#: ASTA_CLAUDE_CLI_MODEL=claude-sonnet-5. A test asserting what an unset tier
#: does would therefore pass here and fail on any machine that leaves it blank.
#:
#: `ASTA_RESPOND` joined them the day it was added. It is on in Arun's .env, so
#: with it inherited the responder fired inside attention tests and changed what
#: they observed — three failures whose messages were all about the ledger and
#: none about the responder. A behaviour switched on for one machine is not a
#: behaviour the suite should be silently exercising.
_MACHINE_PINNED_ENV = ("ASTA_CLAUDE_CLI_MODEL", "ASTA_TURN_IDLE", "ASTA_RESPOND",
                       "ASTA_CHATWATCH", "ASTA_INCOMING")


#: What this machine's .env said, captured before it is cleared. A handful of
#: tests are genuinely ABOUT the live workspace — checking that eval ground truth
#: still matches the lessons it cites — and those need the real directory name
#: back. Handed over through a fixture so the need is declared rather than
#: inherited, and so a test that forgets to ask gets the default like everyone else.
_REAL_CONTEXT_DIRNAMES = {n: os.environ.get(n) for n in
                          ("ASTA_CONTEXT_DIRNAME", "ASTA_CONTEXT_DIRNAMES")}


@pytest.fixture(autouse=True)
def _no_wall_clock_dependence(monkeypatch):
    for name in _TIME_DEPENDENT_ENV + _FIXTURE_SHAPING_ENV + _MACHINE_PINNED_ENV:
        monkeypatch.delenv(name, raising=False)
    yield


@pytest.fixture
def live_workspace_context(monkeypatch):
    """Restore this machine's real context directory name.

    For the few tests that read Arun's actual workspace rather than a fixture.
    They skip where it is absent — which is every CI runner — so restoring the
    name costs nothing there and is the difference between measuring the real
    thing and measuring a default that matches no directory on disk.
    """
    restored = False
    for name, value in _REAL_CONTEXT_DIRNAMES.items():
        if value:
            monkeypatch.setenv(name, value)
            restored = True
    # Yields whether there was anything to restore, so a test can skip rather
    # than validate against a default that matches no directory on disk. Without
    # this the ground-truth test read the generic AGENTS.md fallback — non-empty,
    # so its "skip if empty" guard did not fire — and asserted against text that
    # was never going to contain the facts it was checking for.
    yield restored


@pytest.fixture(autouse=True)
def _a_workspace_to_run_code_in(request, tmp_path, monkeypatch):
    """A registered workspace for the whole suite.

    Code tasks now REFUSE to run without one — `tasks.code_cwd` raises rather
    than falling back to Asta's own root, because that fallback is how a task
    with no workspace once ran real git commands in this repository and moved a
    branch carrying five unpushed commits.

    Most task tests were written when the fallback existed and create their tasks
    with `workspace=None`. Rather than teach every one of them about workspaces,
    the suite gets a real registered one pointing at a tmp directory — which is
    also the right default: a test that runs a code task somewhere unspecified
    should land somewhere disposable, never in the working tree.
    """
    from app import workspace as workspace_mod, workspace_tools
    # The registry's OWN tests exercise this view — add/get/remove and the
    # "known workspaces" listing — so they must see the real thing, not a stub.
    if request.module.__name__.endswith("test_workspace"):
        yield None
        return
    root = tmp_path / "test-workspace"
    root.mkdir(parents=True, exist_ok=True)
    # BOTH modules, because `workspace_tools` re-exports the name at import time:
    # patching only `app.workspace` leaves `tasks` looking at the real registry,
    # and a code task resolving `None` would then land in Arun's actual booking
    # workspace. Same rule as the database — no test reaches live state.
    stub = {"test-workspace": root}
    monkeypatch.setattr(workspace_mod, "WORKSPACES", stub, raising=False)
    monkeypatch.setattr(workspace_tools, "WORKSPACES", stub, raising=False)
    yield root


@pytest.fixture(autouse=True)
def _no_carried_over_browser():
    """No test inherits another test's Teams browser.

    The bridge now keeps ONE context alive across operations — that is the whole
    2.49-seconds-per-operation fix. Module-level state, so a test that stubs the
    launcher leaves its fake in the pool and the next test silently reuses it,
    failing somewhere unrelated to the change that broke it. Same argument as the
    database and the speech cache.
    """
    from app import teams_bridge
    teams_bridge._POOL.clear()
    yield
    teams_bridge._POOL.clear()


@pytest.fixture(autouse=True)
def _no_carried_over_speech():
    """Each test synthesises its own audio.

    Speech is cached by (voice, text) so a call never pays to say "let me check
    that" twice — which is the whole latency fix, and exactly wrong between
    tests. A line one test spoke is served from memory in the next, the `speak`
    monkeypatch is never called, and the assertion fails somewhere unrelated to
    the change that broke it. Same argument as the database above.
    """
    from app import meetings
    meetings._VOICE_CACHE.clear()
    yield
    meetings._VOICE_CACHE.clear()


@pytest.fixture(autouse=True)
def _no_live_brains(monkeypatch):
    """No test may spend money or spawn a subprocess to reach a model.

    `cheap_complete` escalates past the local model to an API key and then to a
    CLI subscription, which is the right behaviour in production and completely
    wrong in a test: one assertion quietly launched a real `copilot -p` and the
    suite went from 22 seconds to 76. Worse than the delay is what it means —
    tests that reach live brains bill real money, need network, and fail for
    reasons that have nothing to do with the code under test.

    Blocked here rather than per file, for the same reason DB_PATH is: a rule
    everyone has to remember is a rule that gets forgotten once. A test that
    genuinely wants the paid path patches these back itself.
    """
    from app import agent as agent_mod

    def _no_api():
        raise RuntimeError("tests must not reach a hosted model")

    async def _no_cli(*a, **k):
        raise RuntimeError("tests must not spawn a CLI brain")

    monkeypatch.setattr(agent_mod, "best_model_name", _no_api, raising=False)
    for mod in ("copilot_cli", "claude_cli"):
        with contextlib.suppress(ImportError, AttributeError):
            monkeypatch.setattr(f"app.{mod}.one_shot", _no_cli, raising=False)
    yield


# --- tests about THIS machine's install --------------------------------------
#
# A few tests assert facts about Arun's real setup rather than about code: that
# booking is the only registered workspace, that each of his repos has a verify
# command, that the eval cases are grounded in lessons that still say what they
# cited. They are worth having — they catch his configuration drifting — and they
# cannot hold anywhere else, because everything they read lives under `data/`,
# which is gitignored for holding the database, OAuth tokens and session cookies.
#
# So they SKIP off this machine instead of failing. That distinction matters: a
# red CI run that means "this is not Arun's laptop" trains people to ignore red
# runs, which is worth more than the tests are.

def _needs(path: str, why: str):
    from pathlib import Path as _P
    if not _P(path).exists():
        pytest.skip(f"{why} — {path} is gitignored and absent here")


@pytest.fixture
def live_verify_commands():
    """His per-repo verify commands. Local-only: they name work repos."""
    _needs("data/verify-commands.json", "no verify commands on this machine")
    yield


@pytest.fixture
def live_eval_cases():
    """The grounded eval cases. Local-only: they quote internal names."""
    from app import evals
    if not evals.load():
        pytest.skip("no eval cases on this machine — data/evals/ is gitignored")
    yield


@pytest.fixture
def live_workspaces():
    """His registered workspaces, which live in the gitignored data/ config."""
    from app.workspace import registry
    if not list(registry.all_workspaces()):
        pytest.skip("no workspaces registered on this machine")
    yield


#: The two edges a test must never actually cross.
#:
#: Found the way these always are. A test wrote a stub for `meetings` that did not
#: take — `from . import meetings` resolves the package ATTRIBUTE, not the
#: `sys.modules` entry a `setitem` had replaced — so the real call path ran:
#: `set_call_mic` switched Arun's system input to BlackHole and left it there, and
#: the next Teams call he takes has no working microphone. It got as far as opening
#: a browser before the test timed out. Nothing dialled, that time.
#:
#: `app.sandbox` already had this guarantee and the bench already used it. The test
#: suite — which runs two thousand times more often — did not.
#:
#: Guarding the EDGES rather than the named operations, because the first attempt
#: blocked `call_person`, `join` and `set_call_mic` themselves and broke twenty
#: tests whose whole subject is that machinery. Those tests mock the layer below;
#: this is that layer:
#:
#:   teams_bridge._launch   no browser, so nothing can be dialled or typed
#:   meetings.SWITCH_AUDIO  a path that does not exist, so his input cannot move
#:
#: A test about either patches it itself, which still works and says so out loud.
@pytest.fixture(autouse=True)
def _no_outward_moves(monkeypatch):
    from app import meetings, teams_bridge

    async def _no_browser(*_a, **_k):
        raise AssertionError(
            "teams_bridge._launch was called for real in a test — that opens a "
            "browser on Arun's machine and can dial a colleague. Patch it.")

    monkeypatch.setattr(teams_bridge, "_launch", _no_browser)
    monkeypatch.setattr(meetings, "SWITCH_AUDIO",
                        "/nonexistent/SwitchAudioSource-blocked-in-tests")
