"""An ask for a file must be given the tool that makes one.

"make a short deck of my open tasks — id, title, status — and send it" ranked
ship_task, task_pr_status, approve_task… and not make_file: "tasks" swamped the
ranker. Two definitions of "he asked for a file" existed — consent's, which the
prompt used to TELL the brain to call make_file, and none at all in the router
that decides which tools the brain HAS. So the brain was told to call a tool it
had not been given. One definition, both doors.
"""

import pytest

from app import consent, tool_index


@pytest.mark.parametrize("ask", [
    "make a short deck of my open tasks — id, title, status — and send it",
    "send me an excel of the failed bookings",
    "give me a pdf of the release notes",
    "turn that into a csv",
])
def test_a_file_ask_is_given_make_file(ask):
    assert consent.asked_for_a_file(ask)
    assert "make_file" in tool_index.required_for(ask)


def test_the_router_and_the_prompt_share_one_definition():
    for ask in ("what is the status of PR 1251", "check booking 88271"):
        assert ("make_file" in tool_index.required_for(ask)) == consent.asked_for_a_file(ask)
