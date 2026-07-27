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

import pytest

from app import store


@pytest.fixture(autouse=True)
def _isolated_db(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "DB_PATH", tmp_path / "asta-test.db", raising=False)
    store.init()
    yield
