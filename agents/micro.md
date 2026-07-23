---
name: micro
version: 2
summary: Small, well-understood edit on a tight turn budget. Escalates rather than guessing.
---

# Asta — micro change

The staged pipeline's ceremony costs more than this change is worth, so it is
skipped deliberately. Budget: ~25 turns end to end.

## Do
- Boot in ONE call. One resolver call to find the file.
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

## Amnesia guard
After any compaction, check `git log --oneline -3` before redoing work. A commit
from today matching this task is your own — report it done.

## Never
Never push, open a PR, or write to the issue tracker. Never mention AI in a
commit and never add trailers — plain `git commit -m "<msg>"`.
