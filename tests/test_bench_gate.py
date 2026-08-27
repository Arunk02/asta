"""The bench runs in CI, or it rots.

A measurement nobody runs is how `check_session` drifted for months while four
different sensors watched it. So the suite itself asserts a floor: capabilities
that are clean today stay clean, and the one known gap stays visible instead of
being quietly absorbed into an average.

Deliberately NOT a "reward must not drop" check against a stored baseline. That
sounds stricter and is worse — it turns every legitimately-added hard scenario
into a red build, so the cheapest way to keep CI green becomes adding easy cases.
Per-capability floors with a named exception can only be satisfied by the code
actually working.
"""

from __future__ import annotations

import pytest

from app import bench

#: Capabilities with no known gaps. Every scenario in these must pass.
CLEAN = ("triage", "summarise", "analyse", "plan", "code", "recover",
         "meetings", "ask", "message")

#: The one standing failure, and why it is allowed to stand. When someone fixes
#: the length backstop they delete this entry, and the gate holds them to it.
KNOWN_GAPS: dict[str, str] = {}


@pytest.mark.asyncio
async def test_every_clean_capability_is_still_clean():
    out = await bench.run()
    failed = [r for r in out["results"] if not r["ok"]]
    regressions = [r for r in failed
                   if r["capability"] in CLEAN and r["id"] not in KNOWN_GAPS]
    assert not regressions, "capability regressions: " + "; ".join(
        f"[{r['capability']}] {r['id']} — "
        f"{r['error'] or r['violations'] or r['missing'] or r['wrong']}"
        for r in regressions)


@pytest.mark.asyncio
async def test_the_known_gap_is_still_the_only_one():
    out = await bench.run()
    failed = {r["id"] for r in out["results"] if not r["ok"]}
    unexpected = failed - set(KNOWN_GAPS)
    assert not unexpected, f"new failures that nobody declared: {sorted(unexpected)}"
    fixed = set(KNOWN_GAPS) - failed
    assert not fixed, (
        f"{sorted(fixed)} now passes — delete it from KNOWN_GAPS so the gate "
        f"starts protecting it")


@pytest.mark.asyncio
async def test_no_scenario_ever_reaches_a_person():
    """The safety axis, asserted rather than assumed. A violation here means a
    scenario tried to call, join or message someone."""
    out = await bench.run()
    tripped = [(r["id"], r["violations"]) for r in out["results"]
               if any("teams_bridge" in v or "meetings" in v or "notify" in v
                      for v in r["violations"])]
    assert not tripped, f"scenarios tried to reach a person: {tripped}"


@pytest.mark.asyncio
async def test_running_everything_at_once_changes_nothing():
    """Parallel and serial must agree. Where they disagree, something is sharing
    state it should not — which is the exact class of bug that cost fourteen
    hours of Teams, found here for free instead of at 23:17 on a Wednesday."""
    serial = await bench.run(concurrency=1)
    parallel = await bench.run(concurrency=16)
    assert serial["passed"] == parallel["passed"]
    assert {r["id"] for r in serial["results"] if r["ok"]} == \
           {r["id"] for r in parallel["results"] if r["ok"]}


@pytest.mark.asyncio
async def test_the_suite_is_not_quietly_shrinking():
    """A gate over zero scenarios passes beautifully and means nothing."""
    out = await bench.run()
    assert out["total"] >= 60, f"only {out['total']} scenarios — did a suite fail to load?"
    covered = set(out["capabilities"])
    assert covered >= set(CLEAN), f"capabilities missing from the run: {set(CLEAN) - covered}"


@pytest.mark.asyncio
async def test_ci_never_spends_tokens_or_reaches_a_brain():
    """The gate must stay free and deterministic.

    Live cases go through Copilot CLI: they cost money, they need a working
    login, and their wording varies between runs. Any of those in a CI gate turns
    a red build into a coin flip, and a coin-flip gate gets ignored — which is
    exactly how a measurement stops protecting anything.
    """
    out = await bench.run()
    assert out["tokens"] == 0, f"the default run spent {out['tokens']} tokens"
    assert not [r for r in out["results"] if r["live"]], "a live case ran in CI"
    assert out["skipped_live"] > 0, (
        "no live cases were skipped — either they vanished or `live` stopped "
        "being honoured, and both mean CI is measuring something else")


def test_live_cases_declare_a_token_budget():
    """An unbudgeted live case reports thrift=1.0 whatever it spends, so a change
    that quietly triples the prompt would look free."""
    unbudgeted = [c["id"] for c in bench.load()
                  if c.get("live") and not (c.get("budget") or {}).get("tokens")]
    assert not unbudgeted, f"live cases with no token budget: {unbudgeted}"
