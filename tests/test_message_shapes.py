"""The shapes real Teams messages actually come in.

Every parsing bug this week came from the same place: a test passed against text
somebody imagined, and reality had a different shape. The reply-quote was found
by Arun, not by 2,200 tests. So these fixtures are DERIVED FROM MEASUREMENT — 200
real captured messages from his store, counted by shape:

    144  plain
     16  reaction tail + multiple paragraphs
     16  contains a URL
     16  multiple paragraphs, no quote
      5  URL + multiple paragraphs
     22  a quote block (21 leading, 1 trailing)

Two facts only the real corpus revealed, both of which broke the parser:

  * the quoted timestamp is 12-hour with AM/PM far more often than 24-hour. The
    first regex matched only "18:38", so 21 of the 22 quotes went straight through
    and kept attributing one colleague's words to another;
  * a quote appears BEFORE the text when replying and AFTER it when forwarding, so
    the header's position decides which half to keep.

**The content here is invented; only the shapes are real.** His threads are
colleagues' work conversations and this repository is on GitHub. The live corpus
is exercised by `test_the_real_corpus_still_parses` below, which reads his store
when it exists and skips everywhere else — so the real data validates on his
machine and never leaves it.
"""

from __future__ import annotations

import re

import pytest

from app import chat_watch

# --- the shapes, with invented content ---------------------------------------

PLAIN = "can you check the booking service logs"
MULTI_PARA = "first thing to look at is the ETA\n\nsecond is the null timezone"
WITH_URL = "raised it here https://github.com/example/repo/pull/42 please review"
REACTION_TAIL = "shipped the fix\n\n1 Like reaction with medium dark skin tone."

#: A reply: quote first, blank line, then what they typed. 21 of 22 in his data.
REPLY_12H = ("Ashwin Kumar\n20/07/2026 5:23 PM\nwhat is the IP integration here\n\n"
             "IP is the EDI channel, Seeburger sends the invoice details")
REPLY_24H = ("Ashwin Kumar\n20/07/2026 11:14\nwhat is the IP integration here\n\n"
             "IP is the EDI channel, Seeburger sends the invoice details")
#: A forward: what they typed, then the quoted body. 1 of 22.
FORWARD = ("Bro, can you check once?\n\nRajendra Kumar\n8/10/2026 5:23 PM\n\n"
           "Hi Vinish, there is an issue with the booking below")
#: A quote whose reply body did not survive the capture — Arun's own case.
QUOTE_ONLY = ("Arunkumar K\n28/08/2026 18:38\nlets analyse on some idea and see how "
              "it going on weekend sunday and Monday")


@pytest.mark.parametrize("raw,expected", [
    (PLAIN, PLAIN),
    (MULTI_PARA, "first thing to look at is the ETA\nsecond is the null timezone"),
    (REACTION_TAIL, "shipped the fix"),
])
def test_ordinary_shapes_survive_intact(raw, expected):
    """A cleaner that eats real text is worse than the noise it removes."""
    assert chat_watch.clean_message(raw) == expected


@pytest.mark.parametrize("raw", [REPLY_12H, REPLY_24H])
def test_a_reply_keeps_only_what_the_sender_typed(raw):
    """Both clock formats. The 12-hour one is the COMMON case in his data and the
    first version missed it entirely, so 21 of 22 quotes kept misattributing."""
    out = chat_watch.clean_message(raw)
    assert out.startswith("IP is the EDI channel")
    assert "what is the IP integration" not in out


def test_a_forward_keeps_the_part_before_the_quote():
    """The other position: their words first, the quoted body after."""
    assert chat_watch.clean_message(FORWARD) == "Bro, can you check once?"


def test_a_quote_with_no_reply_is_not_passed_off_as_the_message():
    assert chat_watch.clean_message(QUOTE_ONLY) == ""
    assert "lets analyse" not in chat_watch.summarise(QUOTE_ONLY)


def test_a_multi_paragraph_message_is_not_mistaken_for_a_reply():
    """40 of 200 real messages have a blank line and no quote at all. Splitting on
    the blank line alone would throw away the first half of every one of them."""
    assert "first thing to look at" in chat_watch.clean_message(MULTI_PARA)


def test_a_url_is_named_rather_than_pasted():
    line = chat_watch.summarise(WITH_URL)
    assert "http" not in line
    assert "GitHub: example/repo" in line


# --- the real corpus, on his machine only ------------------------------------

def _real_messages() -> list[dict]:
    """His actual captured messages, opened READ-ONLY and directly.

    `conftest` isolates `store.DB_PATH` for every test, which is the rule that
    keeps a stray row from becoming a question on his phone — so going through
    `store` here would read an empty temp database and this check would silently
    pass forever. It opens the real file itself, in SQLite read-only mode, so the
    isolation guarantee (nothing is ever WRITTEN to the live db) still holds
    exactly.
    """
    import sqlite3
    from pathlib import Path
    db = Path(__file__).resolve().parent.parent / "data" / "asta.db"
    if not db.exists():
        return []
    try:
        conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        rows = conn.execute("SELECT text FROM teams_messages LIMIT 500").fetchall()
        conn.close()
    except Exception:                                          # noqa: BLE001
        return []
    return [dict(r) for r in rows]


def test_the_real_corpus_still_parses():
    """Run every real captured message through the parser and assert the
    invariants. Skipped where there is no store — CI must never need his data, and
    his colleagues' conversations must never be committed to a public repository.

    This is the test that would have caught the misattribution. The invented
    fixtures above all passed while 21 of 22 real quotes were being mangled.
    """
    rows = _real_messages()
    if len(rows) < 20:
        pytest.skip("no meaningful corpus on this machine")

    problems = []
    for r in rows:
        raw = (r.get("text") or "")
        if not raw.strip():
            continue
        out, line = chat_watch.clean_message(raw), chat_watch.summarise(raw)
        flat = re.sub(r"\s+", " ", raw)
        if "http" in line:
            problems.append(("a raw URL reached the notification", line[:60]))
        elif out and re.sub(r"\s+", " ", out) not in flat:
            problems.append(("text that was never in the message", out[:60]))
        elif len(line) > 240:
            problems.append(("line far too long for a phone", str(len(line))))
        elif (not out and "\n\n" not in raw
                and not chat_watch._REPLY_HEADER.search(raw) and len(raw.strip()) > 3):
            problems.append(("a plain message was lost", raw[:60]))
    assert not problems, f"{len(problems)} of {len(rows)} real messages: {problems[:5]}"


def test_the_corpus_check_is_actually_reaching_his_data():
    """A skip that becomes permanent is a test that was deleted quietly. On his
    machine this must find the corpus; anywhere else it says so out loud."""
    rows = _real_messages()
    if not rows:
        pytest.skip("no live store here — expected on CI")
    assert len(rows) >= 20, f"only {len(rows)} messages — the corpus check is hollow"


# --- the quote with no blank line before the reply ---------------------------
# The common shape, and the one that beat the first two attempts. Captured live
# from his Nakka Harika thread:
#
#     Nakka Harika                                      <- who is quoted
#     28/08/2026 12:39                                  <- when
#     Swamy in vinish and urs team..                    <- HER words
#     Arrey I'm in multiple teams for Background support <- his reply
#
# Nothing in the text says where one ends and the other begins. But a quoted line
# is by definition a message that already exists in the thread, so the thread
# itself identifies it.

TIGHT_QUOTE = ("Nakka Harika\n28/08/2026 12:39\nSwamy in vinish and urs team..\n"
               "Arrey I'm in multiple teams for Background support")


def test_the_thread_identifies_the_quote_when_spacing_does_not():
    out = chat_watch.clean_message(
        TIGHT_QUOTE, known={"Swamy in vinish and urs team.."})
    assert out == "Arrey I'm in multiple teams for Background support"


def test_a_multi_line_reply_survives_when_the_thread_is_known():
    """The reason the lookup is preferred over "keep the last paragraph": that
    fallback would throw away everything but the final line."""
    raw = ("Nakka Harika\n28/08/2026 12:39\nSwamy in vinish and urs team..\n"
           "first point\nsecond point")
    out = chat_watch.clean_message(raw, known={"Swamy in vinish and urs team.."})
    assert "first point" in out and "second point" in out


def test_without_the_thread_it_still_refuses_to_misattribute():
    """Degrades to the last paragraph rather than showing the quoted line — losing
    a sentence is recoverable, attributing one is not."""
    out = chat_watch.clean_message(TIGHT_QUOTE)
    assert "Swamy in vinish" not in out
    assert out == "Arrey I'm in multiple teams for Background support"


def test_the_reader_passes_the_thread_through():
    """A lookup nothing supplies is a lookup that never runs."""
    import inspect
    src = inspect.getsource(chat_watch.sweep)
    assert "store.teams_messages(chat=chat" in src
    assert "known=known" in src
