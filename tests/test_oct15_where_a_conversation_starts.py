"""15 Oct: "it doesn't know where the convo ends and the next starts".

One 1:1 with Vinish on 14 Oct held eight topics — PR 1252, PR 1483, Komal's PR,
a CT failure, the LD issue, SendGrid keys, the config map, a SIT test. Each new
one "reopened" the last conversation and carried its need: an opener at 13:19
still read "share the update on PR 1252 and review PR 1483", and the reply mixed
the two PRs up. A conversation ends on its subject, not on a clock.
"""

import time

from app import chat_watch, store, threads

PR_1252 = "github.com/acme/booking/pull/1252"
PR_1473 = "github.com/acme/booking/pull/1473"


def _closed_about_1252(now):
    t = threads.open("teams", "Vinish Kumar", now=now - 4000)
    threads.update(t["id"], need="Share the update on PR 1252",
                   summary="Vinish asked for PR 1252", entities=[PR_1252])
    threads.close(t["id"], why="he replied", now=now - 3600)
    return t["id"]


def _episodes():
    with store._connect() as conn:
        return [dict(r) for r in conn.execute("SELECT need FROM conv_episodes")]


def test_a_new_subject_after_a_close_is_a_new_conversation():
    now = time.time()
    _closed_about_1252(now)
    text = f"Vinish Kumar: bro can you review Komal's PR https://{PR_1473}"
    t = threads.open("teams", "Vinish Kumar", now=now, text=text,
                     entities=threads.entities_in(text))
    assert t["status"] == "open" and t["need"] == "" and t["entities"] == []
    assert threads.is_new(t, now)                       # its past is looked up afresh
    assert _episodes() == [{"need": "Share the update on PR 1252"}]


def test_the_same_pr_picks_it_back_up():
    now = time.time()
    _closed_about_1252(now)
    text = f"Vinish Kumar: build failed on https://{PR_1252}"
    t = threads.open("teams", "Vinish Kumar", now=now, text=text,
                     entities=threads.entities_in(text))
    assert t["need"] == "Share the update on PR 1252"


def test_one_more_thing_picks_it_back_up():
    now = time.time()
    _closed_about_1252(now)
    t = threads.open("teams", "Vinish Kumar", now=now, text="Vinish Kumar: bro one more thing")
    assert t["need"] == "Share the update on PR 1252"


def test_a_bare_hello_after_a_close_does_not_carry_the_old_need():
    now = time.time()
    _closed_about_1252(now)
    t = threads.open("teams", "Vinish Kumar", now=now, text="Vinish Kumar: Hi bro")
    assert t["need"] == ""


def test_without_the_message_it_reopens_as_before():
    now = time.time()
    _closed_about_1252(now)
    assert threads.open("teams", "Vinish Kumar", now=now)["need"] == "Share the update on PR 1252"


def test_the_transcript_marks_where_this_conversation_starts(monkeypatch):
    now = time.time()
    tid = _closed_about_1252(now)
    rows = [{"sender": "Vinish Kumar", "text": "PR 1252 update?", "sent_at": now - 3900},
            {"sender": "Arunkumar K", "text": "merged bro", "sent_at": now - 3700},
            {"sender": "Arunkumar K", "text": "Vinish once u free call me bro", "sent_at": now - 60},
            {"sender": "Vinish Kumar", "text": "In call with TMS bro", "sent_at": now - 5}]
    monkeypatch.setattr(store, "teams_messages", lambda **k: rows)
    t = threads.open("teams", "Vinish Kumar", now=now, text="Vinish Kumar: In call with TMS bro")
    lines = chat_watch._transcript("Vinish Kumar", now, begins=threads.started_at(t))
    cut = lines.index(chat_watch.CONVERSATION_STARTS)
    assert "PR 1252" in " ".join(lines[:cut])
    # His own message after the close is part of the new conversation.
    assert "call me bro" in " ".join(lines[cut:]) and "TMS" in lines[-1]
    assert tid == t["id"]


def test_after_it_dissolves_the_next_one_starts_where_it_ended():
    now = time.time()
    tid = _closed_about_1252(now - 3 * 3600)
    threads.dissolve_due(now=now)
    t = threads.open("teams", "Vinish Kumar", now=now, text="Vinish Kumar: Good Morning!")
    assert abs(threads.started_at(t) - (now - 3 * 3600 - 3600)) < 1
    assert t["id"] == tid


def test_no_known_end_marks_nothing():
    t = threads.open("teams", "Someone New", now=time.time(), text="Someone New: hi")
    assert threads.started_at(t) == 0.0


def test_a_quick_back_and_forth_stays_one_conversation():
    """15 Oct, Sameer: office, PG, "okay cool", "any weekend plans?" — inside 20
    minutes, split into four conversations, each read cold."""
    now = time.time()
    t = threads.open("teams", "Sameer", now=now - 900)
    threads.update(t["id"], need="", summary="Sameer asked how many are coming to office")
    threads.close(t["id"], why="closing", now=now - 600)
    t = threads.open("teams", "Sameer", now=now, text="Sameer: Any weekend plans?")
    assert t["summary"] == "Sameer asked how many are coming to office"
