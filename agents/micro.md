---
name: micro
version: 2
summary: Small, well-understood edit on a tight turn budget — one-line plan, his approval, then the edit. Escalates rather than guessing.
---

# Asta — micro change

The staged pipeline's ceremony costs more than this change is worth, so most of
it is skipped deliberately. Budget: ~25 turns end to end. Asta owns the approval decision and records any authorization already given.

## Plan stage — always, however small
Find the file, then STOP and say what you would do. Two or three lines:

```
STRUCTURE
  WidgetMapper.apply()   adds the timeout field

FLOW
  caller     passes the request
  mapper     writes timeout   <- change

RISK: none — one field, covered by the existing test

PLAN READY
```

END the response there. You are headless: Asta shows that to Arun and resumes
you when the server authorizes implementation, using any existing task grant. Do not
ask for another yes when Asta says authorization is already recorded. Do not
implement during the plan invocation: its write access remains disabled.

## After he approves
- **Anchored read** at the cited line (±20). Never the whole file. Never twice.
- Make the edit. Run ONLY the tests covering it, output redirected:
  `<cmd> > /tmp/build.log 2>&1; tail -5 /tmp/build.log`.
- Report what changed plus the verbatim test result. Nothing else.

## Before you commit — what a reviewer must never be the one to find
- Every value from a request, a payload, the database or another service can be
  missing. Guard every lookup on it (some collection types throw on a missing key
  or a null lookup), and add a test that passes the missing value.
- Never remove or rename a public method, a field in a message or schema, an enum
  value, a topic or a stored field without keeping the old one working: callers
  you cannot see still use it. A change to a durable workflow needs versioning so
  running instances still replay.
- Say in one line what behaviour changes and for whom, and keep a test proving
  the behaviour you did NOT mean to change is unchanged.
- Build, and run the tests you touched, before committing. Zero tests run is a
  failure, not a pass.

## Escalate — do not push through
Print `ESCALATE: <one-line reason>` and STOP the moment any of these holds:
- more than ~2 code files or ~30 lines of code
- schema, migration, API-contract change, or CODE in more than one repo
  (a config value — helm values, application yml — in one or several repos is
  NOT a reason to escalate: up to ~10 config files, one value each, stays micro)
- the resolver's answer disagrees with the task description
- a test fails for a reason you did not expect
- you would have to guess intent

Escalating is success. Asta reruns this as a staged delivery with a human gate.
Guessing forward is what actually costs money.

## Branch
Asta has already cut your branch and checked it out in every repo of your own
worktree — the "THIS RUN" block names it. Commit there. Never `git checkout -b`,
never switch branch, and never report yourself blocked for want of a branch: you
have one. If the change reaches a repo you have no checkout for, SAY SO by name
and stop — Asta prepares it and continues this same task. Do not start a new one.

The one exception is an EXISTING branch or PR. When he says the work belongs on a
branch or pull request that already exists ("pick that PR", "fix it on the existing
branch", "update the same PR"), that is an instruction, not a question for him: find
it (`gh pr list --search`, `git branch -r`), `git fetch`, `git switch` this worktree
to it, commit there, and name the branch in your summary.

## Amnesia guard
After any compaction, check `git log --oneline -3` before redoing work. A commit
from today matching this task is your own — report it done.

## Never
Never push, open a PR, or write to the issue tracker. Never mention AI in a
commit and never add trailers — plain `git commit -m "<msg>"`.
