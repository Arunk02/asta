"""13 Oct, Komal: "Hi Arunkumar, call ?" -> Asta "Sure bro, what's up?" -> she is
not someone he calls bro. Then her answer, "it is for avro update" + the repo,
reached him as "Komal replied to you (to your: 'Sure bro, what's up?')" and she
got "Is there anything else you need from my side?".

One cause under all of it: lines Asta sends on its own go out under his name, and
were read back as his words — his voice, the terms he uses with her, the message
she was "replying to him" about. And the guard that strips "bro" never saw them.
"""

import asyncio
import time

from app import chat_watch, store, style, writing
from app import teams_bridge as tb

KOMAL = "Komal Jayswal"


def _row(key, sender, text, at, chat=KOMAL):
    return {"key": key, "chat": chat, "sender": sender, "text": text, "sent_at": at, "stamp": ""}


def _komal_morning(now):
    store.save_teams_messages([
        _row("k1", KOMAL, "Hi Arunkumar, call ?", now - 600),
        _row("k2", "Arunkumar K", "Sure bro, what's up?", now - 560),
        _row("k3", KOMAL, "it is for avro update", now - 300),
        _row("k4", KOMAL, "https://github.com/Maersk-Global/empv3-tenant-intake", now - 290),
    ])
    store.record_automatic_teams_message("k2", KOMAL)


# --- Asta's lines are not his words -------------------------------------------

def test_stored_rows_say_which_lines_asta_sent():
    now = time.time()
    _komal_morning(now)
    rows = {r["key"]: r for r in store.teams_messages(KOMAL)}
    assert rows["k2"]["automatic"] is True
    assert rows["k1"]["automatic"] is False


def test_an_asta_line_is_from_his_account_but_not_written_by_him():
    now = time.time()
    _komal_morning(now)
    asta = next(r for r in store.teams_messages(KOMAL) if r["key"] == "k2")
    assert chat_watch.is_from_him(asta["sender"])
    assert not chat_watch.written_by_him(asta, KOMAL)
    assert chat_watch.speaker(asta, KOMAL) == "Asta (as Arun)"


def test_a_line_asta_noted_is_not_his_even_without_a_verified_receipt():
    chat_watch.note_asta_said(KOMAL, "Yes, tell me")
    row = _row("x", "Arunkumar K", "Yes, tell me", time.time())
    assert not chat_watch.written_by_him(row, KOMAL)
    assert chat_watch.written_by_him(_row("y", "Arunkumar K", "thats my agent", time.time()), KOMAL)


def test_asta_saying_bro_does_not_make_bro_his_term_for_her():
    now = time.time()
    _komal_morning(now)
    assert writing.address_terms(KOMAL) == []


def test_his_own_bro_still_counts():
    now = time.time()
    store.save_teams_messages([_row("v1", "Arunkumar K", "ok bro will check", now - 60,
                                    chat="Vinish Kumar")])
    assert writing.address_terms("Vinish Kumar") == ["bro"]


# --- her answer to Asta's question is the ask -----------------------------------

def test_her_answer_to_asta_is_not_a_reply_to_him():
    now = time.time()
    _komal_morning(now)
    assert not chat_watch.answering_him(KOMAL, now - 300)
    assert chat_watch.answering_asta(KOMAL, now - 300) == "Sure bro, what's up?"
    assert chat_watch.his_last_words(KOMAL, before=now - 300) == ""


def test_a_reply_to_his_own_message_is_still_his_conversation():
    now = time.time()
    store.save_teams_messages([_row("s1", "Arunkumar K", "checked PR 94", now - 900),
                               _row("s2", KOMAL, "ok thanks", now - 300)])
    assert chat_watch.answering_him(KOMAL, now - 300)
    assert chat_watch.answering_asta(KOMAL, now - 300) == ""


def test_the_brain_reads_asta_lines_as_asta():
    now = time.time()
    _komal_morning(now)
    lines = chat_watch._transcript(KOMAL, now)
    assert any(line.startswith("Asta (as Arun): Sure bro") for line in lines)
    assert not any(line.startswith("Arun: Sure bro") for line in lines)


# --- nothing Asta says on its own carries a term he never used --------------------

def test_the_line_komal_got_is_fitted():
    assert writing.fit_address("Sure bro, what's up?", KOMAL) == "Sure, what's up?"
    assert writing.fit_address("sure bro — is this about X?", KOMAL) == "sure — is this about X?"
    assert writing.fit_address("Yes bro tell me", KOMAL) == "Yes tell me"
    assert writing.fit_address("no bro, the payload doesn't have it", KOMAL) == \
        "no, the payload doesn't have it"


def test_say_fits_the_line_before_it_goes(monkeypatch):
    sent: list[str] = []

    async def send(chat, text, allow_group=False):
        sent.append(text)
        return chat

    monkeypatch.setattr(tb, "enabled", lambda: False)
    monkeypatch.setattr(tb, "send_message", send)
    assert asyncio.run(chat_watch._say(KOMAL, "Sure bro, what's up?")) is True
    assert sent == ["Sure, what's up?"]


def test_any_automatic_send_is_fitted(monkeypatch):
    sent: list[str] = []

    async def send(chat, text, allow_group=False):
        sent.append(text)
        return chat

    monkeypatch.setattr(tb, "send_message", send)
    asyncio.run(tb.send_automatic(KOMAL, "Let me look into this, bro"))
    assert sent == ["Let me look into this"]


def test_examples_borrowed_from_other_chats_lose_their_bro():
    now = time.time()
    store.save_teams_messages([
        _row(f"v{i}", "Arunkumar K", f"ok bro will check {i}", now - 100 + i, chat="Vinish Kumar")
        for i in range(5)])
    got = style.examples(KOMAL, 3)
    assert got and not any("bro" in g.lower() for g in got)


def test_the_reader_is_not_taught_bro():
    from app import understand
    assert '"Sure bro' not in open(understand.__file__).read()


def test_the_evening_audit_catches_a_term_he_never_used():
    from app import selfaudit
    now = time.time()
    _komal_morning(now)
    store.record_outcome("thread", "said", subject=KOMAL, detail="Sure bro, what's up?")
    store.record_outcome("thread", "said", subject="Vinish Kumar", detail="ok bro, checking")
    store.save_teams_messages([_row("v9", "Arunkumar K", "bro check once", now - 30, chat="Vinish Kumar")])
    found = {f["kind"]: f for f in selfaudit.findings(1)}
    assert found["address"]["count"] == 1 and KOMAL in found["address"]["examples"][0]


# --- his conversation is his ------------------------------------------------------------

SHABDA = "Shabda Anubhav Dev"


def _his_ask_and_her_reply(now, his_at=-60):
    store.save_teams_messages([
        _row("s1", "Arunkumar K", "Shabda can you merge this please", now + his_at, chat=SHABDA),
        _row("s2", SHABDA, "need one more aopprval", now - 10, chat=SHABDA)])


def test_her_reply_to_his_ask_is_not_answered_by_asta(monkeypatch):
    """13 Oct: he asked Shabda to merge event-router PR 96; her "need one more
    approval" got "Who else needs to approve it?" from Asta."""
    from app import answers
    now = time.time()
    _his_ask_and_her_reply(now)
    sent, shown = [], []

    async def send(chat, text, allow_group=False):
        sent.append(text)
        return chat

    async def present(**kw):
        shown.append(kw)
        return True

    monkeypatch.setattr(tb, "enabled", lambda: False)
    monkeypatch.setattr(tb, "send_message", send)
    monkeypatch.setattr(answers, "present", present)
    assert asyncio.run(chat_watch._say(SHABDA, "Who else needs to approve it?", since=now - 10)) is False
    assert sent == []
    assert shown and shown[0]["reply"] == "Who else needs to approve it?"


def test_he_wrote_recently_so_asta_stays_out():
    now = time.time()
    store.save_teams_messages([_row("r1", "Arunkumar K", "ok will check", now - 20 * 60, chat=SHABDA),
                               _row("r2", SHABDA, "hi", now - 5 * 60, chat=SHABDA),
                               _row("r3", SHABDA, "one more thing", now - 60, chat=SHABDA)])
    assert chat_watch.in_his_exchange(SHABDA, now - 60)


def test_an_old_conversation_of_his_does_not_silence_asta():
    now = time.time()
    store.save_teams_messages([_row("o1", "Arunkumar K", "thanks", now - 5 * 3600, chat=SHABDA),
                               _row("o2", SHABDA, "hi Arunkumar", now - 4 * 3600, chat=SHABDA),
                               _row("o3", SHABDA, "call?", now - 30, chat=SHABDA)])
    assert not chat_watch.in_his_exchange(SHABDA, now - 30)


def test_asta_own_line_does_not_make_it_his_exchange():
    now = time.time()
    store.save_teams_messages([_row("a1", "Arunkumar K", "Yes, tell me", now - 120, chat=SHABDA),
                               _row("a2", SHABDA, "need help on PR", now - 30, chat=SHABDA)])
    store.record_automatic_teams_message("a1", SHABDA)
    assert not chat_watch.in_his_exchange(SHABDA, now - 30)
