"""“sendd” is a yes.

2026-09-07, 12:26. A draft to Alex was staged and Asta asked him to reply
"send". He replied `sendd`. It missed the regex by one key, so it was routed as
revision feedback: the brain was told he had NOT approved it, could make nothing
of the word, and re-staged the identical draft — "Re-staged the same draft to
Alex. It's queued". He typed `send` again a minute later and it went.

So a confirmation he had already given cost two more messages, a turn, and the
time. The fix forgives the slip; these tests are mostly about what it must NOT
forgive, because the thing on the other side of this gate is an irreversible
outward message to a colleague.
"""

from __future__ import annotations

import asyncio

import pytest

from app import loop, main, store


# --- the slip he actually made ---------------------------------------------------

@pytest.mark.parametrize("typed", ["sendd", "Sendd", "sendd!", " sendd ", "sedn"])
def test_a_mistyped_send_is_still_a_send(typed):
    approved, read_as = main._affirmation(typed)
    assert approved and read_as == "send"


@pytest.mark.parametrize("typed,target", [("yess", "yes"), ("okk", "ok"),
                                          ("okayy", "okay"), ("confirmm", "confirm")])
def test_the_other_confirmations_get_the_same_grace(typed, target):
    assert main._affirmation(typed) == (True, target)


def test_an_exact_yes_is_not_reported_as_a_correction():
    """Only a slip names what it was read as — otherwise every send would carry
    a note explaining itself."""
    assert main._affirmation("send") == (True, "")
    assert main._affirmation("👍") == (True, "")


# --- what it must never forgive -------------------------------------------------------
#
# These are the whole reason the tolerance is two shapes rather than an edit
# distance. Each of these is ONE edit from a confirmation, and each means
# something else — or nothing.

@pytest.mark.parametrize("typed", [
    "sent",          # one substitution from "send" — and the opposite claim
    "spend", "sand", "end", "send to blake instead", "resend later",
    "n",             # one substitution from "y"
    "no", "nope", "not now", "stop", "don't", "dont send",
    "shorten it", "s", "", "   ",
])
def test_a_near_miss_that_means_something_else_never_sends(typed):
    assert main._affirmation(typed)[0] is False


def test_a_decline_is_never_read_as_a_slip():
    """Belt and braces over the shape rules: anything that reads as no is no."""
    for word in ("no", "nah", "later", "skip", "drop it", "leave it", "👎"):
        assert main._affirmation(word)[0] is False


def test_the_two_lists_of_yes_cannot_drift_apart():
    """The typo targets are checked against the regex that defines a yes. Two
    independent lists of "what approval looks like" is how one of them quietly
    stops matching."""
    for word in main._AFFIRM_WORDS:
        assert main._AFFIRM.match(word), word


# --- and the loop it was stuck in ---------------------------------------------------------

class _Sink:
    def __init__(self):
        self.msgs: list[dict] = []

    async def send(self, m):
        self.msgs.append(m)

    def texts(self) -> str:
        return "\n".join(str(m.get("text", "")) for m in self.msgs)


def _staged(monkeypatch):
    """A draft to Alex waiting on 'can I send this?', as on the day."""
    conv = store.create_conversation(model="copilot", workspace=None)
    loop._awaiting[conv["id"]] = {
        "kind": "send", "what": "the retry fix is merged too, same change as the timeout one.",
        "to": "Alex", "channel": "teams", "to_group": False,
    }
    return conv


def test_the_typo_sends_instead_of_re_staging_the_same_draft(monkeypatch):
    ran: list[dict] = []
    started: list[str] = []

    async def fake_run_op(op, cid, sink, channel):
        ran.append(op)

    monkeypatch.setattr(main, "_run_op", fake_run_op)
    # Sealed, not just observed: without the fix this message takes the revision
    # path, and an unsealed _start_turn would reach for a real brain.
    monkeypatch.setattr(main, "_start_turn", lambda *a, **k: started.append(a[1]))
    conv = _staged(monkeypatch)
    sink = _Sink()
    task = asyncio.run(main._dispatch(conv, "sendd", sink, "web"))

    assert task is None                          # no turn burned re-drafting
    assert started == []                         # nothing went back to a brain
    assert len(ran) == 1                         # the recorded send ran
    assert ran[0]["args"]["to"] == "Alex"
    assert "Read “sendd” as “send”" in sink.texts()
    assert loop.awaiting(conv["id"]) is None


def test_real_feedback_still_revises_rather_than_sending(monkeypatch):
    """The whole point of the gate: anything that is not approval is revision,
    and it must not be widened by the typo rule."""
    ran: list[dict] = []

    async def fake_run_op(op, cid, sink, channel):
        ran.append(op)

    started: list[str] = []

    def fake_start(conv, prompt, sink, channel):
        started.append(prompt)

    monkeypatch.setattr(main, "_run_op", fake_run_op)
    monkeypatch.setattr(main, "_start_turn", fake_start)
    conv = _staged(monkeypatch)
    asyncio.run(main._dispatch(conv, "make it shorter", _Sink(), "web"))

    assert ran == []                             # nothing went out
    assert started and "did NOT approve" in started[0]


def test_a_word_that_means_no_does_not_send(monkeypatch):
    ran: list[dict] = []

    async def fake_run_op(op, cid, sink, channel):
        ran.append(op)

    monkeypatch.setattr(main, "_run_op", fake_run_op)
    monkeypatch.setattr(main, "_start_turn", lambda *a, **k: None)
    conv = _staged(monkeypatch)
    asyncio.run(main._dispatch(conv, "sent", _Sink(), "web"))
    assert ran == []
