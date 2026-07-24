---
name: teams-triage
description: >
  [Teams] Handle people pinging Arun on Teams and summarize calls he missed. Use when
  someone asks Arun a question on Teams and he wants a drafted reply, when he asks "what
  did they want / draft an answer", or when he wants a call/meeting transcript summarized
  ("recap the sync", "what did I miss"). Drafts and summarizes — never sends or speaks as
  Arun without his yes.
---

# Teams triage & call recap

Arun asked: when people ping him, help resolve their queries; when he misses a call,
tell him what matters. This does both — as a drafter, never an impersonator. Asta does
NOT join or speak on live calls; it works from Teams' own recording/recap and from chat.

## When to use

- Someone asked Arun something on Teams and he wants a head start on the reply.
- He wants a call/meeting summarized from its transcript (Teams recording/recap).

## Do not use when

- He's asking to *join or speak on a live call* — Asta can't (consent + tenant policy).
  Offer the recap-after path instead.
- It's a code request → bug-clarification / delegate.

## Answering a person's Teams question

1. **Draft, grounded.** Call `draft_teams_reply(chat)` — it reads the recent thread and
   drafts an answer from Arun's memory + open work. If it lacks the facts, it says what's
   needed rather than guessing.
2. **Let Arun sharpen it.** Show him the draft. If he tweaks it, fold that in.
3. **Send only on his yes.** Stage the final text with `prepare_to_send`
   (channel `teams`, `to` = the person). It posts via `teams_send_message` ONLY after he
   confirms — never in his name unprompted. Honour the 1:1 rule: a person's name = their
   one-to-one chat, never a group.

## Recapping a call he missed

1. **Get the transcript.** From Teams' own recording/recap for that meeting (Teams shows
   its recording banner to everyone — that's the consented source; Asta never silently
   listens to a live call).
2. **Summarize.** Call `meeting_recap(transcript, title)` — it returns TL;DR, decisions,
   action items (his are flagged `ARUN:`), and open questions, and pings him if an item
   is his.
3. **Follow up.** If an action needs a message to someone, draft it and STAGE with
   `prepare_to_send` — confirm before it goes out.

## Pitfalls

- Sending a reply or follow-up without staging it — always `prepare_to_send`.
- Treating a live-call request as doable — it isn't; redirect to recap-after.
- Over-reading the whole chat history — `draft_teams_reply` takes only the recent thread.

## Verification

- Nothing was sent to anyone without Arun's explicit yes.
- The recap flagged his action items; he was pinged if something needed him.
