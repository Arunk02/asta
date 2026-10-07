"""Calls use the same selected brain as chat without giving it call-side tools."""

import asyncio
import json

import pytest

from app import agent, call_mind, conversation, main


@pytest.mark.asyncio
async def test_new_calls_follow_the_shared_model(monkeypatch):
    selected = {"model": "copilot"}
    monkeypatch.setattr(main, "_preferred_model", lambda: selected["model"])
    monkeypatch.setattr(main, "_channel_model", lambda conv: selected["model"])
    async def prime(self, text, timeout):
        return "ready"
    monkeypatch.setattr(call_mind.CopilotMind, "_ask", prime)
    copilot = await call_mind.start("Vinish", "booking PR")
    assert isinstance(copilot, call_mind.CopilotMind)
    assert "Vinish" in copilot.system

    selected["model"] = "claude_cli"
    asked = []
    async def claude(system, model=""):
        asked.append((system, model))
        return "warm claude"
    monkeypatch.setattr(call_mind, "spawn", claude)
    agent.set_tier("claude_cli", "opus")
    assert await call_mind.start("Vinish", "booking PR") == "warm claude"
    assert "Vinish" in asked[0][0] and asked[0][1] == "opus"


@pytest.mark.asyncio
async def test_calls_do_not_silently_change_brain_when_unavailable(monkeypatch):
    monkeypatch.setattr(main, "_preferred_model", lambda: "copilot")
    monkeypatch.setattr(main, "_channel_model", lambda conv: "claude_cli")
    with pytest.raises(call_mind.Unavailable, match="copilot is unavailable"):
        await call_mind.start("Vinish", "booking PR")


@pytest.mark.asyncio
async def test_slow_copilot_does_not_ring_a_colleague(monkeypatch):
    monkeypatch.setattr(main, "_channel_model", lambda conv: "copilot")
    monkeypatch.setattr(conversation, "BRAIN_READY_SECONDS", 0.01)
    monkeypatch.setattr(conversation, "COPILOT_READY_SECONDS", 0.01)
    async def never_ready(*args, **kwargs):
        await asyncio.sleep(1)
    async def no_ring(*args, **kwargs):
        pytest.fail("the colleague must not be rung without a ready brain")
    from app import meetings
    monkeypatch.setattr(call_mind, "start", never_ready)
    monkeypatch.setattr(meetings, "call_person", no_ring)
    result = await conversation.converse("Vinish", "booking PR")
    assert "Didn't call" in result and "Nothing rang" in result


@pytest.mark.asyncio
async def test_copilot_call_streams_and_resumes_with_no_tools(monkeypatch):
    launched = []
    class Output:
        def __init__(self, lines):
            self.lines = iter(lines)
            self.exhausted = False

        async def readline(self):
            line = next(self.lines, b"")
            if not line:
                self.exhausted = True
            return line

    class Process:
        def __init__(self):
            events = [
                {"type": "assistant.message_delta", "data": {"deltaContent": "Okay. "}},
                {"type": "assistant.message_delta", "data": {"deltaContent": "I will check."}},
                {"type": "result", "exitCode": 0},
            ]
            self.stdout = Output([(json.dumps(e) + "\n").encode() for e in events])
            self.returncode = None

        async def wait(self):
            assert self.stdout.exhausted, "drain JSONL before waiting for CLI exit"
            self.returncode = 0
            return 0

        def kill(self):
            self.returncode = -9

    async def spawn(*command, **kwargs):
        launched.append((command, kwargs))
        return Process()

    monkeypatch.setattr(call_mind.asyncio, "create_subprocess_exec", spawn)
    mind = call_mind.CopilotMind("Phone-call rules")
    assert await mind._ask("Ready?", 25) == "Okay. I will check."
    first = [line async for line in mind.sentences("Hello")]
    second = [line async for line in mind.sentences("Thanks")]
    assert first == second == ["Okay.", "I will check."]
    assert "Phone-call rules" in launched[0][0][2]
    assert "Phone-call rules" not in launched[1][0][2]
    assert "--resume" in launched[1][0]
    assert "--available-tools=none" in launched[0][0]
    assert launched[0][1]["stdin"] == asyncio.subprocess.DEVNULL
