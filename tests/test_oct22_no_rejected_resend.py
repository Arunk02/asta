"""22 Oct, Vinish and the cancellation bug.

1. He answered a staged draft with "No — compare with his latest msg on other
   bookings cancelled … suggest him clearly how it was our issue", and the chat
   brain sent the same draft, unchanged, fifteen seconds later.
2. Vinish's "why only this booking — we have so many cancelled ones?" keyed
   like his earlier "is this our code issue?" and got that old answer back.
"""

import asyncio

from app import loop, main, results_cache

OLD = ("Cancel swaps TO to DB one, bookingEquipments stays inbound → mismatch → AP drops "
       "equipment, empty TMS. Gap's in ProcessCancelledBookingActivityImpl. Our bug.")


def test_a_rejected_draft_is_refused_word_for_word_and_nearly():
    loop.note_rejected({"what": OLD, "to": "Vinish Kumar"})
    assert loop.rejected_again(OLD)
    assert loop.rejected_again(OLD.replace("Our bug.", "Our bug!"))     # near-copy
    assert loop.rejected_again("Bro, consolidated on all your points: why only this one…") == ""


def test_the_send_tool_refuses_it(monkeypatch):
    from app import agent, teams_bridge
    sent = []
    monkeypatch.setattr(teams_bridge, "enabled", lambda: True)
    monkeypatch.setattr(teams_bridge, "logged_in_once", lambda: True)

    async def send(chat, text, allow_group=False):
        sent.append(text)
        return chat

    monkeypatch.setattr(teams_bridge, "send_message", send)
    loop.note_rejected({"what": OLD, "to": "Vinish Kumar"})
    out = asyncio.run(agent.teams_send_message("Vinish Kumar", OLD))
    assert "said no to" in out and sent == []
    asyncio.run(agent.teams_send_message("Vinish Kumar", "a revised, longer explanation"))
    assert sent == ["a revised, longer explanation"]


def test_a_no_at_the_draft_records_it(monkeypatch):
    from app import store
    conv = store.create_conversation("copilot", None)
    loop.stage(conv["id"], {"kind": "send", "channel": "teams", "to": "Vinish Kumar", "what": OLD})
    monkeypatch.setattr(main, "_start_turn", lambda *a, **k: None)

    class Sink:
        async def send(self, payload):
            pass

    asyncio.run(main._dispatch(conv, "No compare with his latest msg on other bookings cancelled "
                                     "and send this message", Sink(), "whatsapp"))
    assert loop.rejected_again(OLD)


CTX = ("Vinish: logs confirm booking sent mismatched equipment ids to activity plan "
       "TO0524133009886 F2V24D548Y4QW F4SQWXRJZHDNM H2YT7DW4H97")


def test_a_new_question_about_the_same_case_is_a_new_finding():
    first = results_cache.key_for("ask", CTX + "\nThis is our code issue?", question="This is our code issue?")
    later = "But we have so many bookings cancelled, why is it failing only for this one?"
    assert first != results_cache.key_for("ask", CTX + "\n" + later, question=later)


def test_the_same_question_again_still_shares_the_finding():
    a = results_cache.key_for("ask", CTX + "\nThis is our code issue?", question="This is our code issue?")
    b = results_cache.key_for("ask", CTX + "\nis this our code issue??", question="is this our code issue??")
    assert a == b
