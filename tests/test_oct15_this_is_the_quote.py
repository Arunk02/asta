"""15 Oct: Vinish replied "Good Morning! Please check this" to Arun's own message
of screenshots ("bro verified manually … ran multiple tests with real payloads").
"check this" read as a booking still to come: Asta held the chat waiting for it,
then drafted "which booking is it, and in which environment?". The "this" was
the message he quoted.
"""

import pytest

from app import booking_case as b

HEADER = "Arunkumar K\n14/10/2026 20:34\n"
VINISH = (HEADER + "📷 📷 📷 bro verified manually ask claude so ran multiple tests with real payloads\n"
          "Good Morning!\nPlease check this")


def test_his_quoted_screenshots_are_the_this():
    assert b.waiting_for_more(VINISH) == ""
    assert b.missing(VINISH) == []
    assert b.ask_line(VINISH) == ""


def test_the_already_read_form_too():
    read = ("Please check this (replying to an earlier message)\n  ↳ quoting: Arunkumar K "
            "14/10/2026 20:34 📷 📷 📷 bro verified manually")
    assert b.waiting_for_more(read) == "" and b.missing(read) == []


def test_a_quote_about_a_booking_still_gets_what_it_lacks():
    q = HEADER + "booking stuck in preprod, can you look\nPlease check this"
    assert b.missing(q) == ["booking"]


def test_a_quote_with_the_booking_and_env_lacks_nothing():
    q = HEADER + "MH65W8JZNVNT is in progress in preprod\nPlease check this"
    assert b.missing(q) == []


@pytest.mark.parametrize("text", ["please check this booking:", "check this"])
def test_without_a_quote_nothing_changes(text):
    assert b.waiting_for_more(text) == "names a booking it does not include"
