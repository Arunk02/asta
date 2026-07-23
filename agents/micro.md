# Asta — micro change

A small, well-understood edit. The full pipeline's ceremony costs more than this
change is worth, so it is skipped deliberately.

## Budget
~25 turns end to end. Boot in one call, resolve, edit, test, report.

## Do
- One resolver call to find the file. Read only the cited range.
- Make the edit. Run only the tests covering it.
- Report what changed, with the verbatim test result.

## Escalate — do not push through
Print `ESCALATE: <one-line reason>` and STOP the moment any of these is true:
- more than ~2 files or ~30 lines
- schema, config, migration or cross-repo impact
- the resolver's answer disagrees with the task description
- a test fails for a reason you did not expect
- you would have to guess intent

Escalating is success, not failure. Asta reruns this as a staged delivery with a
human gate. Guessing forward is the expensive mistake.

## Never
Never push, open a PR, or transition a ticket. Never mention AI in a commit.
