"""Memory through the real API over HTTP: remember, scoped recall, correct, forget.

The server process owns the memory folder; this test only speaks HTTP to it,
as a CLI brain's tool calls do.
"""
from __future__ import annotations

import httpx
import pytest

from tests.e2e.harness import server

OWNER = {"Authorization": "Bearer e2e-owner-token"}


@pytest.fixture
def api(tmp_path):
    with server(tmp_path / "e2e.db") as url:
        def call(method, path, **kw):
            r = httpx.request(method, url + path, headers=OWNER, timeout=20, **kw)
            assert r.status_code == 200, r.text
            return r.json()["result"]
        yield call


def test_a_busy_project_does_not_hide_the_asked_one_and_recall_carries_content(api):
    for i in range(30):
        api("POST", "/api/memory", json={"title": f"Retry note {i}", "workspace": "billing",
                                         "fact": f"Retry timeout retry timeout is {i}s in billing."})
    api("POST", "/api/memory", json={"title": "Booking note", "workspace": "booking",
                                     "fact": "Booking retry timeout is 45s."})
    found = api("GET", "/api/memory/search", params={"q": "retry timeout", "workspace": "booking"})
    assert "Booking retry timeout is 45s" in str(found)
    assert "in billing" not in str(found)


def test_a_correction_replaces_the_fact_in_recall_and_keeps_the_old_one(api):
    path = api("POST", "/api/memory", json={"title": "QA cluster", "workspace": "booking",
                                            "fact": "QA runs on cluster alpha."})
    stored = path.split("facts/", 1)[1].split()[0].rstrip(".")
    said = api("POST", "/api/memory/correct", json={
        "path": f"facts/{stored}", "title": "QA cluster", "fact": "QA runs on cluster beta since 3.1.6.",
        "why": "moved in release 3.1.6"})
    assert "superseded" in said
    found = str(api("GET", "/api/memory/search", params={"q": "QA cluster", "workspace": "booking"}))
    assert "cluster beta" in found and "cluster alpha" not in found


def test_forgetting_over_http_is_refused_without_his_words(api):
    path = api("POST", "/api/memory", json={"title": "Alpha", "fact": "Alpha uses namespace one."})
    stored = path.split("facts/", 1)[1].split()[0].rstrip(".")
    said = api("POST", "/api/memory/forget", json={"path": f"facts/{stored}"})
    assert said.startswith("Not deleted")
    assert "namespace one" in str(api("GET", "/api/memory/search", params={"q": "alpha namespace"}))
