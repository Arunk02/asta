"""The two things that damage the reply path: noisy approvals, and a wrong recipient.

Both were found in his live state, not imagined. Three approval requests had
stacked up unanswered —

    🔎 Cyber Security Team asked about something new
    🔎 Ayontika Bhattacharyya asked about something new
    🔎 Vinish Kumar asked about something new

— and not one of them was worth asking about. A company-wide "Action Required"
mail, a channel post beginning "Everyone please review PR", and a bare link to a
repository called `incident-copilot` which the incident detector matched on the
word inside the URL. A queue of questions he never answers teaches him to ignore
the queue, and then the one that matters is ignored too.

The recipient half is worse because it is silent. What he approves is the NAME the
model typed; Teams opens whatever that name resolves to. "Divya" resolves to a
real, EMPTY 1:1, while the person he actually talks to is "Palikala Divya
Maheswari" — the message goes to the wrong person and the approval looks entirely
normal.
"""

from __future__ import annotations

import pytest

from app import main, responder, store


# --- approvals worth answering -----------------------------------------------

@pytest.mark.parametrize("who,text", [
    ("Cyber Security Team",
     "Action Required: Review Confluence & Jira Space Permissions Before AI Search"),
    ("Ayontika Bhattacharyya",
     "Everyone please review PR for the fix of BEPTELIKOS-10501"),
    ("Release Bot", "Dear all, the deployment window closes at 6pm. Do not reply."),
])
def test_a_broadcast_is_never_turned_into_an_approval(who, text):
    """Nobody is waiting on him; it went to a room."""
    assert responder.is_broadcast(who, text), text


@pytest.mark.parametrize("who,text", [
    ("Vinish Kumar", "can you check the production temporal bookings struck"),
    ("Rupesh Kumar", "Arunkumar can you confirm the ETA fix"),
])
def test_a_real_ask_is_not_swept_up_as_a_broadcast(who, text):
    """The gate must not become "ignore everything" — that loses the ones that
    matter, which is the failure it exists to prevent in the other direction."""
    assert not responder.is_broadcast(who, text), text


def test_a_word_inside_a_link_does_not_classify_the_message():
    """"https://github.com/VinishKumar1/incident-copilot" was read as a production
    INCIDENT and queued an approval, on the strength of a repository name."""
    assert responder.what_it_asks(
        "https://github.com/VinishKumar1/incident-copilot") == ""


def test_the_same_words_outside_a_link_still_count():
    """Stripping URLs must not blind it to a real incident."""
    assert responder.what_it_asks(
        "prod incident — bookings are stuck") == "incident"


def test_a_broadcast_is_refused_with_a_reason_he_could_read(monkeypatch):
    # conftest clears ASTA_RESPOND (it is machine-pinned), so without this the
    # "responder is off" gate answers first and the test proves nothing.
    monkeypatch.setenv("ASTA_RESPOND", "1")
    assert responder.should_respond("ask", 1, "k", broadcast=True) \
        == "addressed to a room, not to him"


# --- the right person --------------------------------------------------------

@pytest.fixture(autouse=True)
def _threads(monkeypatch):
    monkeypatch.setattr(main, "_known_threads",
                        lambda: ["Vinish Kumar", "Palikala Divya Maheswari",
                                 "Komal Jayswal"])


def _warn(to):
    return main._recipient_warning({"to": to, "channel": "teams"})


def test_the_exact_thread_he_uses_passes_silently():
    assert _warn("Palikala Divya Maheswari") == ""


def test_a_short_name_that_is_not_his_thread_is_flagged():
    """His live case. "Divya" opens a different, empty chat."""
    out = _warn("Divya")
    assert "Palikala Divya Maheswari" in out
    assert "different chat" in out


def test_a_name_matching_several_threads_asks_for_the_full_one():
    import app.main as m
    m._known_threads = lambda: ["Vinish Kumar", "Rajendra Kumar"]
    out = _warn("Kumar")
    assert "matches 2" in out and "in full" in out


def test_a_stranger_is_flagged_as_unknown():
    assert "No messages on record" in _warn("Nobody At All")


def test_the_warning_reaches_the_message_he_approves():
    """A check nothing displays is not a check."""
    import inspect
    assert "_recipient_warning(intent)" in inspect.getsource(main._present_staged_send)


def test_email_is_left_alone():
    """Mail addresses are not Teams threads, and warning on every one would be
    noise of exactly the kind this file is about."""
    assert main._recipient_warning({"to": "someone@maersk.com", "channel": "email"}) == ""


# --- a call reaches the same wrong person, louder -----------------------------

def test_a_short_name_resolves_to_the_person_he_talks_to(monkeypatch):
    """Teams' own search ranks a 1:1 titled "Divya" — a real chat with no messages
    in it — above "Palikala Divya Maheswari". His rail is the better authority."""
    from app import contacts
    monkeypatch.setattr(contacts, "known_threads",
                        lambda limit=800: ["Vinish Kumar", "Palikala Divya Maheswari"])
    assert contacts.resolve_name("divya")[0] == "Palikala Divya Maheswari"


def test_an_exact_name_is_left_alone(monkeypatch):
    from app import contacts
    monkeypatch.setattr(contacts, "known_threads",
                        lambda limit=800: ["Vinish Kumar", "Divya", "Palikala Divya Maheswari"])
    assert contacts.resolve_name("Divya")[0] == "Divya"


def test_an_ambiguous_name_is_left_undecided(monkeypatch):
    """Guessing which colleague he meant is the mistake that cannot be walked back
    — especially for a call, which rings them."""
    from app import contacts
    monkeypatch.setattr(contacts, "known_threads",
                        lambda limit=800: ["Vinish Kumar", "Rajendra Kumar"])
    settled, near = contacts.resolve_name("kumar")
    assert settled == "" and len(near) == 2


def test_calling_an_ambiguous_name_refuses_rather_than_ringing_someone(monkeypatch):
    import asyncio

    from app import contacts, meetings, teams_bridge
    # Stated rather than inherited: `call_person` checks the bridge is on before it
    # resolves anything, so without this the test passes on his laptop (where .env
    # sets TEAMS_BRIDGE=1) and fails on CI for a reason that has nothing to do with
    # name resolution.
    monkeypatch.setattr(teams_bridge, "enabled", lambda: True)
    monkeypatch.setattr(contacts, "resolve_name",
                        lambda n: ("", ["Vinish Kumar", "Rajendra Kumar"]))
    monkeypatch.setattr(meetings, "_CALL", {})
    with pytest.raises(RuntimeError, match="say which one"):
        asyncio.run(meetings.call_person("kumar"))


def test_the_call_path_uses_the_same_resolver_as_the_send_path():
    """One answer for "who does this name mean", or the two drift and a call goes
    somewhere a message would not."""
    import inspect
    assert "_contacts.resolve_name(who)" in inspect.getsource(meetings_src())


def meetings_src():
    from app import meetings
    return meetings.call_person
