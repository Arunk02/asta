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


@pytest.fixture(autouse=True)
def _no_wall_clock_dependence(monkeypatch):
    for name in _TIME_DEPENDENT_ENV:
        monkeypatch.delenv(name, raising=False)
    yield


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
