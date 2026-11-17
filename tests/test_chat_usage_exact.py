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


def _msg(mid, out, text=""):
    return {"type": "assistant", "message": {"id": mid, "usage": {
        "input_tokens": 10, "cache_creation_input_tokens": 1000, "cache_read_input_tokens": 0,
        "output_tokens": out}, "content": [{"type": "text", "text": text}] if text else []}}


def test_output_is_taken_from_the_settled_message(monkeypatch):
    lines = [_msg("m1", 2), _msg("m1", 180, "a long answer"),
             {"type": "result", "result": "a long answer", "total_cost_usd": 0.05,
              "session_id": "s1"}]

    async def spawn(*a, **k):
        return _Proc(lines)

    monkeypatch.setattr(claude_cli, "available", lambda: True)
    monkeypatch.setattr(claude_cli.asyncio, "create_subprocess_exec", spawn)
    seen = []
    conv = store.create_conversation(model="claude_cli", workspace=None)
    out = asyncio.run(claude_cli.run_turn(conv, "hi", on_usage=seen.append))
    assert out == "a long answer"
    u = seen[-1]
    assert u.output == 180, "the settled copy, not the first partial"
    assert u.cache_write == 1000, "one message counted once"
    assert u.cost_usd == 0.05
