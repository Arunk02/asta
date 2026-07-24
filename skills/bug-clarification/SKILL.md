---
name: bug-clarification
description: >
  [Bug workflow] Turn a reported bug or ticket into a clarified, delegated fix —
  the clarify-before-you-build loop. Use when Arun points at a bug, a defect, a
  failing behaviour, a Jira/GitHub issue, or says "fix", "why is X broken",
  "investigate this ticket". Comments on the ticket, sorts the requirements 1:1
  with Arun (never guesses missing acceptance criteria), then runs the mission.
---

# Bug → clarified → fixed

The expensive failure mode is building the wrong fix confidently. This playbook
spends a little to clarify up front so the mission runs once, not three times.
Every outward message (a ticket comment, a reply) is STAGED with `prepare_to_send`
and only goes out on Arun's yes — never posted directly.

## When to use

- Arun reports a bug, a defect, or a failing behaviour, or hands you a ticket to fix.
- A mission came back needing a decision only Arun can make.

## Do not use when

- It's a brand-new feature with clear acceptance criteria (delegate directly).
- It's a question about existing state ("is X implemented?") — just answer it.

## Procedure

1. **Understand cheaply.** `resolve_context` FIRST with the bug's nouns, then open
   only the returned matches at `source:line`. Never read files blind (BLIND_READ
   waste). For a live production symptom, use the grafana-analyser skill to pin the
   failing signal before touching code.

2. **Acknowledge on the ticket.** Draft a short comment — what you understand the
   bug to be and the area it lives in — and STAGE it with `prepare_to_send`
   (channel `jira`). It posts only after Arun confirms.

3. **Sort the requirements 1:1 — before building.** List every unknown the ticket
   leaves open (repro steps, expected vs actual, scope, which flow). For each, ask
   Arun with `ask_user` — one question per unknown. Do NOT guess an acceptance
   criterion; a wrong assumption is the whole cost this skill exists to avoid. His
   answers become the mission's ACs.

4. **Drive it, don't idle.** Between steps, when you already know the next move,
   call `continue_working` so Asta runs it without waiting for Arun. Stop only to
   ask (step 3) or to stage a send (steps 2, 6).

5. **Delegate the fix.** `delegate_task` (kind `code`, workspace set) with the
   clarified spec + the confirmed ACs in the prompt. A Jira-key ticket runs the
   full staged pipeline (plan gate → Arun approves → implement); a small ad-hoc bug
   runs the micro pipeline. Relay Arun's gate answers: "approve task N" →
   `approve_task`; other feedback → the task's reply endpoint. Never plan the code
   change yourself here.

6. **Close the loop.** When the mission lands, draft the ticket's closing comment
   (root cause, the fix, test coverage, PR link) and STAGE it with
   `prepare_to_send`. Confirmed → it posts.

## Pitfalls

- Posting a ticket comment directly instead of staging it — outward messages always
  go through `prepare_to_send`.
- Delegating before clarifying — you inherit the ticket's ambiguity and re-plan mid-run
  (the priciest avoidable event).
- Reading files whole to "get oriented" — resolve first; it's ~350 tokens vs a class.

## Verification

- Every unknown was either answered by Arun or explicitly noted as an assumption.
- No comment was posted without a confirmation.
- The delegated task carried the confirmed ACs, so it ran without a re-plan cycle.
