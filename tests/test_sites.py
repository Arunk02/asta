"""Any corporate web app, configured in one file — Round 3, and his ask.

"make this as generic not just solar, tmr even if move to other teams also it
should be easy for me to configure"

A door shaped around one system has to be rewritten the day the system changes,
and the parts that matter — which environments may be written to, how to sign
in, how to read a page, what a field expects — are the same for Solar, Kevin,
LaunchMate or whatever comes next. Configuration lives in data/sites.json, which
is gitignored, for the same reason data/temporal-envs.json is: his hostnames are
not in a public repository.
"""

from __future__ import annotations

import asyncio
import json

import pytest

from app import sites, store

#: Stand-ins. His real URLs live in data/sites.json and are not in this file.
TWO_SITES = {
    "solar": {"label": "Solar · inland bookings",
              "envs": {"sit": "https://solar-sit.example.com",
                       "uat": "https://solar-uat.example.com",
                       "prod": "https://solar.example.com"},
              "write": ["sit", "uat"], "landing": "/inland/booking/", "http1": True},
    "kevin": {"label": "Kevin · billing",
              "envs": {"sit": "https://kevin-sit.example.com"},
              "write": []},
}


@pytest.fixture(autouse=True)
def _clean(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "DB_PATH", tmp_path / "t.db", raising=False)
    store.init()
    yield


def _configure(data=None) -> None:
    sites.config_path().write_text(json.dumps(data if data is not None else TWO_SITES))


# --- configured in one file --------------------------------------------------------------

def test_nothing_exists_until_the_file_does():
    assert sites.all_sites() == {}
    assert "No sites configured" in sites.configured()
    assert not sites.allowed("solar", "https://solar-sit.example.com/x")


def test_a_broken_file_is_not_a_crash():
    sites.config_path().write_text("{ not json at all")
    assert sites.all_sites() == {}
    assert "No sites configured" in sites.configured()


def test_adding_a_second_system_needs_no_code():
    """The whole point: tomorrow's team is a file edit."""
    _configure()
    assert sorted(sites.all_sites()) == ["kevin", "solar"]
    assert sites.allowed("kevin", "https://kevin-sit.example.com/invoices")
    assert not sites.allowed("kevin", "https://solar-sit.example.com/x")


def test_an_unknown_site_is_declined_by_name():
    _configure()
    assert "not configured" in sites.configured("launchmate")
    assert "kevin" in sites.configured("launchmate")


# --- read anywhere, write only where he said ---------------------------------------------

def test_every_environment_can_be_read_including_production():
    _configure()
    assert sites.allowed("solar", "https://solar.example.com/booking/88271")
    assert set(sites.envs("solar")) == {"sit", "uat", "prod"}


def test_only_the_write_list_can_be_changed():
    _configure()
    assert sites.writable("solar", "sit") and sites.writable("solar", "uat")
    assert not sites.writable("solar", "prod")
    assert not sites.writable("kevin", "sit"), "writable with an empty write list"


@pytest.mark.parametrize("write", [["prod"], ["sit", "prod"], ["PROD"]])
def test_production_can_never_be_made_writable(write):
    data = json.loads(json.dumps(TWO_SITES))
    data["solar"]["write"] = write
    _configure(data)
    assert not sites.writable("solar", "prod")


def test_a_production_url_under_another_name_is_still_not_writable():
    _configure({"x": {"envs": {"staging": "https://x-production.example.com"},
                      "write": ["staging"]}})
    assert sites.allowed("x", "https://x-production.example.com/a")
    assert not sites.writable("x", "staging")


def test_rubbish_is_refused_rather_than_parsed():
    _configure()
    for bad in ("", "not a url", "javascript:alert(1)", "file:///etc/passwd"):
        assert not sites.allowed("solar", bad)


# --- reading cannot touch -----------------------------------------------------------------

def test_looking_at_a_page_clicks_nothing():
    """What makes `look` safe to point at production is the absence of these."""
    import inspect
    src = inspect.getsource(sites.look) + inspect.getsource(sites._look_once) + sites._LOOK
    for forbidden in (".click(", ".fill(", ".press(", ".select_option(", ".check(",
                      ".submit(", ".set_input_files("):
        assert forbidden not in src, f"look() can {forbidden}"


def test_a_look_reports_the_path_and_never_the_host():
    import inspect
    assert "page.url[len(base):]" in inspect.getsource(sites._look_once)


def test_health_names_environments_and_never_their_urls():
    _configure()
    out = sites.health()
    assert "example.com" not in json.dumps(out), "a URL leaked into the health report"
    solar = next(s for s in out["sites"] if s["site"] == "solar")
    assert solar["writable"] == ["sit", "uat"] and solar["read_only"] == ["prod"]
    assert solar["http1"] is True


def test_a_quirk_belongs_to_its_own_site():
    """Solar needs HTTP/1.1 because its API dies on HTTP/2 in this browser. The
    next site must not inherit that."""
    _configure()
    assert sites.site("solar").get("http1") is True
    assert not sites.site("kevin").get("http1")


# --- a filter is not a change -------------------------------------------------------------

def test_a_filter_is_allowed_where_reading_is():
    """Narrowing a list changes what he is looking at, not what the system
    holds — and on production it is exactly how a live problem gets found."""
    import inspect
    src = inspect.getsource(sites.choose)
    assert "writable(" not in src, "a filter was gated as if it were a write"
    assert "input_value()" in src, "the filter is not read back"


def test_a_filter_on_an_unconfigured_site_is_refused():
    out = asyncio.run(sites.choose("solar", "sit", "Booking id", "88271"))
    assert "No sites configured" in out["error"]


# --- filling a form is a change, and is checked against his documents ---------------------

def test_a_form_is_never_planned_against_a_read_only_environment():
    _configure()
    out = asyncio.run(sites.plan_fill("solar", "prod", {"Booking id": "88271"}))
    assert "read-only" in out["error"] and "sit" in out["error"]


def test_planning_types_nothing_and_submits_nothing():
    _configure()
    out = asyncio.run(sites.plan_fill("solar", "sit", {"Origin city": "Chicago"}))
    assert out["submitted"] is False
    assert "Nothing is typed and nothing is submitted" in out["say"]


def test_a_field_his_documents_do_not_describe_is_named_as_such(monkeypatch):
    """A form filled from imagination is the automation that confidently submits
    something wrong. An undocumented field is surfaced, not quietly guessed."""
    _configure()
    monkeypatch.setenv("ASTA_KNOWLEDGE_DIR", str(sites.config_path().parent / "empty"))
    out = asyncio.run(sites.plan_fill("solar", "sit", {"Quibblesnort code": "7"}))
    assert out["undocumented"] == ["Quibblesnort code"]
    assert "not described in his documents" in out["say"]


def test_a_documented_field_comes_back_with_its_citation(tmp_path, monkeypatch):
    _configure()
    folder = tmp_path / "docs"
    folder.mkdir()
    (folder / "booking.md").write_text(
        "## Origin city\nThe origin city must be the inland city, not the port.\n")
    monkeypatch.setenv("ASTA_KNOWLEDGE_DIR", str(folder))
    out = asyncio.run(sites.plan_fill("solar", "sit", {"Origin city": "Chicago"}))
    field = out["fields"][0]
    assert field["documented"] and "booking.md" in field["cite"]
    assert "inland city" in field["says"]
    assert out["undocumented"] == []


def test_guidance_says_plainly_when_his_documents_are_silent(tmp_path, monkeypatch):
    monkeypatch.setenv("ASTA_KNOWLEDGE_DIR", str(tmp_path / "nothing"))
    out = asyncio.run(sites.guidance("Quibblesnort code"))
    assert out["found"] is False and "say nothing" in out["say"]


# --- the injected scripts have to be valid JavaScript ------------------------------------

@pytest.mark.parametrize("script", ["_LOOK", "_FIND"])
def test_no_injected_script_contains_a_broken_string(script):
    """Live, 26 Sep: a Python-escaped "\\n" became a real newline inside a JS
    single-quoted string, and every read came back "SyntaxError: Invalid or
    unexpected token" — from inside the page, where a traceback does not point at
    the line that wrote it."""
    import re
    js = getattr(sites, script)
    # Per line, because that is what the bug looked like: one line whose string
    # was left open by a newline that should have been the two characters \n.
    unclosed = [(n + 1, line.strip()[:60]) for n, line in enumerate(js.split("\n"))
                if len(re.findall(r"(?<!\\)'", line)) % 2]
    assert not unclosed, f"a string literal is left open: {unclosed}"
    assert js.count("{") == js.count("}") and js.count("(") == js.count(")")


def test_the_controls_are_described_by_the_words_beside_them():
    """A react-select is an input called react-select-2-input with the useful
    words three elements up. The id is no use to a person or to a brain."""
    assert "beside" in sites._LOOK and "label[for=" in sites._LOOK


def test_a_control_is_found_by_its_label_and_never_by_a_coordinate():
    assert "getBoundingClientRect" not in sites._FIND and "clientX" not in sites._FIND
    assert "aria-label" in sites._FIND
