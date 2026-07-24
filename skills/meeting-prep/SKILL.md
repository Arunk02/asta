---
name: meeting-prep
description: >
  [Meetings] Prepare Arun for a meeting or 1:1 and help run the follow-up. Use when
  he asks to prep for a meeting, a 1:1, a sync, a review, or a standup, says "get me
  ready for the 3pm", "what should I raise with X", "draft my catch-up notes", or when
  a pre-meeting heads-up needs a draft. Drafts talking points from his calendar + open
  work, and stages any message to attendees for his confirmation.
---

# Meeting & 1:1 prep

Arrive with a draft, not a blank page. Asta already pings ~30 min before a speaking
meeting; this is the on-demand version and the follow-up. Anything sent to another
person is STAGED with `prepare_to_send` and goes out only on Arun's yes.

## When to use

- Arun asks to prep for a meeting, 1:1, sync, review, or standup.
- A pre-meeting heads-up fired and he wants the draft fleshed out.
- After a meeting, he wants follow-up notes or actions sent.

## Do not use when

- He just wants to know what's on his calendar → `outlook_meetings`.
- It's a code task disguised as a "meeting" → the bug-clarification / delegate path.

## Procedure

1. **Draft the prep.** Call `meeting_prep` (empty for the next speaking meeting, or a
   title to pick one). It returns talking points, questions to ask, and watch-outs,
   grounded in his open Jira work — local-model-first, so it's cheap.
2. **Sharpen it for a 1:1.** For a one-on-one, pull the recent thread with that person
   (`teams_read_chat`) only if it adds something — recent asks, an unresolved point.
   Keep it to what he'd actually raise.
3. **Refine with him, don't guess.** If the meeting's purpose or the ask is unclear,
   `ask_user` one question rather than inventing an agenda.
4. **Drive, don't idle.** Between steps use `continue_working` so the prep is ready
   without waiting on him.
5. **Follow-up.** After the meeting, if he wants notes or actions sent to an attendee,
   draft them and STAGE with `prepare_to_send` (channel `teams` or `email`) — never
   send directly.

## Pitfalls

- Sending prep or notes to anyone without staging — always `prepare_to_send`.
- Over-pulling context (full Teams history, every email) — that's token waste; take
  only the recent, relevant thread.
- Inventing agenda items for a meeting whose purpose is unclear — ask instead.

## Verification

- The draft names concrete talking points tied to his real open work, not filler.
- Nothing was sent to an attendee without a confirmation.
