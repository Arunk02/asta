# "Delivery not confirmed" is settled from the chat

Automatic replies (#174) and a task's update to a named person (#179) record an UNCERTAIN receipt when a send times out, crashes or cannot be confirmed. Nothing resends them, because a duplicate in a colleague's chat is worse than a late message. Until now nothing looked again either.

`reconcile.loop` (every 5 minutes) reads the Teams history Asta already stores:

- **Delivered:** the exact words are in that chat, from his account, after the send began. The receipt becomes `sent`, silently.
- **Not delivered:** a stored read of the chat covers the minutes after the send (`store.teams_history_covers`) and the words are not there. He is told once and nothing is resent; resending stays his decision.
- **Unknown:** history does not cover it yet. It waits for the next sweep, and after a day he is told once that it can't be verified.

A `sending` receipt older than ten minutes is a send a crash interrupted, and is checked the same way. Task update receipts now keep the exact text and the send start, so they can be matched.

Limits: matching is on exact words, after whitespace normalisation. A message Teams reformatted (links, mentions) reads as not found until the chat is read again and the day-long timeout says it is unverifiable. Only Teams sends are reconciled; GitHub approvals and merges remain reconciled by read-only PR polling (#173/#178).
