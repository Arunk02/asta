"""What Asta makes of what it hears in a call.

Split out of `meetings` once that module had grown to cover four unrelated jobs:
building invites, running a call, reading captions, and deciding what — if
anything — to say about them. The first three are mechanics; this one is
judgement, and it is the part with a measurable right answer.

The rule the whole module exists to keep: **the bar for saying something out loud
is higher than the bar for telling him.** Getting it wrong on his phone costs a
glance. Getting it wrong out loud costs an incorrect sentence in front of a
colleague, in a conversation he cannot take back. So regexes decide what is worth
noticing, and a second opinion decides what is worth saying.

Nothing here touches the microphone, the browser, or the call. It takes a line of
text and returns a judgement, which is why it can be tested — and evaluated
against real questions about his codebase — without a call existing at all.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import re

from . import quiet

#: A spoken answer is capped at this many words. Nobody listens to four paragraphs
#: read aloud by an assistant, and every extra word is more synthesis latency and
#: more time holding his microphone. The full answer always goes to his phone.
SPOKEN_ANSWER_WORDS = int(os.environ.get("ASTA_SPOKEN_ANSWER_WORDS", "45"))

#: Questions already raised this call, so a caption settling over several polls
#: does not ask him the same thing five times.
_ASKED: set[str] = set()

#: Questions Asta may offer to look up: they are about the CODE, not about him.
_ANSWERABLE = re.compile(
    r"\b(how (does|do|is|are|did)|where (is|are|do|does)|what (does|do|is|are)\b"
    r"|which (topic|class|service|table|field|repo|method)"
    r"|why (does|is|are|did)|who (calls|consumes|publishes)"
    r"|is there (a|any)\b)", re.I)

#: Questions about HIM. Never auto-answered, in any voice.
_HIS_TO_ANSWER = re.compile(
    r"\b(can you (review|check|look|merge|approve|deploy|release|join|come)"
    r"|will you\b|could you\b|shall we\b|should we\b|are you (ok|fine|free|available|done)"
    r"|when (can|will) you\b|do you (want|mind|agree)|is that (ok|fine)\b"
    r"|what do you think\b|your (call|view|opinion)\b)", re.I)

#: Asks already put to him this call, so repeated captions do not re-ask.
_ASKED: set[str] = set()

def _ask_key(line: str) -> str:
    import hashlib
    words = re.findall(r"[a-z0-9]+", (line or "").lower())
    return hashlib.sha1(" ".join(words[:14]).encode()).hexdigest()[:16]

def classify_line(line: str) -> str:
    """'answerable' | 'his' | 'chatter' for one caption line.

    Order matters: a line can look like both ("can you check how the ATA
    fallback works"), and when it does it is HIS — the sentence is a request of
    him that happens to mention code, and answering it would be answering for
    him.
    """
    text = (line or "").strip()
    if len(text) < 12:
        return "chatter"
    if _HIS_TO_ANSWER.search(text):
        return "his"
    if _ANSWERABLE.search(text):
        return "answerable"
    return "chatter"

def notice_asks(lines: list[str], speaker_is_him: bool = False) -> list[dict]:
    """New things worth reacting to, deduped for the life of the call.

    His OWN lines are skipped: Asta offering to look up a question Arun himself
    just asked out loud is noise, and worse, it would offer to answer the person
    he is talking to on their behalf.
    """
    out = []
    if speaker_is_him:
        return out
    for line in lines:
        kind = classify_line(line)
        if kind == "chatter":
            continue
        key = _ask_key(line)
        if key in _ASKED:
            continue
        _ASKED.add(key)
        out.append({"line": line.strip(), "kind": kind, "key": key})
    return out

def clear_noticed() -> None:
    """Forget this call's asks — a new call starts with a clean slate."""
    _ASKED.clear()

def _call_tools() -> list:
    """The only tools the mid-call brain is given.

    Written out rather than narrowing the capability registry, because the
    registry's ALWAYS set carries `ask_user`, `delegate_task` and
    `prepare_to_send` — none of which have any business firing off a sentence
    somebody happened to say in a meeting. A live call is the worst possible
    place to discover that an overheard phrase looked like an instruction, so the
    brain does not hold a single tool that can change anything.
    """
    from . import agent as agent_mod
    return [agent_mod.resolve_context, agent_mod.read_workspace_file,
            agent_mod.list_services, agent_mod.search_memory]

_ANSWER_PROMPT = (
    "You are Arun's assistant, listening to a live call. Somebody just asked:\n\n"
    "  {question}\n\n"
    "Answer it from the codebase and from what you can find in memory of past "
    "conversations. {ws}Use resolve_context FIRST to find the right files, then read "
    "them; use search_memory for anything that sounds like it was discussed before.\n\n"
    "Answer in at most three sentences, plainly, as you would say it out loud. No "
    "preamble, no bullet points, no markdown. If the codebase does not actually "
    "tell you, say you'd have to check properly — a confident wrong answer in a "
    "live call is far worse than admitting you don't know."
)

async def answer_from_knowledge(question: str) -> str:
    """What Asta can work out about a code question. '' when nothing could answer.

    Empty string rather than an apology template: the caller has to be able to
    tell "here is the answer" from "no brain was available", because one of those
    gets said out loud and the other must not be.
    """
    from . import agent as agent_mod
    from .workspace import registry
    ws = ""
    with contextlib.suppress(Exception):
        ws = registry.infer(question) or ""
    # The lessons Arun's own corrections produced, handed to the thing answering.
    # They existed, they were written FROM his corrections, and the answering path
    # never read them: asked why the booking build fails with a FilerException —
    # documented in lessons.md, cause and fix — the answer came back "I couldn't
    # find any reference to that". Capturing a lesson and never consulting it is
    # the same as not capturing it.
    facts = ""
    with contextlib.suppress(Exception):
        from . import workspace as ws_mod
        conv = ws_mod.conventions(ws) if ws else ""
        if conv and conv.strip():
            facts = ("\n\nWHAT THIS WORKSPACE HAS ALREADY LEARNED — these are "
                     "verified facts written from Arun's own corrections. Prefer "
                     "them over anything you infer from reading the code:\n"
                     + conv[:6000] + "\n")
    prompt = _ANSWER_PROMPT.format(
        question=question[:500],
        ws=f"The workspace is '{ws}'. " if ws else "") + facts
    # The API model first when it works, then the CLI subscriptions Arun already
    # pays for. Measured reason for the fallback: `ANTHROPIC_API_KEY` was set,
    # `available("claude")` said yes because a key was PRESENT, and every call
    # 401'd because the key was invalid. This function returned "" each time and
    # said nothing, so the in-call brain was silently dead while two working CLI
    # brains sat unused.
    try:
        from pydantic_ai import Agent
        name = agent_mod.best_model_name()
        result = await Agent(model=agent_mod.get_model(name),
                             tools=_call_tools(), retries=1).run(prompt)
        answer = (result.output or "").strip()
        if answer:
            return answer
    except Exception as exc:                          # noqa: BLE001
        quiet.note("brain.api_answer", exc)
        # A refused credential is durable: retrying it every question spends a
        # round trip to be told the same thing, and hides the working brains.
        if agent_mod.credential_failure(str(exc)):
            agent_mod.mark_key_rejected("claude", str(exc))

    # A CLI brain reads the workspace directly, which for a question about his
    # own code is not a downgrade — it is the same tooling a code task uses.
    from .workspace import registry as _reg
    root = ""
    with contextlib.suppress(Exception):
        entry = _reg.get(ws) if ws else None
        root = str(entry.root) if entry else ""
    for cli in ("claude_cli", "copilot"):
        if not agent_mod.available(cli) or agent_mod.quota_down(cli):
            continue
        try:
            text = await agent_mod.runner(cli).one_shot(prompt, cwd=root or None,
                                                        timeout=120)
        except Exception as exc:                      # noqa: BLE001
            quiet.note(f"brain.{cli}_answer", exc)
            continue
        if (text or "").strip():
            return text.strip()
    return ""

def spoken_form(answer: str) -> str:
    """Trim an answer down to something worth listening to.

    Nobody sits through four paragraphs read aloud, and every extra word is more
    synthesis latency and longer holding his microphone. The full answer goes to
    his phone regardless, so nothing is lost by cutting it here.
    """
    words = " ".join((answer or "").split()).split(" ")
    if len(words) <= SPOKEN_ANSWER_WORDS:
        return " ".join(words)
    clipped = " ".join(words[:SPOKEN_ANSWER_WORDS])
    cut = max(clipped.rfind(". "), clipped.rfind("? "), clipped.rfind("! "))
    return clipped[:cut + 1] if cut > 40 else clipped + "…"

#: Second opinion before Asta opens its mouth. Local model only, and only when it
#: is already running: this sits between hearing a question and answering it, so a
#: paid round trip here would cost more latency than the answer itself.
CONFIRM_SPEECH = os.environ.get("ASTA_CONFIRM_SPEECH", "1").strip().lower() \
    not in ("0", "false", "no", "off")

async def confident(line: str) -> bool:
    """Is this really a question Asta may answer out loud?

    The classifier is regexes, and regexes are the right tool for deciding
    whether to put something on his phone — cheap, instant, and a false positive
    costs a glance. They are the wrong tool for deciding what to SAY: "can you
    check how the amend flow handles that" and "how does the amend flow handle
    that" differ by three words and by who is being committed to an answer.

    So a second opinion, from the local model that is already running. If it is
    not running, the honest answer is no: silence is always a safe outcome in a
    conversation, and an unnecessary sentence never is.
    """
    if not CONFIRM_SPEECH:
        return True
    from . import memory
    prompt = (
        "Someone said this in a work call:\n\n"
        f"  {line[:300]}\n\n"
        "Is that a general question about how a CODEBASE works — something anyone "
        "who knew the code could answer — or is it a request aimed at a specific "
        "person (asking them to do something, decide something, or say when)?\n\n"
        "Answer with one word: CODE or PERSON.")
    try:
        verdict = await asyncio.to_thread(memory.local_llm_complete, prompt, 8)
    except Exception as exc:                          # noqa: BLE001
        quiet.note("call.confirm_speech", exc)
        return False
    if not (verdict or "").strip():
        return False                       # nothing answered — do not speak
    return (verdict or "").strip().upper().startswith("CODE")

def pending_for_him(lines: list[str]) -> list[str]:
    """Things aimed at HIM, to hand back when the call ends."""
    return [l.strip() for l in lines if classify_line(l) == "his"]
