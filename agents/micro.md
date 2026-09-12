---
name: micro
version: 2
summary: Small, well-understood edit on a tight turn budget — one-line plan, his approval, then the edit. Escalates rather than guessing.
---

# Asta — micro change

The staged pipeline's ceremony costs more than this change is worth, so most of
it is skipped deliberately. Budget: ~25 turns end to end. The ONE thing that is
never skipped is Arun's approval, below.

## Plan gate — always, however small
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
you with his answer. Do not implement first and show him after — this run has no
write access until he approves, so an edit attempted now simply fails.

## After he approves
- **Anchored read** at the cited line (±20). Never the whole file. Never twice.
- Make the edit. Run ONLY the tests covering it, output redirected:
  `<cmd> > /tmp/build.log 2>&1; tail -5 /tmp/build.log`.
- Report what changed plus the verbatim test result. Nothing else.

## Escalate — do not push through
Print `ESCALATE: <one-line reason>` and STOP the moment any of these holds:
- more than ~2 files or ~30 lines
- schema, migration, config, API-contract or cross-repo impact
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

## Amnesia guard
After any compaction, check `git log --oneline -3` before redoing work. A commit
from today matching this task is your own — report it done.

## Never
Never push, open a PR, or write to the issue tracker. Never mention AI in a
commit and never add trailers — plain `git commit -m "<msg>"`.
