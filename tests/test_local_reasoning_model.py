"""The local brain went mute, and reported nothing at all.

Local models are increasingly REASONING models: they spend the whole token
budget thinking before writing a word. Measured on the qwen3.5-9b Arun has
loaded — asked for 200 tokens it used 199 on reasoning and returned
`content: ""`. `call_brain` asks for EIGHT.

So every feature on the free local brain was getting an empty string back and
reading it as "the model had nothing to say". Silently, on every call, for as
long as a reasoning model has been loaded. The same shape as the timeout bug
this file's neighbour fixed: None means "could not", "" means "said nothing",
and conflating them hides a dead brain behind a working one.
"""

from __future__ import annotations

import httpx
import pytest

from app import memory, store


def _reply(content="", reasoning=0, finish="stop", error=None):
    if error:
        return {"error": error}
    return {"choices": [{"finish_reason": finish,
                         "message": {"role": "assistant", "content": content,
                                     "reasoning_content": "thinking…" if reasoning else ""}}],
            "usage": {"completion_tokens_details": {"reasoning_tokens": reasoning}}}


@pytest.fixture
def calls(monkeypatch):
    """Records each request body and serves scripted replies."""
    seen: list[dict] = []
    monkeypatch.setattr(memory, "local_llm_model", lambda: "qwen/qwen3.5-9b")

    def make(replies):
        it = iter(replies)

        def fake_post(url, json=None, timeout=None):
            seen.append(json)

            class R:
                def json(self_inner):
                    return next(it)
            return R()
        monkeypatch.setattr(httpx, "post", fake_post)
        return seen
    return make


def test_thinking_is_turned_off_on_the_first_try(calls):
    """The only option that honours a small max_tokens — 0 reasoning tokens, the
    whole budget spent on the answer."""
    seen = calls([_reply("yes")])
    assert memory.local_llm_complete("is 2 > 1?", 8) == "yes"
    assert seen[0]["reasoning_effort"] == "none"
    assert seen[0]["max_tokens"] == 8, "a verdict must stay short"


def test_a_model_that_thinks_anyway_gets_room_to_finish(calls):
    """`enable_thinking:false` and `/no_think` were both measured as ignored by
    this model. Paying for the thinking is the fallback that always works."""
    seen = calls([_reply("", reasoning=8, finish="length"), _reply("yes")])
    assert memory.local_llm_complete("is 2 > 1?", 8) == "yes"
    assert len(seen) == 2
    assert seen[1]["max_tokens"] == 8 + memory._THINK_HEADROOM
    assert "reasoning_effort" not in seen[1], "retry without the refused parameter"


def test_a_server_that_rejects_the_parameter_still_gets_an_answer(calls):
    """Not every backend knows `reasoning_effort`; a 400 must not end the call."""
    seen = calls([_reply(error={"message": "unknown parameter reasoning_effort"}),
                  _reply("yes")])
    assert memory.local_llm_complete("is 2 > 1?", 8) == "yes"
    assert len(seen) == 2


def test_an_empty_answer_is_a_failure_not_an_answer(calls):
    """It returned "" — indistinguishable from a real empty answer, and the
    reason a working model looked like a mute one."""
    calls([_reply("", reasoning=8, finish="length"),
           _reply("", reasoning=520, finish="length")])
    assert memory.local_llm_complete("is 2 > 1?", 8) is None


def test_the_failure_is_recorded_rather_than_swallowed(calls):
    """A brain that answers nothing must be visible, the same way a wedged CLI
    turn is."""
    calls([_reply(""), _reply("")])
    memory.local_llm_complete("anything", 8)
    kinds = [(o["kind"], o["outcome"]) for o in store.recent_outcomes(5)]
    assert ("turn", "empty_local") in kinds


def test_whitespace_only_counts_as_empty(calls):
    calls([_reply("\n\n  "), _reply("  \n")])
    assert memory.local_llm_complete("anything", 8) is None


def test_a_real_answer_is_stripped_not_mangled(calls):
    """A thinking model prefixes its answer with the newlines it used to break
    out of reasoning."""
    calls([_reply("\n\nCONTEXT OK")])
    assert memory.local_llm_complete("say it", 32) == "CONTEXT OK"


def test_no_model_loaded_is_still_a_quiet_none(monkeypatch):
    monkeypatch.setattr(memory, "local_llm_model", lambda: None)
    assert memory.local_llm_complete("anything") is None


def test_a_timeout_is_still_reported(monkeypatch):
    monkeypatch.setattr(memory, "local_llm_model", lambda: "m")

    def boom(*a, **k):
        raise httpx.TimeoutException("too slow")
    monkeypatch.setattr(httpx, "post", boom)
    assert memory.local_llm_complete("anything") is None
    kinds = [(o["kind"], o["outcome"]) for o in store.recent_outcomes(5)]
    assert ("turn", "stopped_idle") in kinds


def test_the_headroom_is_configurable():
    """A bigger reasoning model may need more; it must not need a code change."""
    import inspect
    assert "ASTA_LOCAL_THINK_HEADROOM" in inspect.getsource(memory)


# --- and a context it can actually fit in ------------------------------------

def test_the_embedding_model_is_not_mistaken_for_the_brain(monkeypatch):
    """An embedding model is always loaded alongside and is listed FIRST, so
    "the first loaded model" picked it and reported its 2048-token context as
    the chat brain's."""
    def fake_get(url, timeout=None):
        class R:
            def json(self_inner):
                return {"data": [
                    {"id": "text-embedding-nomic-embed-text-v1.5", "state": "loaded",
                     "type": "embeddings", "loaded_context_length": 2048},
                    {"id": "qwen/qwen3.5-9b", "state": "loaded",
                     "loaded_context_length": 32768},
                ]}
        return R()
    monkeypatch.setattr(httpx, "get", fake_get)
    assert memory.local_ctx() == ("qwen/qwen3.5-9b", 32768)


def test_a_big_enough_context_is_left_alone(monkeypatch):
    """Reloading a 10 GB model for nothing costs five seconds and a cold cache."""
    monkeypatch.setattr(memory, "local_ctx", lambda: ("qwen/qwen3.5-9b", 32768))
    assert memory.ensure_local_context() == ""


def test_every_instance_is_unloaded_before_reloading():
    """`lms load` on an already-loaded model does not re-load it with the new
    setting, and if the unload has not settled it STACKS another instance —
    "qwen/qwen3.5-9b:2". A 10.45 GB model became 20 GB of a 32 GB machine, still
    serving 4096 from the first copy."""
    import inspect
    src = inspect.getsource(memory.ensure_local_context)
    assert "_loaded_instances" in src and "unload" in src
    assert src.index("unload") < src.index('"load"'), "unload must come first"


def test_duplicate_instances_are_all_found(monkeypatch):
    def fake_get(url, timeout=None):
        class R:
            def json(self_inner):
                return {"data": [{"id": "qwen/qwen3.5-9b", "state": "loaded"},
                                 {"id": "qwen/qwen3.5-9b:2", "state": "loaded"},
                                 {"id": "other/model", "state": "loaded"}]}
        return R()
    monkeypatch.setattr(httpx, "get", fake_get)
    assert memory._loaded_instances("qwen/qwen3.5-9b") == \
        ["qwen/qwen3.5-9b", "qwen/qwen3.5-9b:2"]


def test_health_fixes_it_rather_than_only_reporting_it():
    """A model loaded too small is WORSE than one not running: everything reports
    healthy and every local call dies. The fix is one reload; the alternative is
    him opening the GUI every time it auto-unloads."""
    import inspect
    from app import health
    assert "ensure_local_context" in inspect.getsource(health)
