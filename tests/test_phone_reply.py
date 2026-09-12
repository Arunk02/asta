"""What lands on his phone when Asta answers a question there.

He got a four-hundred-word reply that was the model's entire debugging diary —
"I want to check the actual state of these tasks", "Fixing it now.", "Now
retrying ship." — with sentences welded together across paragraph boundaries
("…what's really there.Task #94 (transportAssetPriority…").

Two causes, one per fix here: the phone sink buffered every block of a multi-tool
agent loop and pushed the lot, and consecutive content blocks in the stream carry
no separator between them.
"""

from __future__ import annotations

import asyncio
import json

import pytest

from app import claude_cli, main


class _Phone:
    """Stands in for WhatsApp: records each push."""

    def __init__(self) -> None:
        self.sent: list[str] = []

    async def __call__(self, text: str) -> bool:
        self.sent.append(text)
        return True


def _run(events: list[dict]) -> list[str]:
    phone = _Phone()
    sink = main.PushSink(phone, "conv-1")

    async def go():
        for e in events:
            await sink.send(e)
    asyncio.run(go())
    return phone.sent


def test_only_the_answer_reaches_the_phone_not_the_running_commentary():
    sent = _run([
        {"type": "delta", "text": "Let me check the actual state first."},
        {"type": "tool", "status": "start", "name": "list_tasks"},
        {"type": "delta", "text": "Fixing it now."},
        {"type": "tool", "status": "start", "name": "edit"},
        {"type": "delta", "text": "Task #94 is done and ready to ship."},
        {"type": "done"},
    ])
    assert sent == ["Task #94 is done and ready to ship."]


def test_narration_is_kept_when_the_turn_ends_on_a_tool_call():
    """Silence is worse than narration — a turn whose last act is a tool call
    has nothing after it to send."""
    sent = _run([
        {"type": "delta", "text": "Shipping it now."},
        {"type": "tool", "status": "start", "name": "ship"},
        {"type": "done"},
    ])
    assert sent == ["Shipping it now."]


def test_a_turn_with_no_tools_is_unchanged():
    sent = _run([{"type": "delta", "text": "Yes — merged this morning."},
                 {"type": "done"}])
    assert sent == ["Yes — merged this morning."]


def test_notes_and_errors_still_go_out_as_they_happen():
    sent = _run([{"type": "note", "text": "✚ adding to the running task"},
                 {"type": "delta", "text": "done"},
                 {"type": "done"}])
    assert sent == ["✚ adding to the running task", "done"]


def test_a_tool_end_event_does_not_clear_the_answer():
    """Only `status: start` marks the boundary; anything else must not eat it."""
    sent = _run([{"type": "delta", "text": "The answer."},
                 {"type": "tool", "status": "end", "name": "x"},
                 {"type": "done"}])
    assert sent == ["The answer."]


# --- the welded sentences ----------------------------------------------------

def _stream(*evs: dict) -> bytes:
    return b"".join(json.dumps(e).encode() + b"\n" for e in evs)


def _blocks(*texts: str) -> list[dict]:
    out: list[dict] = []
    for t in texts:
        out.append({"type": "stream_event",
                    "event": {"type": "content_block_start",
                              "content_block": {"type": "text"}}})
        out.append({"type": "stream_event",
                    "event": {"type": "content_block_delta",
                              "delta": {"type": "text_delta", "text": t}}})
    return out


@pytest.mark.parametrize("first,second,joined", [
    ("Let me look at what's really there.", "Task #94 is done.", False),
    ("Fixing it now.", "I don't have a direct file-edit tool.", False),
    ("A trailing space ", "and the next block.", True),
])
def test_consecutive_content_blocks_are_separated(first, second, joined):
    """They arrived welded: '…really there.Task #94 (transportAssetPriority…'."""
    chunks: list[str] = []

    async def on_delta(text: str) -> None:
        chunks.append(text)

    async def go():
        for e in _blocks(first, second):
            ev = e["event"]
            if (ev["type"] == "content_block_start" and chunks
                    and ev["content_block"]["type"] == "text"
                    and not "".join(chunks[-2:]).endswith((" ", "\n"))):
                await on_delta("\n\n")
            elif ev["type"] == "content_block_delta":
                await on_delta(ev["delta"]["text"])
    asyncio.run(go())

    out = "".join(chunks)
    assert (f"{first}{second}" in out) is joined
    assert first in out and second in out


def test_the_separator_is_wired_into_the_real_stream_reader():
    """The check above models the rule; this pins it to the code that runs."""
    import inspect
    src = inspect.getsource(claude_cli)
    assert 'ev.get("type") == "content_block_start"' in src
    assert 'chunks.append("\\n\\n")' in src


# --- "what are you doing?" while it is doing it ------------------------------

def test_a_status_ask_is_answered_without_waiting_for_the_lock(monkeypatch):
    """The local status answer already existed — inside the turn, which is inside
    the per-conversation lock. So the one question only ever asked WHILE
    something is running was the one that could not be answered until it
    stopped. He asked at 22:45, four minutes in, and got the reply at 22:48 when
    the work finished: by then it had answered itself."""
    import inspect
    src = inspect.getsource(main._conduct)
    before_lock = src.split("_turn_lock")[0]
    assert "is_status_ask" in before_lock, \
        "the status shortcut must be reached before the lock is taken"


def test_his_actual_words_reach_the_shortcut():
    """It was too narrow to match how he types: neither the "got it" lead-in nor
    the texted "u" was allowed, so the message went to the brain instead."""
    from app import activity
    for q in ("got it what u doing ?", "what u doing", "whats the update",
              "what are you doing?", "still working?", "u there", "how long",
              "anything yet", "ok what is happening", "progress"):
        assert activity.is_status_ask(q), q


def test_a_real_question_is_still_not_swallowed():
    """The `$` anchor is what keeps it safe — a status ask carries no object."""
    from app import activity
    for q in ("what are you doing about the PR?", "any update on alex",
              "status of the booking service", "how long does the build take",
              "what is happening to the email service"):
        assert not activity.is_status_ask(q), q
