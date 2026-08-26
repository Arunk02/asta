"""A plan he can read in thirty seconds, standing up, on a phone.

Arun approves code plans from WhatsApp. The gate's output is trimmed to fit a
notification, and the trim preferred the TAIL — right for the question at the
bottom, wrong for the thing he actually reads first. He asked for the plan to be
shaped "kind of class diagram": what changes, where, and how the pieces relate.

So the plan opens with a STRUCTURE block and the trim pins it. Two ways to lose
it were live before this: a tail-only cut drops anything at the top of a long
plan, and the fence stripper deleted the tree outright when the brain wrapped it
in a code fence — which is what a brain naturally does with an indented diagram.
"""

from __future__ import annotations

from app import tasks

_TREE = """  EtaValidator                     NEW  · rejects import ETA at/after gate-in
    └─ called by BookingService.applyVesselEta()   ~10 lines changed
         └─ reads ServicePlanLeg.portGateIn (LATEST)
  BookingServiceTest               +3 cases (before / at / after gate-in)"""

_STEPS = """STEPS
1. Add EtaValidator with a single check(eta, gateIn) -> boolean.
2. Call it from applyVesselEta before the persist.

RISK: a caller relying on the silent skip now gets a 4xx.

Reply 'PLAN APPROVED' to implement."""

_FILLER = "\n".join(f"- discovery note {i}: read BookingService and its tests" for i in range(60))


def _plan(head: str) -> str:
    return f"Boot 0 done. CONTEXT CLEAR.\n\n{head}\n\n{_FILLER}\n\n{_STEPS}"


def test_the_shape_survives_a_plan_too_long_to_send():
    """A tail-only cut drops the top of the plan, which is where the shape is."""
    out = tasks._phone_text(_plan(f"STRUCTURE\n{_TREE}"))
    assert "EtaValidator" in out and "└─" in out
    assert "PLAN APPROVED" in out, "the question at the bottom still has to arrive"


def test_a_fenced_tree_keeps_its_contents_and_loses_its_fence():
    """Wrapping an indented diagram in a fence is what a brain naturally does,
    and the fence stripper deleted the whole thing."""
    out = tasks._phone_text(_plan(f"STRUCTURE\n```\n{_TREE}\n```"))
    # The tree marker, not "EtaValidator": that name also appears in STEPS, so
    # the first version of this assertion passed with the whole block deleted.
    assert "└─" in out and "applyVesselEta" in out
    assert "```" not in out


def test_build_output_is_still_dropped():
    """Only the fence that holds the tree is spared. Everything else is code or
    build noise, and letting it through is what made the phone text unusable."""
    noisy = ("STRUCTURE\n" + _TREE + "\n\nOUTPUT\n```\n"
             + "\n".join(f"[INFO] compiling module {i}" for i in range(40)) + "\n```\n\n" + _STEPS)
    out = tasks._phone_text(noisy)
    assert "└─" in out
    assert "compiling module" not in out


def test_the_block_does_not_swallow_the_rest_of_the_plan():
    """It ends at the first blank line followed by something unindented."""
    keep = (f"STRUCTURE\n{_TREE}\n\n{_STEPS}").splitlines()
    start, end = tasks._structure_span(keep)
    assert keep[start] == "STRUCTURE"
    assert "EtaValidator" in "\n".join(keep[start:end])
    assert "RISK" not in "\n".join(keep[start:end])


def test_internal_blank_lines_stay_inside_the_block():
    lines = ["STRUCTURE", "  A  NEW", "", "  B  changed", "", "STEPS", "1. do it"]
    start, end = tasks._structure_span(lines)
    assert lines[start:end][-1].strip() == "B  changed"


def test_a_plan_without_a_structure_block_is_unchanged():
    """Most gates are not plans — verification, a repo question, a draft. They
    must trim exactly as they did."""
    plain = _plan("Here is what I found while reading the service.")
    out = tasks._phone_text(plain)
    assert "PLAN APPROVED" in out
    assert tasks._structure_span(out.splitlines()) == (-1, -1)


def test_it_still_fits_a_notification():
    out = tasks._phone_text(_plan(f"STRUCTURE\n{_TREE}"), limit=1100)
    assert len(out) <= 1100


def test_a_structure_block_alone_survives():
    """A short plan that is nothing but its shape must not come back empty."""
    out = tasks._phone_text(f"STRUCTURE\n{_TREE}")
    assert "└─" in out and "EtaValidator" in out


def test_the_heading_is_matched_however_the_brain_writes_it():
    for head in ("STRUCTURE", "## Structure", "**Structure**", "Class diagram:"):
        start, _ = tasks._structure_span([head, "  A  NEW"])
        assert start == 0, head


def test_the_brain_is_actually_told_to_produce_one():
    """A trim that preserves a block nothing writes preserves nothing."""
    assert "STRUCTURE" in tasks.CODE_OVERRIDES
    assert "THIRTY SECONDS" in tasks.CODE_OVERRIDES


def test_the_plan_gate_is_still_unconditional():
    """The rule this readability work sits inside — he removed the
    small-change exemption deliberately."""
    assert "THE PLAN GATE IS UNCONDITIONAL" in tasks.CODE_OVERRIDES
