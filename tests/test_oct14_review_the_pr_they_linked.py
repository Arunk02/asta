"""14 Oct: Vinish — "please review <booking PR 1483 link>".

The review ran as a 15-minute investigation and timed out. Its retry was titled
"Review Vinish Kumar's PR #1252": the reader's summary ("share the promised update
(PR 1252) and review PR 1483") came first in the text, and its first number won.
"""

from app import responder, store, tasks

LINK = "https://github.com/Maersk-Global/telikos-booking-service/pull/1483/changes"
SUMMARY = ("So far: Vinish asked Arun to fix PR 1252; Arun fixed and merged it.\n"
           "Share the promised update (PR 1252) and review PR 1483\n"
           f"Vinish Kumar: Also can you please review {LINK}")


def test_the_pr_they_linked_wins_over_a_number_in_the_summary():
    assert responder.pr_number(SUMMARY) == "1483"
    assert responder.title_for("review_request", "Vinish Kumar", SUMMARY) == "Review Vinish Kumar's PR #1483"
    assert "pull request #1483" in responder.brief_for("review_request", "Vinish Kumar", SUMMARY)


def test_the_newest_link_wins_when_there_are_several():
    text = ("Arun: https://github.com/o/ap/pull/1252\n"
            "Vinish: please review https://github.com/o/booking/pull/1483")
    assert responder.pr_number(text) == "1483"


def test_a_bare_number_still_works_without_a_link():
    assert responder.pr_number("please review PR 1483") == "1483"
    assert responder.pr_number("nothing here") == ""


def _task(kind_tag):
    t = store.create_task("Review Vinish Kumar's PR #1483", "analysis", "x", None)
    if kind_tag:
        store.kv_set(f"responder_task:{t['id']}", f"teams|Vinish Kumar|{kind_tag}|why")
    return t["id"], store.get_task(t["id"])


def test_a_review_gets_a_review_sized_timeout():
    tid, t = _task("review_request")
    assert tasks.timeout_for(tid, t) == tasks.REVIEW_TIMEOUT > tasks.TASK_TIMEOUT["analysis"]
    tid, t = _task("pr_review")
    assert tasks.timeout_for(tid, t) == tasks.REVIEW_TIMEOUT


def test_an_investigation_keeps_its_own_timeout():
    tid, t = _task("debug")
    assert tasks.timeout_for(tid, t) == tasks.TASK_TIMEOUT["analysis"]
    tid, t = _task("")
    assert tasks.timeout_for(tid, t) == tasks.TASK_TIMEOUT["analysis"]
