"""P1–P5, 30 Sep: from "reads and tells" towards "does the work, like him".

P1 the reader stops falling back: retried smaller, then a second model, and the
   tool-less CLI loads no MCP servers (measured +2.3s each, and a hang risk).
P2 drafts sound like HIM: his own recent messages to that person, and what he
   changed when he edited earlier drafts.
P3 a task INSIDE an app — "open excel add a column" — is carried out and checked.
P4 investigations check the way an engineer would: an id is traced in EVERY
   environment first (booking H65ZMWX52B2 was in preprod; the search that said
   "no trace" used `telikosprod`, a namespace that does not exist).
P5 someone wants him → a reply in his voice is staged, so "yes" answers them.
"""

from __future__ import annotations

import asyncio
import json
import time

import pytest

from test_round4_phase_d import _decide, _m, phone, rail  # noqa: F401  (fixtures)

from app import store


# --- P1 -------------------------------------------------------------------------------

def _items(n):
    return [{"id": f"teams:p{i}", "who": f"P{i}", "new": ["can you check booking 1234567890?"],
             "so_far": ""} for i in range(n)]


def _reply_for(prompt_text):
    ids = [b["id"] for b in json.loads(prompt_text.split("Conversations:\n", 1)[1])]
    return json.dumps({"threads": [{"id": i, "state": "ask", "need": "check"} for i in ids]})


def test_a_failed_chunk_is_retried_smaller_before_the_rules(monkeypatch):
    from app import understand
    monkeypatch.setenv("ASTA_UNDERSTAND_MODEL", "haiku")
    monkeypatch.delenv("ASTA_UNDERSTAND_FALLBACK_MODEL", raising=False)
    calls = []

    async def flaky(text, *rest):
        n = len(json.loads(text.split("Conversations:\n", 1)[1]))
        calls.append(n)
        if n > understand.RETRY_CHUNK:
            raise RuntimeError("claude one-shot timed out")
        return _reply_for(text)

    monkeypatch.setattr(understand, "_call", flaky)
    got = asyncio.run(understand.read(_items(4)))
    assert all(d["source"] == "model" for d in got.values())
    assert calls[0] == 4 and all(c <= understand.RETRY_CHUNK for c in calls[1:])
    assert any(r["outcome"] == "recovered" for r in store.recent_outcomes(20)
               if r["kind"] == "understand")


def test_what_the_first_model_cannot_read_goes_to_the_second(monkeypatch):
    from app import understand
    monkeypatch.setenv("ASTA_UNDERSTAND_MODEL", "haiku")
    monkeypatch.setenv("ASTA_UNDERSTAND_FALLBACK_MODEL", "sonnet")
    used = []

    async def call(text, model_name=""):
        used.append(model_name or "haiku")
        if not model_name:
            return "I can't help with that."
        return _reply_for(text)

    monkeypatch.setattr(understand, "_call", call)
    got = asyncio.run(understand.read(_items(2)))
    assert all(d["source"] == "model" for d in got.values())
    assert used[-1] == "sonnet"


def test_the_rules_are_still_the_floor(monkeypatch):
    from app import understand
    monkeypatch.setenv("ASTA_UNDERSTAND_MODEL", "haiku")
    monkeypatch.setenv("ASTA_UNDERSTAND_FALLBACK_MODEL", "sonnet")

    async def down(text, model_name=""):
        raise RuntimeError("usage limit")

    monkeypatch.setattr(understand, "_call", down)
    got = asyncio.run(understand.read(_items(1)))
    assert got["teams:p0"]["source"] == "rules"


def test_a_tool_less_call_loads_no_mcp_servers(monkeypatch):
    # The suite seals claude_cli.one_shot so no test reaches a model; a fresh copy
    # of the module is the real function, with the spawn itself stubbed out.
    import importlib.util
    from app import claude_cli as sealed
    spec = importlib.util.spec_from_file_location("app._claude_cli_fresh", sealed.__file__)
    claude_cli = importlib.util.module_from_spec(spec)
    claude_cli.__package__ = "app"
    spec.loader.exec_module(claude_cli)
    seen = {}

    async def spawn(*cmd, **kw):
        seen["cmd"] = cmd
        raise RuntimeError("stop here")

    monkeypatch.setattr(claude_cli, "available", lambda: True)
    monkeypatch.setattr(claude_cli.asyncio, "create_subprocess_exec", spawn)
    with pytest.raises(RuntimeError):
        asyncio.run(claude_cli.one_shot("x", tools_off=True, model="haiku"))
    cmd = list(seen["cmd"])
    assert "--strict-mcp-config" in cmd
    assert cmd[cmd.index("--mcp-config") + 1] == '{"mcpServers":{}}'


# --- P2 -------------------------------------------------------------------------------

@pytest.fixture
def his_messages(monkeypatch):
    from app import chat_watch, style
    rows = [
        {"chat": "Vinish Kumar", "sender": "Arunkumar K", "text": "AP u deploy in SIT as well as pp bro ?", "sent_at": 5},
        {"chat": "Vinish Kumar", "sender": "Arunkumar K", "text": "bro u created a java project", "sent_at": 4},
        {"chat": "Vinish Kumar", "sender": "Arunkumar K", "text": "bro u created a java project 👍", "sent_at": 3},
        {"chat": "Vinish Kumar", "sender": "Arunkumar K", "text": "creating ?", "sent_at": 2},
        {"chat": "Vinish Kumar", "sender": "Arunkumar K", "text": "Could you tell me a bit more about what you need help with?", "sent_at": 1},
        {"chat": "Vinish Kumar", "sender": "Vinish Kumar", "text": "not his words at all here", "sent_at": 6},
        {"chat": "Aayush", "sender": "Arunkumar K", "text": "Hi Aayush - Good evening, this one was request triggered from our end", "sent_at": 7},
    ]

    def fake(chat=None, limit=0, **k):
        return [r for r in rows if chat is None or r["chat"] == chat]

    monkeypatch.setattr(store, "teams_messages", fake)
    monkeypatch.setattr(chat_watch, "is_from_him", lambda s: s == "Arunkumar K")
    store.record_outcome("thread", "said", subject="Vinish Kumar",
                         detail="Could you tell me a bit more about what you need help with?")
    return style


def test_drafts_learn_from_what_he_sent_that_person(his_messages):
    ex = his_messages.examples("Vinish Kumar")
    assert ex[0] == "AP u deploy in SIT as well as pp bro ?"
    assert "bro u created a java project" in ex
    assert sum("java project" in e for e in ex) == 1, "an emoji variant is not a new example"
    assert "creating ?" not in ex, "a fragment teaches nothing"
    assert not any("tell me a bit more" in e for e in ex), "Asta's own lines are not his voice"
    assert "not his words at all here" not in ex


def test_a_colleague_with_little_history_borrows_his_general_voice(his_messages):
    ex = his_messages.examples("Someone New")
    assert ex and any("Aayush" in e or "bro" in e for e in ex)


def test_his_edits_become_lessons(his_messages):
    from app import ledger
    for _ in range(3):
        ledger.record("send", "Navya R", "amended")
    rows = ledger.recent(3, "send")
    for r in rows:
        ledger.set_edit(r["id"], "Hi Navya, merged it now, thanks a lot for waiting!", "Merged.")
    assert his_messages.lessons() and "shorter" in his_messages.lessons()[0]
    assert "When he edited earlier drafts" in his_messages.rider("Vinish Kumar")


def test_every_reply_brief_carries_his_voice(his_messages):
    from app import answers
    assert "AP u deploy in SIT" in answers.brief_rider("Vinish Kumar")


# --- P3 -------------------------------------------------------------------------------

@pytest.mark.parametrize("text,want", [
    ("open excel add a column", ("excel", "add a column")),
    ("open excel and add a column named Status", ("excel", "add a column named Status")),
    ("in keynote add a slide titled Q3 plan", ("keynote", "add a slide titled Q3 plan")),
    ("open youtube", None), ("open word", None), ("add a reminder", None),
])
def test_an_app_and_a_task_is_one_instruction(text, want):
    from app import app_tasks
    assert app_tasks.direct_ask(text) == want


def test_the_brain_is_given_the_tool_when_an_app_task_is_asked():
    from app import tool_index
    assert "do_in_app" in tool_index.required_for("can you open word and draft a note to Navya")
    assert "do_in_app" not in tool_index.required_for("open word")


@pytest.fixture
def numbers(monkeypatch):
    from app import app_tasks
    ran = []
    plans = []

    async def ask(text):
        return plans.pop(0)

    async def run(script, timeout=60):
        ran.append(script)
        if "FAIL" in script:
            return 1, "execution error: Can't get column 9. (-1728)"
        if "DENY" in script:
            return 1, "Not authorized to send Apple events to Numbers. (-1743)"
        return 0, "Status" if script.startswith("CHECK") else ""

    monkeypatch.setenv("ASTA_APPS", "1")
    monkeypatch.setattr(app_tasks, "_ask_model", ask)
    monkeypatch.setattr(app_tasks, "osascript", run)
    monkeypatch.setattr(app_tasks, "resolve", lambda name: (__import__("pathlib").Path(
        "/Applications/Numbers.app"), "Microsoft Excel isn't installed here, so I used Numbers. "))
    return app_tasks, plans, ran


def _plan(script='tell application "Numbers" to add column', check="CHECK", **kw):
    return {"script": script, "check": check, "changes": "adds a column Status.", **kw}


def test_the_task_is_done_and_checked(numbers):
    app_tasks, plans, ran = numbers
    plans.append(_plan())
    line = asyncio.run(app_tasks.do("excel", "add a column named Status"))
    assert line.startswith("Microsoft Excel isn't installed") and "Checked: Status" in line
    assert "Status.." not in line
    assert len(ran) == 2


def test_a_failed_script_is_repaired_once(numbers):
    app_tasks, plans, ran = numbers
    plans += [_plan(script='tell application "Numbers" FAIL'), _plan()]
    assert "Done in Numbers" in asyncio.run(app_tasks.do("numbers", "add a column"))


def test_a_script_that_leaves_the_app_is_refused(numbers):
    app_tasks, plans, ran = numbers
    plans.append(_plan(script='tell application "Numbers"\ndo shell script "rm -rf ~"\nend tell'))
    line = asyncio.run(app_tasks.do("numbers", "add a column"))
    assert "unsafe" in line and ran == []


def test_something_that_cannot_be_undone_waits_for_his_yes(numbers):
    app_tasks, plans, ran = numbers
    plans.append(_plan(script='tell application "Numbers" to delete column 3',
                       changes="deletes column 3"))
    line = asyncio.run(app_tasks.do("numbers", "delete column 3"))
    assert "Say yes" in line and ran == []
    plans.append(_plan(script='tell application "Numbers" to delete column 3'))
    assert "Done" in asyncio.run(app_tasks.do("numbers", "delete column 3", confirmed=True))


def test_a_missing_permission_is_named_with_the_fix(numbers):
    app_tasks, plans, ran = numbers
    plans.append(_plan(script='tell application "Numbers" DENY'))
    line = asyncio.run(app_tasks.do("numbers", "add a column"))
    assert "Automation" in line and "Numbers" in line


def test_app_doors_off_means_nothing_runs(numbers, monkeypatch):
    app_tasks, plans, ran = numbers
    monkeypatch.setenv("ASTA_APPS", "0")
    assert "app doors are off" in asyncio.run(app_tasks.do("numbers", "add a column"))
    assert ran == []


def test_a_missing_detail_becomes_one_question(numbers):
    app_tasks, plans, ran = numbers
    plans.append({"script": "", "question": "Which file should I add it to?"})
    assert "Which file" in asyncio.run(app_tasks.do("numbers", "add a column"))


def test_the_dictionary_is_read_from_the_bundle(tmp_path):
    from app import app_tasks
    res = tmp_path / "X.app" / "Contents" / "Resources"
    res.mkdir(parents=True)
    (res / "X.sdef").write_text(
        '<dictionary><suite name="S"><command name="add column"/><command name="print"/>'
        '<class name="table"><property name="name"/><element type="column"/></class>'
        '<class name="unrelated widget"/></suite></dictionary>')
    d = app_tasks.dictionary(tmp_path / "X.app", "add a column")
    assert "add column" in d and "class table" in d and "unrelated" not in d


# --- P4 -------------------------------------------------------------------------------

@pytest.mark.parametrize("given,real", [
    ("", "telikos-prod"), ("telikosprod", "telikos-prod"), ("uat", "telikos-uat"),
    ("PP", "telikos-preprod"), ("telikos_sit", "telikos-sit"), ("other-ns", "other-ns"),
])
def test_an_environment_name_or_a_misspelling_finds_the_real_namespace(given, real, monkeypatch):
    from app import grafana
    monkeypatch.setenv("ASTA_GRAFANA_NAMESPACE", "telikos-prod")
    assert grafana.resolve_namespace(given) == real


def test_an_id_is_traced_in_every_environment_and_prod_narrows_itself(monkeypatch):
    from app import grafana
    monkeypatch.setenv("ASTA_GRAFANA_NAMESPACE", "telikos-prod")
    monkeypatch.setenv("ASTA_GRAFANA_ENVS", "prod,preprod,uat")
    asked = []

    async def logs(service="", terms=None, minutes=0, ns="", errors_only=True, **k):
        asked.append((ns, minutes))
        if ns == "telikos-prod" and minutes > 1080:
            raise grafana.GrafanaError("400: the query would read too many bytes")
        return {"namespace": ns, "scanned": 141 if ns == "telikos-preprod" else 0,
                "signatures": [], "minutes": minutes}

    monkeypatch.setattr(grafana, "logs", logs)
    got = asyncio.run(grafana.logs_everywhere(terms=["H65ZMWX52B2"], minutes=4320))
    assert {g["env"] for g in got} == {"prod", "preprod", "uat"}
    assert next(g for g in got if g["env"] == "preprod")["scanned"] == 141
    assert not any(g.get("error") for g in got), "prod read a shorter window instead of failing"


def test_the_investigation_is_told_how_to_check():
    from app import responder
    pb = responder.playbook("could you pls check once? H65ZMWX52B2 isRFP failed")
    assert 'namespace="all"' in pb and "H65ZMWX52B2" in pb
    run = responder.playbook("build failing https://github.com/o/r/actions/runs/36547977396/")
    assert "--log-failed" in run
    assert responder.playbook("thanks bro") == ""


# --- P5 -------------------------------------------------------------------------------

def test_someone_wanting_him_gets_a_reply_he_can_send_with_one_word(rail):
    from app import loop
    tell = "Yogesh wants to call you about the defect closure. He's asking if you're ready now."
    rail.rows["Yogesh Kumar Ravichandran"] = [_m("Yogesh Kumar Ravichandran", "Arunkumar K Call ?")]
    rail.script["teams:Yogesh Kumar Ravichandran"] = {
        **_decide("ask", "call about the defect", work="talk"), "tell": tell,
        "reply": "Sure, give me a sec"}
    rail.sweep()
    text = rail.pushed[0]["text"]
    assert text.startswith(tell) and "> Sure, give me a sec" in text
    staged = loop.awaiting(store.kv_get("wa_conversation"))
    assert staged["to"] == "Yogesh Kumar Ravichandran" and staged["what"] == "Sure, give me a sec"
    assert rail.sent == [], "nothing goes to Yogesh before his yes"
