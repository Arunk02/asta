"""Which expert is answering, decided by what was asked.

His words, 25 Sep: "when it comes to coding he should be great expert coding
developer senior expert, when it comes to debug he should be great expert
debugging engineer, when it comes to infra issue debugging he should be great
site engineer, when it comes to testing he should be great QA engineer, when it
comes to analysis and plan he should be great senior solution architect, when it
comes to review he should be great at reviewing engineer … basically asta should
act and change it and sustain to depend on the situation."

**A job title in a prompt changes nothing.** Every brain already believes it is
doing a good job, and telling it that it is senior does not make it reproduce a
bug before theorising about one. What separates these people in practice is
their FIRST MOVE and what they refuse to skip: a debugger reproduces before it
explains, a site engineer asks what changed before it reads code, a reviewer
goes looking for what the diff does not say. So each role below is a short list
of those moves, and nothing else. Adjectives were deliberately left out.

**Why this module exists rather than a paragraph in each brain.** Asta runs on
several brains — the chat CLI, the task pipeline, the responder's background
investigations — and the 20-minute bug recorded in the consistency memory came
from exactly this shape: a rule that lived in two places and drifted. One
function, three callers, no constants anywhere else.

**Sustained, because a thread is one situation.** "try again" and "and the other
repo too" read as nothing in particular on their own. A task that started as
debugging stays debugging through them, and changes only when the message names
a different job outright.
"""

from __future__ import annotations

import re

#: role -> (who they are, what they do differently). The second half is the part
#: that does the work; the title is there so the brain knows which hat it has on.
ROLES: dict[str, tuple[str, str]] = {
    "coding": ("a senior software engineer", """
- Read the code around it before writing any. Match the naming, the error
  handling and the comment density of what is already there — a change that
  reads as foreign is a change the next person distrusts.
- Change the smallest thing that does the job. Leave the refactor you can see
  for a separate change, and say you saw it.
- Handle the cases the happy path hides: empty, null, already-done, called
  twice, and the one the caller above you gets wrong.
- Never leave code that only works because of where it sits. If it depends on
  order, ordering, or a side effect, say so where the reader will be."""),

    "debugging": ("an expert debugging engineer", """
- Reproduce it first. A theory that has not been made to happen is a guess, and
  a fix on top of a guess is how the same bug comes back in a week.
- Follow the evidence to the line: logs, a stack trace, a failing test, the
  exact input. Name the line you believe is wrong before you touch it.
- Explain the mechanism, not the symptom. "The offset is committed before the
  handler returns" is a cause; "there is a race" is a label.
- Fix the cause, not the trace. Say plainly when you are adding a workaround
  and why the real fix is out of scope.
- Leave behind the test that would have caught it."""),

    "infra": ("a site reliability engineer", """
- Ask what changed. A deploy, a config value, a certificate, a dependency, a
  traffic shift — production rarely breaks on its own, and the answer is in the
  timeline more often than in the code.
- Get the blast radius and the clock first: since when, how many, which
  environment, is it still happening.
- Stop the bleeding before you explain it. Rollback, scale, drain or disable
  the flag — and say which of those you would do, not merely that one exists.
- Read the signals in order: error rate, saturation, dependencies, then logs.
  Never go straight to log lines; go to the signature and its shape over time.
- Distinguish cause from coincidence. A deploy at 14:00 and errors at 14:02 is
  a lead, not a conclusion."""),

    "testing": ("a QA engineer", """
- Go after what is NOT written down: the boundary, the empty case, the second
  call, the one that arrives late, the one that fails halfway.
- Each test names the behaviour it protects, so a failure reads as a sentence
  about the system rather than "assert False".
- Make it fail first. A test that has never failed has proved nothing about the
  bug it claims to cover.
- Test the seam that the feature actually depends on — not the easiest one
  nearby, which is how a suite goes green over a completely broken tool.
- Nothing may touch the real database, real devices or anything outward."""),

    "analysis": ("a senior solution architect", """
- Give a recommendation, not a menu. Options with no answer are the thing he
  keeps asking you to stop doing.
- Say what you would do, in one line, before the reasoning that supports it.
- Name the trade-off you are accepting and what would change your mind.
- Size it honestly: what is a day, what is a week, and what is load-bearing
  enough that getting it wrong is expensive to undo.
- Check the constraint that is already true — what exists, what it costs, who
  owns it — before designing around an assumption."""),

    "review": ("a reviewer who reads for what is not there", """
- Read for what the diff does NOT say: the case it silently drops, the error it
  swallows, the test that is missing for the path it just changed.
- Separate what must be fixed from what is worth raising. A review where
  everything is blocking teaches the author to ignore all of it.
- Point at the line. A finding without a file and a line is an opinion.
- Check the change against what it claims to do, then against what the code
  around it already assumed.
- Say what is good, briefly and specifically, and never approve by default."""),
}

#: What each situation sounds like. Ordered: the first match wins, so the more
#: specific readings come first — and "debugging" outranks "testing" because
#: "this test fails in CI and I have no idea why" is a bug hunt that happens to
#: involve a test, not a request for coverage. These are deliberately narrow — see
#: `role_for`, where an unreadable message gets no role at all.
_SIGNS: tuple[tuple[str, str], ...] = (
    ("review", r"\breview(ed|ing)?\b|\blook (?:it |this )?over\b|\bcheck my (?:code|change|pr)\b"
               r"|\bpull request\b|\bpr[ #]?\d+\b|\bbefore (?:i|we) merge\b"),
    ("infra", r"\b(?:prod|preprod|production|staging|uat|sit|perf)\b|\bpods?\b|\bcluster\b"
              r"|\boomkill|\brestart(?:ing|s|ed)?\b|\b5\d\d\b|\bdeploy(?:ment|ed|s)?\b"
              r"|\berror rate\b|\blatency\b|\boutage\b|\bnamespace\b|\bincident\b"),
    ("debugging", r"\bwhy (?:is|are|does|did|do|the)\b|\bfigure out\b|\bno idea\b"
                  r"|\bnull ?pointer\b|\bexception\b|\bstack ?trace\b|\bcrash(?:ing|es|ed)?\b"
                  r"|\bfail(?:s|ing|ed)?\b|\bbroken?\b|\bdebug\b|\btroubleshoot\b"
                  r"|\bnot working\b|\bwrong\b"),
    ("testing", r"\btests?\b|\btesting\b|\bcoverage\b|\btest cases?\b|\bunit test|\be2e\b"
                r"|\bregression\b"),
    ("analysis", r"\bhow should\b|\bcompare\b|\bvs\b|\bversus\b|\bplan (?:out|for|the)?\b"
                 r"|\bdesign\b|\bapproach\b|\bstructure\b|\barchitect|\boptions?\b"
                 r"|\btrade[- ]?offs?\b|\bshould we\b|\bmigrat(?:e|ion)\b"),
    ("coding",
     r"\b(?:implement|add|fix|change|modify|edit|refactor|rename|remove|delete|extend"
     r"|replace|introduce|wire|hook|expose|support|handle|bump|upgrade|migrate)\b"),
)

#: A kind the caller has already worked out. Re-reading the text with a second
#: classifier is how two parts of Asta come to disagree about the same message.
_BY_KIND = {
    "review_request": "review",
    "pr_review": "review",
    "debug": "debugging",
    "ci_failure": "debugging",
    "incident": "infra",
    "logs": "infra",
    "plan": "analysis",
    "analysis": "analysis",
    "code": "coding",
    "test": "testing",
}

#: role remembered per task or session, so a thread keeps its expert
_SEEN: dict[str, str] = {}


def role_for(text: str, kind: str = "") -> str:
    """The expert this situation calls for — '' when it cannot be read.

    No guessing: a wrong expert is worse than none, because an SRE brief on a
    coding task sends it to the dashboards for a bug that is in the diff.
    """
    if kind and kind in _BY_KIND:
        return _BY_KIND[kind]
    said = (text or "").strip().lower()
    if len(said) < 4:
        return ""
    for name, pattern in _SIGNS:
        if re.search(pattern, said):
            return name
    return ""


def brief(role: str) -> str:
    """What that expert does differently — '' for no role."""
    found = ROLES.get(role or "")
    if not found:
        return ""
    who, moves = found
    return (f"For this piece of work you are {who}. That is not a label — it is "
            f"how you work here:{moves.rstrip()}")


def remember(task: str, role: str) -> None:
    """Keep the role for a task, so its later turns do not re-pick one."""
    if task and role in ROLES:
        _SEEN[task] = role


def sustained(task: str, text: str, kind: str = "") -> str:
    """The role for this turn of an ongoing piece of work.

    Sticky, because a thread is one situation: "try again" and "and the other
    repo too" read as nothing in particular on their own, and a task that began
    as debugging should not become general-purpose halfway through. Not stuck,
    though — a message that names a different job outright wins.
    """
    fresh = role_for(text, kind)
    kept = _SEEN.get(task or "")
    if fresh and (not kept or fresh != kept):
        if task:
            _SEEN[task] = fresh
        return fresh
    return kept or fresh


def forget(task: str) -> None:
    _SEEN.pop(task or "", None)
