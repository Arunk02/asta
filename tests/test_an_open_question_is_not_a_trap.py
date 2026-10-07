"""An open question must not swallow the next thing he tells Asta to do.

28 Sep. Asta put a question on his phone — "two open findings, reply 1, 2, or
both". He replied:

    send this feedback to swamy

and got back:

    ✅ Passed that back to whatever asked: "Two open findings from today…"

The instruction was filed as the answer to a multiple-choice question, nothing
was sent, and the confirmation quoted his own question back at him. The comment
above that branch in main.py says it plainly: *"An open ask_user question owns
the next message."* Unconditionally — nothing asked whether the message was an
answer at all.

The offers path three screens above already learned this. It reads accept,
decline, and *"Anything else: he moved on"*. `ask_user` never learned it.

The bias below is deliberate and one-directional, the same as `work_intent`:
divert ONLY when the message is clearly an instruction. A wrongly-diverted
answer leaves a question open that he can still answer; a wrongly-swallowed
instruction is silently not done, and he finds out from the person who never
heard from him.
"""

from __future__ import annotations

import pytest

from app import asking, store


def _open_question(text: str = "Two open findings — reply 1, 2, or both."):
    return store.create_question(text, source="claude-code")


# --- what is plainly an instruction, not an answer ----------------------------

@pytest.mark.parametrize("said", [
    "send this feedback to swamy",
    "Send this feedback to Swamy",
    "ping vinish about the build",
    "message harika that i'll be late",
    "forward that to the team",
    "call vinish now",
    "post it as a PR comment",
])
def test_an_instruction_is_not_filed_as_the_answer(said):
    _open_question()
    assert asking.pending_for_reply(said) is None, (
        f"{said!r} is something to DO — filing it as an answer means it never happens")


@pytest.mark.parametrize("said", [
    "Approve task 219",
    "approve task #220",
    "release/3.1.6 is still missing — implement it in the booking service",
    "why is the AP PR not updated yet?",
    "Komal asked for the AP change in telikos-activityplanworkflow-service; please fix it",
])
def test_other_work_and_explicit_task_commands_do_not_answer_a_question(said):
    _open_question("Which VTS approach should I use: 1 or 2?")
    assert asking.pending_for_reply(said) is None


# --- what really is an answer -------------------------------------------------

@pytest.mark.parametrize("said", [
    "1",
    "both",
    "option 2",
    "the second one",
    "yes go ahead",
    "2 first, then 1",
    "neither for now",
    "the second one, but check with vinish first",
])
def test_a_real_answer_still_answers(said):
    _open_question()
    assert asking.pending_for_reply(said) is not None, (
        f"{said!r} answers the question — diverting it would strand the caller "
        "that is blocked waiting")


# --- the guards that were already there must still hold -----------------------

def test_two_open_questions_still_refuse_to_guess():
    _open_question("first question?")
    _open_question("second question?")
    assert asking.pending_for_reply("1") is None


def test_no_text_is_never_an_answer():
    _open_question()
    assert asking.pending_for_reply("   ") is None


def test_called_with_nothing_behaves_as_it_always_did():
    """Existing callers pass no text; they must keep the old behaviour."""
    _open_question()
    assert asking.pending_for_reply() is not None


# --- and the confirmation should say who asked --------------------------------

def test_the_confirmation_names_the_asker_not_whatever():
    """He was told "Passed that back to whatever asked". Asta knows the source —
    it is a column on the row — and "whatever" reads as a machine that has lost
    track of its own errand."""
    q = _open_question()
    line = asking.delivered_line(q)
    assert "whatever" not in line.lower()
    assert "claude-code" in line
