""""send komal review comments" to a draft waiting for Komal is its approval."""
from __future__ import annotations

import pytest

from app import main

DRAFT = {"kind": "send", "to": "Komal Jayswal", "channel": "teams", "what": "Two issues remain…"}


@pytest.mark.parametrize("said", ["send komal review comments", "send komal the review comments",
                                  "send komal the review", "send komal comments",
                                  "send komal's findings", "pls send komal the update",
                                  "send to komal", "send komal"])
def test_sending_the_named_person_their_review_approves_the_draft(said):
    assert main._send_to_staged(said, DRAFT)


@pytest.mark.parametrize("said", ["send navya review comments", "send komal review comments to the group",
                                  "send komal review comments but drop point 2"])
def test_someone_else_or_an_edit_is_not_its_approval(said):
    assert not main._send_to_staged(said, DRAFT)


def test_a_group_draft_is_never_approved_this_way():
    assert not main._send_to_staged("send komal review comments", {**DRAFT, "to_group": True})
