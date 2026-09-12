""""New task" means a new task.

On 9 September every message Arun sent while task #117 was live got folded into
it — including one that literally began "New task". #117 was the
BookingEquipment equals/hashCode fix in telikos-booking-service; what it ended up
holding was clarifying questions about creating three Kafka topics in
empv3-tenant-intake, a different repo entirely.

The damage was not just a mis-filed message. Its DONE push carried one task's
TITLE over the other task's BODY, and so did its PLAN — a plan he was one word
away from approving, which would have provisioned topics and ACLs from a task
named after an unrelated bug fix. He typed "Approve task 117" and the only thing
that stopped it was the run noticing the mismatch itself.

Two causes:
  * `classify_interjection` had no detector for "this is separate work" at all.
  * `_ADD` actively MATCHED some of them — "new task: add a retry" classified as
    `augment` on the word "add", i.e. read as the exact opposite of what it said.
"""

from __future__ import annotations

import pytest

from app import activity, store, tasks


# --- what he actually typed --------------------------------------------------

@pytest.mark.parametrize("text", [
    "New task Hey have to create new topic for service plan , booking , transport order",
    "Now go and raise new topic create New task have to create new topic for service plan",
    "new task: add a retry to the client",
    "start a new task to check the logs",
    "do this as a separate task",
    "another task - look at the billing logs",
    "different task: chase the PRs",
])
def test_it_is_recognised_as_separate_work(text):
    assert activity.classify_interjection(text) == "new_task"


def test_the_add_cue_no_longer_wins_over_it():
    """"new task: add a retry" was classified `augment` on the word "add"."""
    assert activity.classify_interjection("new task: add a retry to the client") == "new_task"


@pytest.mark.parametrize("text", [
    "the new task is failing",
    "your new task looks wrong",
    "that new task never finished",
    "this new task is the one I meant",
])
def test_talking_ABOUT_a_task_does_not_start_one(text):
    """The lookbehinds keep it to the instruction. A question about running work
    must not spawn more of it."""
    assert activity.classify_interjection(text) != "new_task"


def test_the_other_verdicts_are_untouched():
    assert activity.classify_interjection("also add a test for the null case") == "augment"
    assert activity.classify_interjection("no, stop that") == "redirect"
    assert activity.classify_interjection("what's the status") == "status"


def test_it_is_checked_before_redirect_and_add():
    """It overrides both: "new task, stop doing X" still starts a new task."""
    assert activity.classify_interjection("new task, and also stop that") == "new_task"


# --- and it changes what happens ---------------------------------------------

def test_a_live_task_refuses_to_absorb_it():
    """Belt and braces at the far end: even if routing goes wrong, a task must
    not swallow work that announced itself as separate."""
    t = store.create_task("fix equals/hashCode", "code", "fix it", None)
    with pytest.raises(ValueError, match="new task"):
        tasks.augment(t["id"], "New task create the kafka topics in empv3-tenant-intake")


def test_a_genuine_addition_still_folds_in():
    """The point is not to make augment harder to reach."""
    t = store.create_task("fix equals/hashCode", "code", "fix it", None)
    assert "noted for task" in tasks.augment(t["id"], "also add a null test")


def test_the_running_work_is_left_alone_not_cancelled():
    """Without its own branch it fell through to REDIRECT, which cancels the
    running turn. He never said stop — he said start something else."""
    import inspect
    from app import main
    src = inspect.getsource(main)
    branch = src[src.index('if intent == "new_task":'):src.index('if intent == "ambiguous":')]
    assert "_followups" in branch, "queued as its own turn"
    assert "cancel" not in branch, "the running work must not be cancelled"


def test_the_background_task_router_hands_it_back():
    """`_route_to_task` returns False for anything that is not status/redirect/
    augment, which means "not about the task — answer it normally"."""
    import inspect
    from app import main
    src = inspect.getsource(main._route_to_task)
    assert 'if intent != "augment":\n        return False' in src


# --- naming a task that has finished -----------------------------------------

def test_naming_a_finished_task_does_not_hand_the_message_to_a_running_one():
    """`_named_task` only matches ids that are LIVE, so naming a finished task
    read as "named nothing" — and the single live task then claimed the message.

    On 10 September "prepare all list of topics per env and send it to Vinish
    from task 121" was answered as task #117: a different task, a different repo.
    #121 had shipped, so it was invisible to the matcher, and #117 was the only
    thing in flight."""
    from app import main, store
    finished = store.create_task("shipped work", "code", "x", None)
    store.update_task(finished["id"], status="shipped")
    live = store.create_task("something else", "code", "y", None)

    assert main._names_another_task(
        f"send the list from task {finished['id']}", [live["id"]]) is True


def test_naming_the_live_task_is_not_naming_another():
    from app import main, store
    live = store.create_task("running", "code", "y", None)
    assert main._names_another_task(f"task {live['id']} status", [live["id"]]) is False


@pytest.mark.parametrize("text", [
    "bump partition to 4", "use 6 envs not 4", "retention 604800000",
    "also add a null test",
])
def test_a_bare_number_is_not_a_task_reference(text):
    """`_TASK_REF` accepts a bare number — right when steering a live task ("14
    also cover the amend path"), and wrong here: "bump partition to 4" would
    name task #4 and take the message away from the one actually running."""
    from app import main, store
    store.create_task("decoy", "code", "x", None)      # so low ids exist
    live = store.create_task("running", "code", "y", None)
    assert main._names_another_task(text, [live["id"]]) is False


def test_a_hash_reference_still_counts():
    """"look at #121 output" — `\\b` cannot match before "#", both are non-word."""
    from app import main, store
    finished = store.create_task("done thing", "code", "x", None)
    store.update_task(finished["id"], status="done")
    live = store.create_task("running", "code", "y", None)
    assert main._names_another_task(f"look at #{finished['id']} output",
                                    [live["id"]]) is True


def test_the_live_route_is_skipped_when_another_task_is_named():
    import inspect
    from app import main
    src = inspect.getsource(main._dispatch)
    assert "elif len(live) == 1 and not _names_another_task" in src


# --- "as well" is ordinary English, not an instruction to amend ---------------

def test_a_whole_request_ending_as_well_is_not_an_amendment():
    """From WhatsApp, 4:13pm: "Can you update the topic details in the booking
    and ap and raise PR and inform Vinish as well" — its own repos, its own
    recipient, a complete piece of work — was answered "✚ noted for task #117",
    a different task in a different repo, on the strength of its last two words.

    `_ADD` matched the cue ANYWHERE in the message."""
    text = ("Can you update the topic details in the booking and ap and raise PR "
            "and inform Vinish as well")
    assert activity.classify_interjection(text) != "augment"


@pytest.mark.parametrize("text", [
    "also add a test for the null case",
    "and also bump the retention",
    "while you're at it, add the ACL",
    "don't forget to validate the json",
    "one more thing: use partition 4",
    "add a null test as well",
])
def test_a_real_addition_still_folds_in(text):
    """A genuine addition either LEADS with its cue or is a fragment short
    enough to be nothing else. Narrowing this must not break that."""
    assert activity.classify_interjection(text) == "augment"


def test_a_trailing_cue_on_another_full_request():
    assert activity.classify_interjection(
        "Please raise the PR for the booking repo and tell Ravi about it as well"
    ) != "augment"


def test_the_rule_is_position_and_length_not_a_word_list():
    """Pinned so it is not "fixed" back into a bigger regex of phrases."""
    import inspect
    src = inspect.getsource(activity._is_addition)
    assert "m.start() <= _ADD_LEAD" in src and "_ADD_FRAGMENT" in src
