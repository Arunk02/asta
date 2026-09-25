"""The Solar fence — Round 3.

Solar has a duplicate page for any booking, which is the point and also the
danger: an assistant that can duplicate a booking is an assistant that can
duplicate a real customer's booking.

The first version of this refused production outright, and that was the wrong
shape. He reads production to debug — his own account has no write access there
— so refusing it took away something real to prevent something that was never
possible. The fence is now on what may be DONE, not on which environments exist.
"""

from __future__ import annotations

import asyncio

import pytest

from app import solar

#: Stand-ins. His real URLs live in .env, which is gitignored, and are not in
#: this file, the example env, or any log line.
FOUR = ("sit=https://solar-sit.example.com,uat=https://solar-uat.example.com,"
        "prod=https://solar.example.com")


def test_nothing_is_allowed_until_he_says_so(monkeypatch):
    """No default URL, no guess. Asta has never been told where Solar lives."""
    monkeypatch.delenv("ASTA_SOLAR_ENVS", raising=False)
    assert solar.envs() == {}
    assert not solar.allowed("https://solar-sit.example.com/booking/1")
    assert "not configured" in solar.configured()


def test_every_environment_he_named_can_be_read(monkeypatch):
    """Production included — reading it is how a live problem gets debugged."""
    monkeypatch.setenv("ASTA_SOLAR_ENVS", FOUR)
    assert set(solar.envs()) == {"sit", "uat", "prod"}
    assert solar.allowed("https://solar.example.com/booking/88271")
    assert solar.allowed("https://solar-sit.example.com/booking/88271/duplicate")
    assert not solar.allowed("https://solar-other.example.com/booking/1")


def test_only_what_he_marked_writable_can_be_changed(monkeypatch):
    monkeypatch.setenv("ASTA_SOLAR_ENVS", FOUR)
    monkeypatch.setenv("ASTA_SOLAR_WRITE", "sit")
    assert solar.writable("sit")
    assert not solar.writable("uat"), "writable by being readable"
    assert solar.writable("https://solar-sit.example.com/booking/1/duplicate")
    assert not solar.writable("https://solar-uat.example.com/booking/1/duplicate")


@pytest.mark.parametrize("write_list", ["prod", "sit,prod", "prod,uat,sit"])
def test_production_can_never_be_made_writable(write_list, monkeypatch):
    """Excluded in code, not by leaving it out of a list — the list is his to
    edit and this rule is not. A duplicate submitted against production is a
    real booking for a real customer."""
    monkeypatch.setenv("ASTA_SOLAR_ENVS", FOUR)
    monkeypatch.setenv("ASTA_SOLAR_WRITE", write_list)
    assert not solar.writable("prod")
    assert not solar.writable("https://solar.example.com/booking/1/duplicate")


def test_a_production_url_under_another_name_is_still_not_writable(monkeypatch):
    """Renaming it does not change what it is."""
    monkeypatch.setenv("ASTA_SOLAR_ENVS", "staging=https://solar-production.example.com")
    monkeypatch.setenv("ASTA_SOLAR_WRITE", "staging")
    assert solar.allowed("https://solar-production.example.com/booking/1")
    assert not solar.writable("staging")


def test_nothing_is_writable_when_he_has_not_said_so(monkeypatch):
    monkeypatch.setenv("ASTA_SOLAR_ENVS", FOUR)
    monkeypatch.delenv("ASTA_SOLAR_WRITE", raising=False)
    assert [e for e in solar.envs() if solar.writable(e)] == []


def test_rubbish_is_refused_rather_than_parsed(monkeypatch):
    monkeypatch.setenv("ASTA_SOLAR_ENVS", FOUR)
    for bad in ("", "not a url", "javascript:alert(1)", "file:///etc/passwd"):
        assert not solar.allowed(bad)
        assert not solar.writable(bad)


def test_an_unnamed_environment_is_declined_by_name(monkeypatch):
    monkeypatch.setenv("ASTA_SOLAR_ENVS", "sit=https://solar-sit.example.com")
    said = asyncio.run(solar.login("preprod"))
    assert "not in" in said and "sit" in said


def test_health_names_environments_and_never_their_urls(monkeypatch):
    """His URLs are his. They stay in .env and out of every report."""
    monkeypatch.setenv("ASTA_SOLAR_ENVS", FOUR)
    monkeypatch.setenv("ASTA_SOLAR_WRITE", "sit")
    out = solar.health()
    assert out["writable"] == ["sit"] and "prod" in out["read_only"]
    assert "example.com" not in repr(out), "a URL leaked into the health report"
