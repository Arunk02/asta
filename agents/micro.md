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
- Anchored read at the cited line, make the edit with Edit, run ONLY the tests
  covering it (coding skill: Verify).
- Report what changed plus the verbatim test result. Nothing else.

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
