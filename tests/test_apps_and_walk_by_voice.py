"""Opening apps, and reviewing a change in IntelliJ — by voice as well as chat.

Arun, 3 Oct: "check open apps and run, make it doable via voice as well, i still
see some issue with apps opening on command … and work walkthrough in intellij
code walkthrough make it voice so it's easy to review". What was wrong:

  * "open VS Code" found nothing beside an installed Visual Studio Code; "open
    excel" (not installed) took a slow brain turn and ended vague;
  * every open said "I can't confirm it is running" — System Events was never
    allowed on this Mac;
  * by voice, the wrapper around his words hid every instant command, so
    "open intellij" was "On it." and a 20-60 s job;
  * a voice walkthrough lost its place on the next sentence: every voice job
    was a new conversation.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from app import apps, store, voice_mode as vm
from test_walkthrough import session  # noqa: F401 — the review fixture, reused
from test_voice_mode import helper  # noqa: F401 — the menu-bar recorder, reused


@pytest.fixture
def mac(monkeypatch):
    installed = [Path(f"/Applications/{n}.app") for n in
                 ("IntelliJ IDEA", "IntelliJ IDEA CE", "Google Chrome", "Visual Studio Code",
                  "Microsoft Word", "Numbers", "Offset Explorer 2", "MongoDB Compass")]
    installed.append(Path("/System/Library/CoreServices/Finder.app"))
    monkeypatch.setattr(apps, "installed_apps", lambda refresh=False: list(installed))
    monkeypatch.setattr(apps, "bundle_id", lambda app: f"test.{Path(app).stem.lower()}")
    monkeypatch.setattr(apps, "enabled", lambda: True)
    monkeypatch.setattr(apps, "START_WAIT", 0.3)
    state = {"ran": [], "running": set()}

    async def run(*argv):
        state["ran"].append([str(a) for a in argv])
        for a in argv[1:]:
            if str(a).endswith(".app"):
                state["running"].add(f"test.{Path(a).stem.lower()}")
        return 0, ""

    async def running():
        return set(state["running"])

    monkeypatch.setattr(apps, "_run", run)
    monkeypatch.setattr(apps, "running_bundles", running)
    return state


# --- which app ------------------------------------------------------------------------

@pytest.mark.parametrize("said, want", [
    ("vs code", "Visual Studio Code"), ("vscode", "Visual Studio Code"),
    ("intelligent", "IntelliJ IDEA"),            # how speech hears "IntelliJ"
    ("word", "Microsoft Word"), ("kafka", "Offset Explorer 2"), ("mongo", "MongoDB Compass"),
    ("finder", "Finder"), ("intellij app", "IntelliJ IDEA"),
])
def test_he_names_it_the_way_he_says_it(mac, said, want):
    app, _ = apps.find_app(said)
    assert app is not None and app.stem == want


@pytest.mark.parametrize("said", ["open up chrome", "switch to intellij", "open the vs code app"])
def test_more_ways_of_saying_open(mac, said):
    assert apps.open_ask(said) is not None


def test_an_app_he_names_that_is_not_installed_is_said_at_once_and_nothing_else_opens(mac):
    """Never Numbers in place of Excel: he asked for Excel."""
    assert apps.open_ask("open excel") == ("missing", "Microsoft Excel", "")
    line = asyncio.run(apps.open_it(*apps.open_ask("open excel")))
    assert "isn't installed" in line and "open numbers" in line
    assert mac["ran"] == [], "nothing was opened in its place"
    assert apps.open_ask("open photoshop") is None, "an unknown name still goes to a brain"


# --- did it open: the process list, no permission needed ----------------------------

def test_running_is_read_from_the_process_list_first(monkeypatch):
    monkeypatch.setattr(apps, "bundle_id", lambda app: f"id.{Path(app).stem.lower()}")
    monkeypatch.setattr(apps, "_BUNDLE_OF", {})

    async def run(*argv):
        assert argv[0] == "/bin/ps"
        return 0, ("/Applications/IntelliJ IDEA.app/Contents/MacOS/idea\n"
                   "/usr/sbin/cfprefsd\n"
                   "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome\n")

    async def no_osascript(script, args):
        raise AssertionError("System Events is not needed when ps answers")

    monkeypatch.setattr(apps, "_run", run)
    monkeypatch.setattr(apps, "_osascript", no_osascript)
    assert asyncio.run(apps.running_bundles()) == {"id.intellij idea", "id.google chrome"}


def test_system_events_is_the_fallback_when_ps_cannot_be_read(monkeypatch):
    async def run(*argv):
        return 1, ""

    async def osascript(script, args):
        return "com.apple.safari, com.google.chrome"

    monkeypatch.setattr(apps, "_run", run)
    monkeypatch.setattr(apps, "_osascript", osascript)
    assert "com.google.chrome" in asyncio.run(apps.running_bundles())


# --- the voice wrapper no longer hides the command --------------------------------

def test_his_words_are_read_without_the_voice_wrapper():
    wrapped = vm.with_context("open intellij", ["Arun: hello", "Asta: hi"])
    assert wrapped != "open intellij" and vm.his_words(wrapped) == "open intellij"
    assert vm.his_words("open intellij") == "open intellij"


def test_a_voice_job_saying_open_takes_the_instant_door(mac):
    wrapped = vm.with_context("open intellij", ["Arun: hello"])
    assert apps.open_ask(vm.his_words(wrapped)) == ("app", "intellij", "")


def test_by_voice_open_is_done_at_once_and_said(helper, mac, monkeypatch):
    vm._STATE.update(speaker=True)
    out = asyncio.run(vm.converse("Asta, open IntelliJ"))
    assert out["did"] == "opened"
    assert "Opening IntelliJ IDEA." in helper.said()
    assert any("IntelliJ IDEA.app" in " ".join(r) for r in mac["ran"])


def test_by_voice_a_missing_app_is_said_plainly(helper, mac):
    vm._STATE.update(speaker=True)
    asyncio.run(vm.converse("open excel"))
    assert any("isn't installed" in s for s in helper.said())
    assert mac["ran"] == []


def test_app_names_are_in_the_speech_vocabulary(mac):
    assert "IntelliJ IDEA" in vm.vocabulary() and "Visual Studio Code" in vm.vocabulary()


# --- the walkthrough, by voice -----------------------------------------------------

def test_a_walkthrough_starts_from_a_spoken_pr_number_or_the_last_change(session):
    from app import walkthrough
    assert walkthrough.wants_to_start("walk me through PR 1432") == f"task {session.tid}"
    assert walkthrough.wants_to_start("walk me through booking PR number 1432") == f"task {session.tid}"
    assert walkthrough.wants_to_start("walk me through my last change") == f"task {session.tid}"


def _start_by_voice(session):
    from app import walkthrough
    asyncio.run(walkthrough.start(vm.VOICE_WALK, f"task {session.tid}"))


def test_next_by_voice_moves_the_same_session_and_says_the_step(helper, session):
    vm._STATE.update(speaker=True)
    _start_by_voice(session)
    out = asyncio.run(vm.converse("next"))
    assert out["did"] == "walkthrough"
    said = " ".join(helper.said())
    assert "Step 2 of 3: Service, in PriorityService, line 10. Saves then publishes." in said
    assert session.opened[-1] == ("src/main/java/x/PriorityService.java", 10), "IntelliJ followed"
    asyncio.run(vm.converse("okay next"))
    assert "Step 3 of 3" in " ".join(helper.said())


def test_a_question_mid_walkthrough_is_answered_about_this_step(helper, session, monkeypatch):
    from app import voice_talker
    vm._STATE.update(speaker=True)
    _start_by_voice(session)

    async def route(text, context=""):
        return voice_talker.ANSWER
    monkeypatch.setattr(voice_talker, "route", route)
    out = asyncio.run(vm.converse("why is the id null checked here?"))
    assert out["did"] == "walkthrough"
    assert any("id can be absent" in s for s in helper.said())


def test_a_note_mid_walkthrough_is_kept_not_a_job(helper, session, monkeypatch):
    from app import voice_talker, walkthrough
    vm._STATE.update(speaker=True)
    _start_by_voice(session)

    async def route(text, context=""):
        return voice_talker.DO
    monkeypatch.setattr(voice_talker, "route", route)
    started = []
    monkeypatch.setattr(vm, "_work", lambda *a, **k: started.append(a))
    asyncio.run(vm.converse("this should use the enum instead of a string"))
    assert not started, "a note, not a job"
    assert walkthrough.get(vm.VOICE_WALK)["notes"][0]["note"].startswith("this should use the enum")


def test_room_talk_mid_walkthrough_stays_quiet(helper, session, monkeypatch):
    from app import voice_talker, walkthrough
    vm._STATE.update(speaker=True)
    _start_by_voice(session)

    async def route(text, context=""):
        return voice_talker.QUIET
    monkeypatch.setattr(voice_talker, "route", route)
    before = len(helper.said())
    asyncio.run(vm.converse("I'll send you the deck after lunch, Vinish"))
    assert len(helper.said()) == before
    assert not walkthrough.get(vm.VOICE_WALK)["notes"]


def test_done_by_voice_says_the_notes_and_keeps_them_in_writing(helper, session, monkeypatch):
    sent = []

    async def to_chat(text):
        sent.append(text)
    monkeypatch.setattr(vm, "to_chat", to_chat)
    vm._STATE.update(speaker=True)
    _start_by_voice(session)
    from app import walkthrough
    asyncio.run(walkthrough.handle(vm.VOICE_WALK, "this should use the enum"))

    async def go():
        await vm.converse("done")
        await asyncio.sleep(0)
    asyncio.run(go())
    assert sent and "enum" in sent[0]


def test_a_walkthrough_with_no_target_asks_which(helper, mac):
    vm._STATE.update(speaker=True)
    out = asyncio.run(vm.converse("walk me through the change"))
    assert out["did"] == "walkthrough_which"
    assert "task number, or a PR number" in " ".join(helper.said())


def test_a_long_step_is_said_whole_and_the_prompts_are_not_read_out(helper):
    vm._STATE.update(speaker=True)
    long = ("Step 1 of 7: Hook, in Impl, line 73. " + "This sentence is long enough to matter. " * 20
            + "Last words here.\nnext · back · ask anything · done")
    asyncio.run(vm._say_all(long, at_most=30))
    said = helper.said()
    assert said[-1] == "Last words here.", "nothing cut mid-word at a length cap"
    assert not any("·" in s or "next ·" in s for s in said)


def test_done_by_voice_is_said_as_a_person_would(helper, session):
    vm._STATE.update(speaker=True)
    _start_by_voice(session)
    from app import walkthrough
    asyncio.run(walkthrough.handle(vm.VOICE_WALK, "this should use the enum"))
    asyncio.run(vm.converse("done"))
    said = " ".join(helper.said())
    assert "You made 1 note; they're in your chat." in said
    assert f"Say apply and task {session.tid} makes the changes" in said
    assert "note(s)" not in said


def test_the_opening_reads_step_one_even_if_he_already_said_next(session):
    from app import walkthrough
    _start_by_voice(session)
    asyncio.run(walkthrough.handle(vm.VOICE_WALK, "next"))
    assert walkthrough.spoken(vm.VOICE_WALK, 0).startswith("Step 1 of 3")
    assert walkthrough.spoken(vm.VOICE_WALK).startswith("Step 2 of 3")


# --- the PR's own code, the next file, and its tests --------------------------------

def _git(cwd, *args):
    import subprocess
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True,
                          text=True).stdout.strip()


def test_the_walkthrough_opens_the_prs_own_code_and_never_moves_his_checkout(tmp_path):
    """3 Oct: "it is opened some other branch, not the respective one". His
    shared clone was on another feature branch; every jump was the wrong code."""
    from app import walkthrough
    origin = tmp_path / "origin.git"
    _git(tmp_path, "init", "--quiet", "--bare", str(origin))
    work = tmp_path / "seed"
    _git(tmp_path, "init", "--quiet", "-b", "develop", str(work))
    _git(work, "-c", "user.email=a@b", "-c", "user.name=a", "commit", "--quiet",
         "--allow-empty", "-m", "base")
    _git(work, "remote", "add", "origin", str(origin))
    _git(work, "push", "--quiet", "origin", "develop")
    (work / "Change.java").write_text("class Change {}\n")
    _git(work, "add", ".")
    _git(work, "-c", "user.email=a@b", "-c", "user.name=a", "commit", "--quiet", "-m", "pr")
    pr_head = _git(work, "rev-parse", "HEAD")
    _git(work, "push", "--quiet", "origin", "HEAD:refs/pull/7/head")
    ws = tmp_path / "ws"
    ws.mkdir()
    _git(ws, "clone", "--quiet", str(origin), "svc")
    _git(ws / "svc", "checkout", "--quiet", "-b", "feature/something-else")

    tree = asyncio.run(walkthrough.review_tree(ws, ws / "svc", "https://github.com/o/svc/pull/7"))
    assert tree and Path(tree).name == "review-svc-7"
    assert _git(tree, "rev-parse", "HEAD") == pr_head, "the PR's head commit"
    assert (Path(tree) / "Change.java").exists()
    assert _git(ws / "svc", "branch", "--show-current") == "feature/something-else", \
        "his checkout's branch is untouched"
    again = asyncio.run(walkthrough.review_tree(ws, ws / "svc", "https://github.com/o/svc/pull/7"))
    assert again == tree, "reused, not a second copy"


def test_a_pr_that_cannot_be_fetched_opens_nothing_rather_than_the_wrong_code(tmp_path):
    from app import walkthrough
    (tmp_path / "svc").mkdir()
    assert asyncio.run(walkthrough.review_tree(tmp_path, tmp_path / "svc",
                                               "https://github.com/o/svc/pull/7")) == ""


def test_next_file_skips_to_the_next_class_and_previous_file_comes_back(session):
    from app import walkthrough
    session.start()
    s = walkthrough.get(session.cid)
    s["steps"].insert(1, {**s["steps"][0], "line": 50, "title": "Same file again"})
    walkthrough._save(session.cid, s)
    out = session.say("next file")
    assert "PriorityService.java:10" in out
    out = session.say("previous file")
    assert "1/4" in out and "BookingController.java:42" in out
    session.say("go to next file")
    session.say("next file")
    assert "last file" in session.say("next file")


def test_show_the_unit_tests_for_this_step(session, tmp_path):
    from app import walkthrough
    session.start()
    s = walkthrough.get(session.cid)
    root = tmp_path / "telikos-booking-service"
    test = root / "src/test/java/x/BookingControllerTest.java"
    test.parent.mkdir(parents=True, exist_ok=True)
    test.write_text("class BookingControllerTest {\n  @Test\n  void setsPriority() {}\n"
                    "  @Test\n  void rejectsUnknownBooking() {}\n}\n")
    s["root"] = str(root)
    s["tests"] = {"src/test/java/x/BookingControllerTest.java": ["setsPriority"]}
    walkthrough._save(session.cid, s)
    assert walkthrough.is_command("show unit test cases for this")
    out = session.say("show unit test cases for this")
    assert "Opened BookingControllerTest — 2 tests" in out and "1 added in this change: setsPriority" in out
    assert session.opened[-1] == ("src/test/java/x/BookingControllerTest.java", 2)
    session.say("next")
    assert "No unit tests for PriorityService" in session.say("where are the tests?")


def test_tests_added_by_a_change_are_read_from_its_diff():
    from app import walkthrough
    diff = ("diff --git a/src/test/java/x/AT.java b/src/test/java/x/AT.java\n"
            "+++ b/src/test/java/x/AT.java\n+  @Test\n+  void addsAta() {\n+  }\n"
            "diff --git a/src/main/java/x/A.java b/src/main/java/x/A.java\n+++ b/src/main/java/x/A.java\n"
            "+  void notATest() {}\n")
    assert walkthrough._tests_in_diff(diff) == {"src/test/java/x/AT.java": ["addsAta"]}


def test_by_voice_next_file_and_tests_are_instant_commands(helper, session):
    vm._STATE.update(speaker=True)
    _start_by_voice(session)
    out = asyncio.run(vm.converse("next file"))
    assert out["did"] == "walkthrough"
    assert "Step 2 of 3" in " ".join(helper.said())


def test_no_filler_while_asta_is_still_deciding_if_it_was_meant_for_her(helper, monkeypatch):
    """Voice bench, 3 Oct: "one sec, I'm on a call" → "One moment." The second
    look may still stay quiet; the filler waits until it is known to be for Asta."""
    from app import voice_talker
    monkeypatch.setattr(vm, "_talker_selected", lambda: True)
    vm._STATE.update(speaker=True)
    monkeypatch.setattr(vm, "FILLER_SECONDS", 0.05)
    monkeypatch.setattr(vm, "in_conversation", lambda *a, **k: True)

    async def route(text, context=""):
        return voice_talker.QUIET

    async def sentences(text, kind, **k):
        await asyncio.sleep(0.3)
        yield voice_talker.QUIET
    monkeypatch.setattr(voice_talker, "route", route)
    monkeypatch.setattr(voice_talker, "sentences", sentences)
    out = asyncio.run(vm.converse("one sec, I'm on a call with Vinish now"))
    assert out["did"] == "not_for_asta"
    assert "One moment." not in helper.said()
