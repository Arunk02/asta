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
