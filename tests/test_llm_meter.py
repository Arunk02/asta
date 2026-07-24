"""Token metering — the accounting that was silently all zeros for CLI turns.

These lock in the two mistakes that made the old traces useless: dropping cache
writes (the biggest first-turn line), and letting an unmeasured brain read as
free.
"""

from __future__ import annotations

from app import llm_meter as m


def test_anthropic_block_is_parsed_including_cache_write():
    u = m.from_anthropic({
        "input_tokens": 2756, "output_tokens": 715,
        "cache_read_input_tokens": 6427, "cache_creation_input_tokens": 6689,
    })
    assert (u.input, u.output, u.cache_read, u.cache_write) == (2756, 715, 6427, 6689)
    assert u.measured is True


def test_effective_weights_the_expensive_tokens():
    """Output costs ~5x input; cache read is cheap. A raw sum hides both."""
    out_heavy = m.Usage(output=1000)
    read_heavy = m.Usage(cache_read=1000)
    assert out_heavy.effective > read_heavy.effective
    assert m.Usage(input=1000).effective == 1000


def test_summing_a_multi_call_turn():
    """A CLI turn is a whole agent loop; usage must accumulate across calls."""
    total = m.Usage()
    for _ in range(3):
        total = total + m.from_anthropic({"input_tokens": 10, "output_tokens": 5})
    assert total.input == 30 and total.output == 15


def test_an_estimate_is_never_marked_measured():
    """Mixing estimates into measured comparisons is how a trend line lies."""
    est = m.estimated(4000, 800)
    assert est.measured is False
    assert est.input == 1000 and est.output == 200


def test_a_measured_turn_plus_an_estimate_stays_measured_if_either_is():
    assert (m.from_anthropic({"input_tokens": 1}) + m.estimated(0, 0)).measured is True
    assert (m.estimated(4, 4) + m.estimated(4, 4)).measured is False


def test_garbage_usage_block_does_not_raise():
    assert m.from_anthropic(None).total == 0
    assert m.from_anthropic({}).total == 0
    assert m.from_anthropic("nonsense").total == 0


def test_trace_persists_the_new_columns(tmp_path, monkeypatch):
    """End to end: a recorded CLI-style turn is queryable, not zeroed."""
    from app import store
    monkeypatch.setattr(store, "DB_PATH", tmp_path / "t.db")
    store.init()
    store.add_trace("c1", "claude_cli", "web", 900, 4200,
                    2760, 1210, 36296, 0, 40, ["claude_cli"],
                    cache_write_tokens=10562, cost_usd=0.0, measured=True)
    row = store.list_traces(1)[0]
    assert row["cache_write_tokens"] == 10562
    assert row["measured"] == 1
    assert row["input_tokens"] == 2760
