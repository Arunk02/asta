"""The mind on the other end of a live call: one warm brain for the whole call.

The first call where Asta was actually heard (22 Sep, 11:35) ended on its first
exchange. The colleague said "Yes, we can go through", and the answer took 37 seconds —
long enough to time out — because each reply started a fresh CLI brain that
also read the workspace, which is the right machinery for a code question and
the wrong one for "yes, go on". On a call, a pause of a few seconds is the line
going dead.

So a call gets its own brain, started while the phone is still ringing and kept
for the whole call: each thing they say is one more message into a process that
is already running (measured: ~3-4 s a turn, against 6-9 s to start a new one).
No tools — nothing a call brain says can act on anything — and one job: say the
next one or two sentences, never commit Arun to anything, and mark the end of
the call when its purpose is done.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import re
import tempfile

#: The model a call talks with. Speed matters more than depth here: anything
#: that needs real digging is "I'll check with Arun and come back".
MODEL = os.environ.get("ASTA_CALL_MODEL", "sonnet")

#: The CLI a call brain runs on. A module constant so the test suite can point
#: it at nothing — a test must never start a real brain.
CLAUDE = "claude"

END = "[END]"

_PERSONA = (
    "You are Asta, Arun's assistant, speaking out loud on a live phone call with "
    "his colleague {who}. Why you called: {topic}.{agenda}{length}\n"
    "Talk like a person on a phone call, not a survey. Reply with ONLY the words "
    "you say next — usually ONE short sentence, two at most; plain words, no "
    "markdown, no lists, no stage directions.\n"
    "- React to what they actually said first (agree, answer, laugh it off, ask "
    "what they meant). Follow their lead; if they change the subject, go with it.\n"
    "- The points to cover are a loose guide, not a script: bring one up only when "
    "the conversation leaves room for it, in your own words, in whatever order fits.\n"
    "- ALWAYS start your reply with exactly one of these short reactions, word for "
    "word, as its own sentence: {reactions} — then say the rest. They are ready to "
    "play instantly, so the other person never waits in silence.\n"
    "- If they ask you something, answer it directly and honestly, briefly.\n"
    "- You have already said who you are. If they answer with a greeting of their "
    "own (\"hello\", \"hi\", \"how can I help you\"), do NOT introduce yourself "
    "again — go straight to why you called.\n"
    "- When they say they have not done a thing yet, ask ONCE for a rough sense of "
    "when, then let it go: pressing twice is what makes a call feel like chasing.\n"
    "- If they ask whether there is anything else, ANSWER it — yes and say what, or "
    "no and start closing. Do not reply with a pleasantry.\n"
    "- Close on what actually happened. Never thank them for something they did not "
    "give: if they had no update, say you will check back, not \"thanks for the "
    "update\". The last thing you say is what they remember of the call.\n"
    "- Speak the language they speak — English, Hindi, or the mix they use. When "
    "you speak Hindi, WRITE IT IN DEVANAGARI (आप कैसे हैं), never in Latin letters: "
    "the voice reads the script to decide how to pronounce it, and romanised Hindi "
    "comes out as mangled English.\n"
    "- If what they said came through garbled, say so naturally and ask again.\n"
    "- Never invent facts about Arun's work or plans, and never commit him to "
    "anything — if something needs him, say you will check with Arun.\n"
    "- If they say bye, thanks, or that they have to go, close in one short "
    f"sentence and put {END} at the end — never ask another question then.\n"
    "When the call is done, close warmly in one sentence and put "
    f"{END} at the very end."
)

#: Where a sentence ends in speech. A reply is spoken sentence by sentence, so
#: the first one plays while the rest are still being written.
_SENTENCE_END = re.compile(r"[.!?]+[\"')\]]*\s+")

#: Marks the end of the reply's text inside the event stream.
_COMPLETE = object()

#: The first thing a reply says, from a fixed set whose audio is made while the
#: phone rings — so the first sound after they stop talking needs no synthesis.
REACTIONS = ("Got it.", "Okay.", "Great.", "Right.", "Sure.", "Oh, nice.",
             "Hmm, fair point.", "Thanks.", "Makes sense.", "Good question.",
             "Perfect.", "No worries.", "Sorry.", "Hmm.", "Nice.", "Yes.",
             "Sounds good.", "Of course.", "Absolutely.")

#: A reply that opens with one of the reactions, however it is punctuated.
_REACTION_OF = {r.rstrip(".").lower().replace(",", ""): r for r in REACTIONS}
_OPENS_WITH = re.compile(
    r"(" + "|".join(re.escape(r.rstrip(".")).replace(",", ",?").replace(r"\ ", r"\s")
                    for r in REACTIONS) + r")[,.!—–-]+\s*", re.I)


def _capitalised(sentence: str, do: bool) -> str:
    return sentence[:1].upper() + sentence[1:] if do else sentence


class Unavailable(RuntimeError):
    """The call brain cannot answer — out of its usage window, or failing. Its
    message must never be spoken: on 22 Sep the reply to "Yes" came back as
    "You've hit your session limit · resets 2:40pm", ready to be read aloud."""


def _refusal(text: str) -> bool:
    from .agent import transient_limit
    return transient_limit(text)


class Mind:
    """One conversation's brain. `reply()` per turn; `close()` when the call ends."""

    def __init__(self, proc):
        self.proc = proc
        self.lock = asyncio.Lock()

    async def _ask(self, text: str, timeout: float) -> str:
        async with self.lock:
            msg = {"type": "user", "message": {"role": "user", "content": text}}
            self.proc.stdin.write((json.dumps(msg) + "\n").encode())
            await self.proc.stdin.drain()
            said = ""
            while True:
                line = await asyncio.wait_for(self.proc.stdout.readline(), timeout)
                if not line:
                    raise RuntimeError("the call brain stopped")
                try:
                    event = json.loads(line)
                except ValueError:
                    continue
                if event.get("type") == "assistant":
                    for part in (event.get("message") or {}).get("content") or []:
                        if part.get("type") == "text":
                            said += part.get("text") or ""
                if event.get("type") == "result":
                    if event.get("is_error") or _refusal(said):
                        raise Unavailable(said.strip() or str(event.get("result") or "error"))
                    return said.strip()

    async def reply(self, theirs: str, timeout: float = 25, elapsed: float = 0,
                    limit: float = 0) -> tuple[str, bool]:
        """(what to say, whether that ends the call)."""
        out = " ".join([s async for s in self.sentences(theirs, timeout, elapsed, limit)])
        ended = END in out
        return out.replace(END, "").strip(), ended

    async def sentences(self, theirs: str, timeout: float = 25, elapsed: float = 0,
                        limit: float = 0):
        """The reply, one sentence at a time, as fast as it is written."""
        clock = ""
        if limit:
            clock = f"\n[{int(elapsed)}s of about {int(limit)}s used"
            clock += ("; time is nearly up — close the call now]" if elapsed >= limit - 45
                      else "]")
        buffer = ""
        opened = False
        capital = False                   # the sentence after a split-off reaction
        async for piece in self._stream(f'They just said: "{theirs}"{clock}', timeout):
            if not opened and piece is not _COMPLETE:
                # A reply that opens with a ready reaction ("Great, glad to…")
                # sends the reaction the moment it is written; its audio is made.
                probe = (buffer + piece).lstrip()
                m = _OPENS_WITH.match(probe)
                if m:
                    opened = True
                    yield _REACTION_OF[m.group(1).lower().replace(",", "")]
                    buffer = probe[m.end():].lstrip()
                    capital = True
                    continue
                if len(probe) > 24 or _SENTENCE_END.search(probe):
                    opened = True
            if piece is _COMPLETE:
                # The message is whole; the CLI's own "done" follows ~1.4 s later,
                # and a last sentence held for it is 1.4 s of silence on the line.
                if buffer.strip():
                    yield _capitalised(buffer.strip(), capital)
                    capital = False
                buffer = ""
                continue
            buffer += piece
            while (m := _SENTENCE_END.search(buffer)):
                yield _capitalised(buffer[:m.end()].strip(), capital)
                capital = False
                buffer = buffer[m.end():]
        if buffer.strip():
            yield _capitalised(buffer.strip(), capital)

    async def _stream(self, text: str, timeout: float):
        """Text deltas of one reply. Falls back to the whole message when the CLI
        sends no deltas, so a flag change degrades to slower, never to silence."""
        async with self.lock:
            msg = {"type": "user", "message": {"role": "user", "content": text}}
            self.proc.stdin.write((json.dumps(msg) + "\n").encode())
            await self.proc.stdin.drain()
            streamed = False
            while True:
                line = await asyncio.wait_for(self.proc.stdout.readline(), timeout)
                if not line:
                    raise RuntimeError("the call brain stopped")
                try:
                    event = json.loads(line)
                except ValueError:
                    continue
                kind = event.get("type")
                if kind == "stream_event":
                    delta = ((event.get("event") or {}).get("delta") or {})
                    if delta.get("type") == "text_delta" and delta.get("text"):
                        streamed = True
                        yield delta["text"]
                elif kind == "assistant":
                    if not streamed:
                        whole = "".join(part.get("text") or ""
                                        for part in (event.get("message") or {}).get("content") or []
                                        if part.get("type") == "text")
                        # A usage-limit notice arrives whole, never streamed.
                        if _refusal(whole):
                            raise Unavailable(whole.strip())
                        if whole:
                            yield whole
                    yield _COMPLETE
                elif kind == "result":
                    if event.get("is_error"):
                        raise Unavailable(str(event.get("result") or "the call brain failed"))
                    return

    async def opener(self, timeout: float = 20) -> str:
        """The first line, written while the phone rings."""
        out = await self._ask(
            "The phone is ringing. Write the exact line you will say the moment they "
            "pick up, under fifteen words: greet them by first name, say you are "
            "Asta, Arun's assistant, and ask if now is a good time. Just the line.",
            timeout)
        return out.replace(END, "").strip()

    async def notes(self, timeout: float = 30) -> str:
        """After the hang-up: what Arun should know, and what to do better."""
        return await self._ask(
            "The call has ended. Write notes for Arun in plain text, 3 to 6 short "
            "bullets starting with '- ': what they said that matters, anything they "
            "asked for or that needs Arun, and any feedback on how Asta sounded or "
            "behaved on the call — with one concrete way to do better next time.",
            timeout)

    async def close(self) -> None:
        with contextlib.suppress(Exception):
            self.proc.stdin.close()
        with contextlib.suppress(Exception):
            await asyncio.wait_for(self.proc.wait(), 5)
        with contextlib.suppress(Exception):
            if self.proc.returncode is None:
                self.proc.kill()


async def start(who: str, topic: str, agenda: str = "", minutes: float = 0) -> Mind:
    """Spawn the call's brain and prime it while the phone rings."""
    # The same environment every CLI brain gets: his refused API key stripped
    # (left in, the CLI tries it instead of his subscription and stalls), and
    # Claude Code's private memory off.
    from .claude_cli import _subprocess_env
    env = _subprocess_env()
    # Only the call's own brief. Claude Code's default system prompt, his user
    # settings (hooks) and the MCP servers they bring (Grafana's 43 tools) all
    # ride along otherwise — measured on 22 Sep: first words in 1.9-4.4 s with
    # them, 0.9-1.2 s without. A call brain has nothing to look up anyway.
    proc = await asyncio.create_subprocess_exec(
        CLAUDE, "-p", "--input-format", "stream-json", "--output-format", "stream-json",
        "--verbose", "--include-partial-messages", "--model", MODEL, "--tools", "",
        "--setting-sources", "", "--strict-mcp-config", "--mcp-config", '{"mcpServers":{}}',
        "--system-prompt", _PERSONA.format(
            who=who or "a colleague", topic=topic or "a quick word",
            reactions=" / ".join(f'"{r}"' for r in REACTIONS),
            agenda=f"\nWhat to cover: {agenda}" if agenda else "",
            length=(f"\nThe call should last about {minutes:g} minutes: keep it going "
                    f"naturally until then, and close when told time is nearly up."
                    if minutes else "")),
        stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL, cwd=tempfile.gettempdir(), env=env)
    mind = Mind(proc)
    # The first answer of any session is the slow one; pay it now, not in front
    # of the person. A brain that is out of its usage window says so here —
    # before anybody's phone rings — rather than in the middle of the call.
    try:
        await mind._ask("The call is ringing. Reply with just: ready", 40)
    except Unavailable:
        await mind.close()
        raise
    except Exception:                                          # noqa: BLE001
        pass
    return mind
