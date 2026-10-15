"""15 Oct: "chat, email or anything — unless they tag me or mention my name, why
does it need my attention? I told you multiple times."

"SR Client Certificate Renewal … @Shabda / @Yogesh - Can you pls take care of the
below" was pushed to him as "Outlook — needs you". Triage read the wording ("can
you") and never asked whom it was asking. Now a request is his only when it is a
1:1, @mentions or names him, or tags the whole room (@everyone).
"""

import asyncio

import pytest

from app import outlook, responder, triage


class _Notify:
    def __init__(self):
        self.sent = []

    async def notify(self, text, level="info", urgency="direct", priority=None, **kw):
        self.sent.append((text, urgency))


def _mail(sender, subject, preview=""):
    return {"unread": True, "important": False, "sender": sender,
            "subject": subject, "when": "9:00 AM", "preview": preview}


def test_a_mail_asking_two_colleagues_is_not_pushed_as_needing_him():
    n = _Notify()
    asyncio.run(outlook._push_mail(n, [_mail(
        "Platform Team", "SR Client Certificate Renewal-Retina Shared and Inventory Cluster",
        "@Shabda / @Yogesh - Can you pls take care of the below")]))
    assert n.sent, "still told once, quietly"
    text, urgency = n.sent[0]
    assert "needs you" not in text and urgency == "fyi"


def test_the_same_mail_naming_him_is_his():
    n = _Notify()
    asyncio.run(outlook._push_mail(n, [_mail(
        "Platform Team", "SR Client Certificate Renewal", "@Arun - Can you pls take care of the below")]))
    assert "needs you" in n.sent[0][0] and n.sent[0][1] == "direct"


@pytest.mark.parametrize("text,his", [
    ("Arunkumar can you check the build?", True),
    ("Arun K please review PR 1486", True),
    ("hi everyone please vote by 2 pm", True),          # @everyone, rendered without its @
    ("@here can someone confirm the deploy", True),
    ("@Shabda can you check the build?", False),
    ("can someone confirm the deploy", False),         # a room question nobody tagged
    ("Arunachalam can you check this", False),         # not his name, only its start
])
def test_his_name_or_a_room_tag_makes_it_his(text, his):
    assert triage.classify("Someone", text).action is his


def test_a_1_to_1_is_always_his():
    assert triage.classify("Komal", "can you check this", addressed=True).action is True


def test_a_model_cannot_make_an_untagged_item_his(monkeypatch):
    from app import memory

    async def act(*a, **k):
        return "ACT"

    monkeypatch.setattr(triage, "_complete", act)
    monkeypatch.setattr(memory, "quick_verdict", act, raising=False)
    v = triage.classify("Platform Team", "quarterly cluster maintenance window")
    assert asyncio.run(triage.refine(v, "Platform Team", "quarterly cluster maintenance window")).action is False


def test_the_responder_still_sees_an_ask_in_a_1_to_1():
    # Only his own messages reach the responder; it asks "is this a request?", not "is it his?".
    assert responder.what_it_asks("can you check why the build failed") != ""


# --- the yes is spent on what was offered ---------------------------------------------
#
# 12:07: "…want me to ask which ones you want to tackle first?" — he said "Go head",
# and a code task started fixing every point on his PR 1476 branch.

from app import consent, notify, store  # noqa: E402


def test_a_bare_yes_to_an_offer_that_was_not_code_starts_no_code():
    held = consent.yes_to_something_else("Go head", "code", "ask which ones you want to tackle first")
    assert "not an offer to change code" in held


@pytest.mark.parametrize("turn", ["fix them", "go ahead and fix P1", "raise a PR for it"])
def test_saying_what_to_do_is_not_a_bare_yes(turn):
    assert consent.yes_to_something_else(turn, "code", "ask which ones you want to tackle first") == ""


def test_a_yes_to_an_offer_to_fix_starts_the_fix():
    assert consent.yes_to_something_else("go ahead", "code", "fix the P1 and P2 issues on the PR") == ""


def test_only_code_is_held():
    assert consent.yes_to_something_else("go ahead", "analysis", "ask which ones") == ""


def test_the_last_offer_is_read_from_what_reached_his_phone(monkeypatch):
    monkeypatch.setattr(notify, "SAID_WINDOW", 3600, raising=False)
    notify._note_on_phone("Vinish shared the issues. Want me to ask which ones you want to tackle first?")
    assert notify.last_offer().startswith("ask which ones you want to tackle first")
    assert notify.last_offer(now=__import__("time").time() + 7200) == ""


def test_the_spawn_guard_calls_it():
    import inspect

    from app import agent
    assert "yes_to_something_else" in inspect.getsource(agent)
