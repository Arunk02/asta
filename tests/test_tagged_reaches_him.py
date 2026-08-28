"""Being tagged by name must reach him, even while he is at the laptop.

On 28 August four people used his name in Teams between 11:58 and 13:04 —
Komal, Nakka Harika, and Abhijit twice, once in a 1:1 and once in a channel —
and not one reached his phone. He found them by opening the screen:

    "Abhijit pinged me in multiple places one to one as well group chat ,
     I haven't get notificaiton in wgatsapp"

Nothing was broken. Each step was reasonable and the composition was silence:

  triage      "hi Arunkumar K" contains no ask verb → action=False
  attention   no ask, so P_FYI
  _push       needs=False → urgency="ambient"
  notify      ambient + presence.at_laptop() → held, never sent
  seen-set    already recorded, so it can never be retried

The rule he had already given, the day before: "if they tag me u have to respond
to me". Someone using his name IS the ask, whether or not the sentence parses as
one. Same shape as the L2 incident — reasonable layers composing into a loss.
"""

from __future__ import annotations

import pytest

from app import attention

TAGGED_DM = "Abhijit Mohapatra mentioned you — hi Arunkumar K — 13:04 — In chat with you"
TAGGED_CHANNEL = ("Abhijit Mohapatra mentioned you — Arunkumar K is already taken in "
                  "our team 😄 — 12:47 — OHP Garage - Le'ts Build, Inspire, Learn Together")
REACTION = ("Palikala Divya Maheswari reacted to your message — Just want to show — "
            "14:01 — In chat with you")


def _rank(line):
    return attention.rank(False, line, addressed=True, key=line[:24], who="Abhijit")


def test_a_tagged_dm_is_pushed_not_filed():
    pri, why, _ = _rank(TAGGED_DM)
    assert pri <= attention.P_TODAY, f"ranked p{pri} — held while he is at the laptop"
    assert "tagged you" in why


def test_a_tagged_channel_message_is_pushed_too():
    """His second case, and yesterday's "ohp garage" complaint — the same channel."""
    pri, _, _ = _rank(TAGGED_CHANNEL)
    assert pri <= attention.P_TODAY


def test_a_greeting_with_his_name_still_counts():
    """"hi Arunkumar K" has no ask verb in it. It is still somebody wanting him,
    and treating it as FYI is what produced the silence."""
    assert _rank("Someone mentioned you — hi Arunkumar K — In chat with you")[0] \
        <= attention.P_TODAY


def test_a_reaction_is_not_a_ping():
    """The other half. Reaction rows carry the same "in chat with you" marker, so
    flooring everything addressed would make every emoji a direct push — the noise
    that gets a notifier muted, and a muted notifier loses the real ones too."""
    pri, why, _ = _rank(REACTION)
    assert pri >= attention.P_FYI, f"an emoji ranked p{pri}"
    assert "tagged you" not in why


@pytest.mark.parametrize("text", [
    "Vinish Kumar reacted to your message — ok — In chat with you",
    "Komal liked your message — thanks — In chat with you",
])
def test_the_shapes_a_reaction_takes(text):
    assert attention.is_reaction(text), text


def test_a_real_message_is_never_read_as_a_reaction():
    assert not attention.is_reaction(TAGGED_DM)
    assert not attention.is_reaction("Priya mentioned you — can you review your PR")


def test_untagged_channel_noise_stays_quiet():
    """Not addressed at all: the floor must not become "push everything"."""
    pri, _, _ = attention.rank(False, "Someone posted: deploy finished", addressed=False,
                               key="k-noise", who="Someone")
    assert pri >= attention.P_FYI


def test_his_own_queue_still_outranks_the_tag_rule():
    """The two floors must not fight. A ticket in his group was already floored for
    its own reason, and that reason is the one worth reading in the ledger."""
    import app.attention as a
    real = a.MY_GROUPS
    a.MY_GROUPS = ("oh - telikos - l2",)
    try:
        pri, why, _ = a.rank(False, "Incident INC1 assigned to group OH - TELIKOS - L2",
                             addressed=True, key="k-q", who="IT Service Desk")
        assert pri <= a.P_TODAY
        assert "your group" in why
    finally:
        a.MY_GROUPS = real


# --- and the urgency has to read the rank ------------------------------------

def test_both_push_paths_send_a_ranked_item_directly():
    """The floor is only half the fix. `needs` comes from triage finding an ask
    verb, so a tagged greeting left urgency at "ambient" — which notify holds while
    he is at the laptop. Both sources must read the rank."""
    import inspect

    from app import outlook, teams_bridge
    for mod in (teams_bridge, outlook):
        src = inspect.getsource(mod)
        assert "wants_him = needs or started or (top is not None and top <= attention.P_TODAY)" \
            in src, f"{mod.__name__} still decides urgency without the rank"
