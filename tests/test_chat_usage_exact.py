"""A chat turn's usage counts each message once, from its settled copy.

17 Nov: the stream emits a message as partials, then settled; the first copy
carries only the opening output tokens, so ~16 output tokens a turn were
recorded whatever the answer's length."""
from __future__ import annotations

import asyncio
import json

from app import claude_cli, store


class _Proc:
    def __init__(self, lines):
        self._data = ("\n".join(json.dumps(x) for x in lines) + "\n").encode()
        self.returncode = 0
        self.stdout = self
        self.stderr = None

    async def read(self, n=-1):
        out, self._data = self._data[:4096], self._data[4096:]
        return out

    async def wait(self):
        return 0

    def kill(self):
        pass


def _turn(mid, start_out, final_out, text):
    """One model call as the real CLI streams it (recorded 18 Nov, haiku):
    message_start, ONE assistant record carrying the opening count, then the
    message_delta with the real one."""
    usage = {"input_tokens": 10, "cache_creation_input_tokens": 1000,
             "cache_read_input_tokens": 0, "output_tokens": start_out}
    return [
        {"type": "stream_event", "event": {"type": "message_start",
                                           "message": {"id": mid, "usage": usage}}},
        {"type": "stream_event", "event": {"type": "content_block_delta",
                                           "delta": {"type": "text_delta", "text": text}}},
        {"type": "assistant", "message": {"id": mid, "usage": usage,
                                          "content": [{"type": "text", "text": text}]}},
        {"type": "stream_event", "event": {"type": "message_delta",
                                           "usage": {"output_tokens": final_out}}},
    ]


def _run(monkeypatch, lines):
    async def spawn(*a, **k):
        return _Proc(lines)

    monkeypatch.setattr(claude_cli, "available", lambda: True)
    monkeypatch.setattr(claude_cli.asyncio, "create_subprocess_exec", spawn)
    seen = []
    conv = store.create_conversation(model="claude_cli", workspace=None)
    out = asyncio.run(claude_cli.run_turn(conv, "hi", on_usage=seen.append))
    return out, seen[-1]


def test_output_is_the_message_deltas_count_not_the_opening_one(monkeypatch):
    out, u = _run(monkeypatch, _turn("m1", 4, 199, "Five sentences about the sea.") + [
        {"type": "result", "result": "Five sentences about the sea.", "total_cost_usd": 0.05,
         "session_id": "s1"}])
    assert out == "Five sentences about the sea."
    assert u.output == 199, "the message_delta count, not the opening 4"
    assert u.cache_write == 1000, "one message counted once"
    assert u.cost_usd == 0.05


def test_every_call_of_a_tool_loop_counts_its_own_output(monkeypatch):
    lines = _turn("m1", 3, 40, "Checking. ") + _turn("m2", 2, 260, "Here it is.") + [
        {"type": "result", "result": "Checking. Here it is.", "total_cost_usd": 0.07,
         "session_id": "s1"}]
    _out, u = _run(monkeypatch, lines)
    assert u.output == 300 and u.cache_write == 2000
