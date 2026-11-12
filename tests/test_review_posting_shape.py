"""What a posted review looks like (12 Nov live test: PR 1481, PR 1538).

A range "File.java:336-339" left "339 (and the…" at the start of the comment, the
same point landed two and three times on one line, and the review body was the
worker's label "VERDICT: Request changes". Synthetic paths and text only.
"""
from __future__ import annotations

from app import review

NOTES = """VERDICT: Request changes
BLOCKING
- `service/a/Helper.java:336-339` (and the zoned twin at :644-648) — only leg[0] is checked
- `service/pom.xml:46` — JWT bumped with no auth regression test run → run the auth suite
TESTS
- `service/pom.xml:46` — no test exercising JWT 1.1.19 against existing auth flows → add an auth test run
- `service/a/Consumer.java:69` — early return is untested → add a unit test
- `service/a/Consumer.java:69` — should invalid acks route to a dead letter queue instead?
"""


def test_a_line_range_attaches_to_its_first_line_and_leaves_no_number_behind():
    first = review.parse_findings(NOTES)[0]
    assert first["line"] == 336 and not first["body"].startswith("339")
    assert first["body"].startswith("(and the zoned twin")


def test_the_same_point_twice_on_a_line_is_one_comment():
    pom = [f for f in review.parse_findings(NOTES) if f["path"].endswith("pom.xml")]
    assert len(pom) == 1 and pom[0]["blocking"]
    assert "\n\n" not in pom[0]["body"], "a near-duplicate is dropped, not joined"


def test_two_different_points_on_a_line_are_joined_into_one_comment():
    rows = [f for f in review.parse_findings(NOTES) if f["path"].endswith("Consumer.java")]
    assert len(rows) == 1
    assert "untested" in rows[0]["body"] and "dead letter" in rows[0]["body"]


def test_the_body_says_the_reason_not_the_label():
    assert review.plain_body("VERDICT: REQUEST CHANGES — the fix ships untested.") == "The fix ships untested."
    assert review.plain_body("VERDICT: Request changes", comments=2) == "Comments inline."
    assert review.plain_body("VERDICT: Request changes") == "Requesting changes."
    assert review.plain_body("VERDICT: APPROVE") == ""
    assert review.plain_body("Looks good — one question inline.") == "Looks good — one question inline."


def test_a_case_that_only_might_happen_is_a_question_not_blocking():
    """12 Nov, PR 1538: "a TRANSIT leg before the facility leg" was posted as
    blocking; the author was right that the flow never produces it."""
    from app import roles
    reviewer = roles.brief("review")
    assert "real input that reaches it" in reviewer and "never blocking" in reviewer
    assert "check the domain and real data" in reviewer
    assert "only might happen is a QUESTION" in review.REVIEW_BRIEF
