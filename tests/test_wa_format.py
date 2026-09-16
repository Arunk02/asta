"""What Arun actually sees on his phone.

Two separate defects lived in every push Asta has ever sent him, and the plan
screenshot showed both at once: WhatsApp is not markdown, so `**no changes**`
arrived as four literal asterisks; and the STRUCTURE tree is column-aligned,
which is right in a terminal and turns into a paragraph-shaped blob the moment a
forty-column bubble wraps it.
"""

from __future__ import annotations

from app import tasks, wa_format


# --- WhatsApp's markup -------------------------------------------------------

def test_double_asterisk_bold_becomes_whatsapp_bold():
    """The one he pointed at: '**no changes**' with the asterisks showing."""
    out = wa_format.for_whatsapp("services: **no changes** — already done")
    assert "*no changes*" in out and "**" not in out


def test_inline_backticks_are_dropped():
    """WhatsApp has no inline code, so a backtick is just a backtick on screen."""
    assert wa_format.for_whatsapp("call `setDomainData(x)` first") == \
        "call setDomainData(x) first"


def test_headings_and_bullets_are_translated():
    out = wa_format.for_whatsapp("### Steps\n- add the field\n* wire the mapper")
    assert out.splitlines() == ["*Steps*", "• add the field", "• wire the mapper"]


def test_a_fenced_block_is_left_exactly_as_written():
    """Monospace is the one piece of markdown WhatsApp shares, and a fence is the
    only place a literal asterisk or backtick is meant."""
    src = "before\n```\na = b ** 2  # `x`\n```\nafter"
    assert wa_format.for_whatsapp(src) == src


def test_conversion_happens_at_the_send_boundary_not_in_callers():
    """Thirty-odd call sites push text; one of them converting is the bug."""
    import inspect
    from app import notify
    assert "wa_format.for_whatsapp" in inspect.getsource(notify.wa_send)


# --- width -------------------------------------------------------------------

_ALIGNED = [
    "telikos-email-service (feature/asta-94-transportassetpriority-carry-the-field-t)",
    "  common/dto/TransportAssetPriority.java   NEW  · mirrors booking-service record: "
    "transportAssetPriorityGroupName, transportAssetPriorityName (both nullable String)",
    "  common/dto/BookingEquipment.java         +1 field: transportAssetPriority",
    "    └─ mapper/ServicePlanMapper.java       unchanged — MapStruct auto-generates it",
    "  <new> mapper/BookingEquipmentMappingTest.java   NEW  · asserts the field survives",
]


def test_no_line_is_wide_enough_to_wrap_on_a_phone():
    """The whole point. A wrapped aligned line puts its padding mid-sentence."""
    for line in wa_format.reflow_tree(_ALIGNED):
        assert len(line) <= wa_format.WIDTH + 8, line


def test_one_line_per_entry_and_no_continuation_lines():
    """The first version wrapped each note under its name. Every line fit and the
    block was still unreadable — six files became twenty stacked fragments, and a
    plan you have to assemble in your head is not one you can approve standing
    up. His word for it was "clumsy"."""
    out = wa_format.reflow_tree(_ALIGNED)
    assert len([l for l in out if l.strip()]) <= len(_ALIGNED) + 1   # +1: repo/branch split
    assert not any(l.strip().startswith("↳") for l in out)


def test_a_note_survives_when_the_name_leaves_room_for_it():
    """Dropping is a last resort, not the rule: a short name keeps its note."""
    out = wa_format.reflow_tree(["  EtaValidator   NEW · rejects ETA at/after gate-in"])
    assert out == ["  🆕 EtaValidator  rejects ETA at/after gate-in"]


def test_each_entry_is_marked_by_what_happens_to_it():
    out = wa_format.reflow_tree(_ALIGNED)
    assert any(l.startswith("  🆕 ") and "TransportAssetPriority.java" in l for l in out)
    assert any(l.startswith("  ✏️ ") and "BookingEquipment.java" in l for l in out)
    assert any("🧪" in l and "MappingTest" in l for l in out)


def test_nesting_survives_as_indent_without_the_box_characters():
    """Both said "this hangs off the one above". Keeping both cost four columns
    of a forty-six column line and pushed the glyph out of the column it is
    scanned in, so the indent stays and the box characters go."""
    out = wa_format.reflow_tree(_ALIGNED)
    nested = [l for l in out if "ServicePlanMapper" in l]
    assert nested and nested[0].startswith("    ") and "└" not in nested[0]


def test_the_glyphs_word_is_not_repeated_in_the_note():
    """'🆕 Foo.java  NEW · mirrors …' says NEW twice in nine characters, and a
    forty-six column line has no nine characters to spare."""
    out = wa_format.reflow_tree(["  Foo.java   NEW  · mirrors the record"])
    assert out == ["  🆕 Foo.java  mirrors the record"]


def test_a_note_that_only_restates_its_glyph_is_dropped_entirely():
    """"🆕 Foo.java  NEW" spends a third of the line saying it twice."""
    assert wa_format.reflow_tree(["  Foo.java   NEW"]) == ["  🆕 Foo.java"]
    assert wa_format.reflow_tree(["  Bar.java   unchanged"]) == ["  ⚪ Bar.java"]


def test_the_brains_own_inline_marker_is_dropped():
    out = wa_format.reflow_tree(["  <new> Foo.java   NEW · a thing"])
    assert "<new>" not in "\n".join(out)


def test_clip_never_ends_mid_identifier():
    assert wa_format.clip("transportAssetPriorityGroupName is nullable", 20) \
        .endswith("…")
    assert " " not in wa_format.clip("one two three four", 9).rstrip("…")[-1:]


def test_a_blank_line_stays_blank():
    assert wa_format.reflow_tree(["  A  NEW", "", "  B  changed"])[1] == ""


# --- the two joined together -------------------------------------------------

def test_the_plan_push_arrives_narrow():
    """End to end: `_phone_text` reflows the pinned STRUCTURE block."""
    plan = ("STRUCTURE\n" + "\n".join(_ALIGNED)
            + "\n\nSteps:\n1. add it\n\nRISK: low\n\nReply 'PLAN APPROVED'.")
    out = tasks._phone_text(plan)
    assert "🆕" in out and "PLAN APPROVED" in out
    tree = [l for l in out.splitlines() if l.startswith(" ")]
    assert tree and all(len(l) <= wa_format.WIDTH + 8 for l in tree)


def test_the_brain_is_told_that_a_reflow_can_only_cut():
    """What the note omits is gone; what it packs in gets truncated."""
    assert "reflows both blocks" in tasks.CODE_OVERRIDES
    assert "what you leave out of a note is lost" in tasks.CODE_OVERRIDES


# --- approving with amendments -----------------------------------------------

async def _noop(*a, **k):
    return None


def _gated(result: str = "PLAN READY\nadd the field"):
    from app import store
    t = store.create_task("carry the field", "code", "carry the field", None)
    store.update_task(t["id"], status="awaiting_approval", result=result)
    return t["id"]


def _reply(task_id: int, text: str) -> None:
    import asyncio

    async def go():
        tasks.reply(task_id, text)
        await asyncio.sleep(0)
    asyncio.run(go())


def test_a_plan_approved_with_changes_goes_straight_to_implementation(monkeypatch):
    """Exact-match meant "PLAN APPROVED — but hold the PDF half" scored as a
    rejection, ran at planning effort and cost a whole extra planning round
    before a line was written. Narrowing scope at the gate is the normal case."""
    seen: dict = {}

    async def spy(task_id, text, approved=False):
        seen["approved"] = approved
    monkeypatch.setattr(tasks, "_resume_worker", spy)

    _reply(_gated(), "PLAN APPROVED — but hold the PDF half, dto layer only")
    assert seen["approved"] is True


def test_an_amended_approval_is_not_scored_as_a_clean_one(monkeypatch):
    """Both are approvals to the pipeline and very different answers to the only
    question the metric asks — did the plan hold?"""
    from app import store
    monkeypatch.setattr(tasks, "_resume_worker", _noop)

    _reply(_gated(), "PLAN APPROVED")
    _reply(_gated(), "PLAN APPROVED — dto only")
    _reply(_gated(), "no, read the mapper first")

    got = [o["outcome"] for o in store.recent_outcomes(10) if o["kind"] == "plan"]
    assert got[:3] == ["replanned", "approved_amended", "approved"]   # newest first


# --- shortening a path -------------------------------------------------------

def test_a_long_path_loses_its_leading_segments_not_its_filename():
    """The filename is the whole point; `common/` is the part carrying nothing."""
    got = wa_format._fit_path("common/models/TransportAssetPriority.java", 37)
    assert got == "…/models/TransportAssetPriority.java"


def test_the_distinguishing_segment_is_never_dropped():
    """`common/dto/X` and `common/models/X` collapse to the same line without it.
    One line that wraps by three characters beats two he cannot tell apart."""
    a = wa_format._fit_path("common/dto/TransportAssetPriority.java", 20)
    b = wa_format._fit_path("common/models/TransportAssetPriority.java", 20)
    assert a != b and "dto" in a and "models" in b


def test_fitting_a_path_always_terminates():
    """It looped forever: each pass stripped the leading segment and then put the
    ellipsis back, so the string never got shorter."""
    import signal

    def bail(*_a):
        raise AssertionError("_fit_path did not terminate")
    signal.signal(signal.SIGALRM, bail)
    signal.alarm(5)
    try:
        for path in ("a/b/c/d/VeryLongClassNameHereIndeed.java", "x/y/z.java",
                     "Foo.java", "", "/", "a//b//c"):
            for budget in (0, 1, 5, 30, 200):
                wa_format._fit_path(path, budget)
    finally:
        signal.alarm(0)


# --- answering a gate from the phone -----------------------------------------

def test_the_brain_has_a_tool_for_an_amendment_at_all():
    """The plan message ends "or just reply with the changes", and for months
    nothing implemented that sentence: `approve_task` carries no words and
    `refine_task` at a gate only buffers, so an approval with conditions either
    lost the conditions or parked forever."""
    from app import capabilities
    cap = capabilities.get("reply_to_task")
    assert cap is not None and cap.fn is not None and cap.write


def test_an_amendment_through_that_tool_actually_resumes_the_task(monkeypatch):
    """The seam, not the registration: a capability that resolves and does
    nothing is the failure this is here to catch."""
    import asyncio
    from app import agent, store

    seen: dict = {}

    async def spy(task_id, text, approved=False):
        seen.update(task_id=task_id, text=text, approved=approved)
    monkeypatch.setattr(tasks, "_resume_worker", spy)

    tid = _gated()

    async def go():
        out = await agent.reply_to_task(tid, "PLAN APPROVED — dto only, hold the PDF half")
        await asyncio.sleep(0)
        return out
    detail = asyncio.run(go())

    assert seen["task_id"] == tid and seen["approved"] is True
    assert "hold the PDF half" in seen["text"], "his words must reach the pipeline"
    assert store.get_task(tid)["status"] == "running"
    assert "approved" in detail


def test_refine_at_a_plan_gate_only_buffers(monkeypatch):
    """Why the separate door exists. This is not a bug to fix — buffering is
    right for a RUNNING task — but at a gate nothing drains it until a gate
    action arrives, so feedback sent this way waits in silence."""
    import asyncio
    from app import store

    monkeypatch.setattr(tasks, "_resume_worker", _noop)
    tid = _gated()
    asyncio.run(tasks.refine(tid, "hold the PDF half"))

    assert store.get_task(tid)["status"] == "awaiting_approval"


def test_prose_around_the_tree_is_not_clipped_to_the_column_width():
    """The brain writes sentences beside the tree — "booking-service: no changes
    — their legs are already done". Clipping one to 46 columns deletes it;
    WhatsApp wraps prose perfectly well on its own."""
    line = ("telikos-booking-service / telikos-activityplanworkflow-service: no "
            "changes — their legs are already done on separate branches.")
    assert wa_format.reflow_tree([line]) == [line]


def test_a_generated_branch_name_is_not_shown_at_the_plan_gate():
    """A whole wrapped line spent on a name Asta made up, that he never types and
    decides nothing by. The DONE push carries it, where checking it out is why."""
    out = wa_format.reflow_tree(
        ["telikos-email-service (feature/asta-94-transportassetpriority-carry-the-field-t)"])
    assert out == ["📁 telikos-email-service"]


def test_a_human_aside_in_brackets_is_kept():
    """Only branch-shaped parentheticals go — a real aside is content."""
    line = "telikos-email-service (the one that renders the PDF)"
    assert wa_format.reflow_tree([line]) == [line]


def test_the_brains_own_sign_off_is_dropped_before_the_buttons():
    """Two asks in a row, the first unactionable, is the clutter he pointed at."""
    assert tasks._ASK_LINE.sub("", 'Reply "PLAN APPROVED" (or amend) to proceed.').strip() == ""
    assert tasks._ASK_LINE.sub("", "Reply 'PLAN APPROVED' to implement.\n").strip() == ""
    assert "keep me" in tasks._ASK_LINE.sub("", "keep me\nReply PLAN APPROVED now\n")


def test_no_triple_blank_lines_survive():
    plan = "STRUCTURE\n  A  NEW\n\n\n\nRISK: low\n\nReply 'PLAN APPROVED'."
    assert "\n\n\n" not in tasks._phone_text(plan)


# --- what happens in each class, and where it sits in the flow ----------------

def test_a_note_now_fits_beside_even_a_long_java_path():
    """Width was first set to 38 from an over-cautious guess, and that was wrong
    in the expensive direction: a Java path filled the line by itself, so every
    note was dropped as "not fitting" and the block became a bare list of
    filenames. He asked for the notes back — two or three words per class."""
    out = wa_format.reflow_tree(
        ["  common/dto/TransportAssetPriority.java   NEW · new record, 2 fields"])
    # The path is the half that shortens without loss; the note survives whole.
    assert out == ["  🆕 …/dto/TransportAssetPriority.java  new record, 2 fields"]


def test_a_new_test_file_does_not_say_new_twice():
    out = wa_format.reflow_tree(["  FooTest.java   NEW · field survives + null"])
    assert out == ["  🧪 FooTest.java  field survives + null"]


def test_the_flow_is_numbered_in_the_order_it_happens():
    """The tree says WHAT changes; the flow says WHERE it sits in the events —
    and that a service in the MIDDLE needs no change is the fact a reviewer most
    needs and the easiest one to get wrong."""
    out = wa_format.reflow_flow([
        "  booking-service   writes domainData JSON",
        "  AP workflow       carries it byte-for-byte, no change",
        "  email-service     deserialises into dto  <- change",
    ])
    assert out[0].startswith("1 booking-service · writes")
    assert out[1].startswith("2 AP workflow · carries")
    assert out[2].startswith("3 email-service · deserialises")


def test_the_step_the_change_lands_in_is_marked():
    out = wa_format.reflow_flow(["  email-service   deserialises into dto  <- change"])
    assert out[0].endswith("⬅ HERE") and "<-" not in out[0]


def test_the_marker_is_accepted_however_the_brain_writes_it():
    for m in ("<- change", "<-- change", "← change", "⬅ change", "<-", "← here"):
        assert wa_format.reflow_flow([f"  svc   does a thing  {m}"])[0].endswith("⬅ HERE"), m


def test_a_flow_step_the_brain_already_numbered_is_not_numbered_twice():
    out = wa_format.reflow_flow(["  1. booking-service   writes it",
                                 "  2. email-service     reads it"])
    assert out == ["1 booking-service · writes it", "2 email-service · reads it"]


def test_both_blocks_are_pinned_and_keep_their_order():
    plan = ("STRUCTURE\n  Foo.java  NEW · a new record\n\n"
            "FLOW\n  svc-a   writes it\n  svc-b   reads it  <- change\n\n"
            "RISK: low\n\nReply 'PLAN APPROVED'.")
    out = tasks._phone_text(plan)
    assert out.index("STRUCTURE") < out.index("FLOW")
    assert "🆕 Foo.java  a new record" in out
    assert "2 svc-b · reads it  ⬅ HERE" in out
    assert "RISK: low" in out


def test_a_plan_with_only_a_flow_block_still_pins_it():
    out = tasks._phone_text("FLOW\n  svc-a   writes it\n\nRISK: low\n\nReply 'PLAN APPROVED'.")
    assert "1 svc-a · writes it" in out


def test_neither_block_swallows_the_other():
    lines = ["STRUCTURE", "  A  NEW", "FLOW", "  x  does y", "RISK: low"]
    assert tasks._structure_span(lines) == (0, 2)
    assert tasks._block_span(lines, tasks._FLOW_HEAD) == (2, 4)


def test_the_brain_is_told_to_write_both_blocks_that_way():
    """A reflow that formats a block nothing writes formats nothing."""
    for rule in ("STRUCTURE", "FLOW", "TWO TO FOUR WORDS", "<- change", "THIRTY SECONDS"):
        assert rule in tasks.CODE_OVERRIDES, rule


def test_crispness_is_a_rule_for_every_channel_not_just_whatsapp():
    """"always make sure asta telling in crisp in whatsapp and other channels,
    unless i ask to elaborate, irrespective of what it is"."""
    from app import agent
    assert "BE CRISP" in agent.PERSONA
    assert "elaborate" in agent.PERSONA
    for channel in ("whatsapp", "telegram", "teams", "voice"):
        assert channel in agent.CHANNEL_NOTES, channel


def test_the_suite_cannot_reach_his_phone():
    """He asked why the same mic warning had arrived two hundred times in three
    days. It was the suite: `wa_send` posts to WA_BRIDGE_URL, which defaults to
    the bridge on his laptop, and five tests reach it on every run. The bridge is
    an outward door like the microphone and the voice server, and conftest points
    all three at nothing — this asserts it stays that way."""
    from app import notify, telegram

    assert notify.bridge_url() == "http://127.0.0.1:9"      # a closed port
    assert telegram.enabled() is False
