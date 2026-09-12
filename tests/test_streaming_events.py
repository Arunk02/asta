"""Reading pydantic-ai's own event objects, across a version bump.

Arun got "AttributeError: 'FunctionToolResultEvent' object has no attribute
'result'" instead of an answer. pydantic-ai renamed that field to `.part` in
2.13. The handler already used `getattr(..., "tool_name", "")` — defensive on the
INNER lookup and not on the outer one — so the rename raised out of the streaming
loop and took the whole reply with it, for the sake of a label on a tool
activity line.

Built against the REAL event classes, not stand-ins: a mock shaped the way the
code expects is exactly what would have kept passing through the rename.
"""

from __future__ import annotations

import dataclasses

import pytest
from pydantic_ai.messages import (FunctionToolCallEvent, FunctionToolResultEvent,
                                  PartDeltaEvent, PartStartEvent, TextPart,
                                  TextPartDelta, ToolCallPart, ToolReturnPart)

from app import main


def test_the_finished_tool_name_is_read_from_the_installed_library():
    event = FunctionToolResultEvent(
        part=ToolReturnPart(tool_name="teams_send_message", content="ok",
                            tool_call_id="1"))
    assert main._finished_tool_name(event) == "teams_send_message"


def test_the_old_field_name_still_works():
    """So a downgrade, or a pinned older version elsewhere, does not break."""
    class Old:
        result = ToolReturnPart(tool_name="jira_comment", content="ok",
                                tool_call_id="1")
    assert main._finished_tool_name(Old()) == "jira_comment"


def test_neither_field_costs_the_label_and_nothing_else():
    """Losing a tool label is worth nothing next to losing the turn."""
    class Neither:
        pass
    assert main._finished_tool_name(Neither()) == ""


def test_a_part_without_a_tool_name_is_not_an_error():
    event = FunctionToolResultEvent(part=TextPart(content="hi"))
    assert main._finished_tool_name(event) == ""


# --- the other events the loop reads, against the same library ---------------

def test_every_field_the_streaming_loop_reads_still_exists():
    """The rename was one field of five. Pinning all of them means the next bump
    fails HERE, in a test, instead of in his chat."""
    assert "part" in [f.name for f in dataclasses.fields(FunctionToolCallEvent)]
    assert "part" in [f.name for f in dataclasses.fields(PartStartEvent)]
    assert "delta" in [f.name for f in dataclasses.fields(PartDeltaEvent)]
    assert "content" in [f.name for f in dataclasses.fields(TextPart)]
    assert "content_delta" in [f.name for f in dataclasses.fields(TextPartDelta)]
    assert {"tool_name", "args"} <= {f.name for f in dataclasses.fields(ToolCallPart)}


def test_the_run_result_event_still_carries_its_result():
    """Usage accounting hangs off this one — silently zeroed, not crashed, if it
    ever moves."""
    from pydantic_ai import AgentRunResultEvent
    assert "result" in [f.name for f in dataclasses.fields(AgentRunResultEvent)]


def test_the_handler_no_longer_touches_the_attribute_directly():
    import inspect
    src = inspect.getsource(main)
    block = src[src.index("elif isinstance(event, FunctionToolResultEvent):"):]
    block = block[:block.index("elif isinstance(event, AgentRunResultEvent)")]
    assert "event.result" not in block
    assert "_finished_tool_name(event)" in block
