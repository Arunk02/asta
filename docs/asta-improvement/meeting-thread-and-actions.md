# Meeting prep from the thread, and recorded action items

**Prep reads the thread.** `meeting_prep` adds the meeting's recent Teams messages to the invite and his open Jira items.

- It reads the meeting's own chat, named after the meeting, and falls back to the organiser's 1:1.
- Messages come from the last seven days, at most twelve.
- They are read from the messages Asta already stored, so prep opens nothing and sends nothing.
- The prompt labels them as material written by others, not instructions.
- `copilot_cli._teams_with` is the one function that reads a thread. It gained `days` and `lead` options; the reply path's output is unchanged.

**Recap action items are recorded** (`app/meeting_actions.py`). `meeting_recap` records each bullet under **Action items** with:

- its meeting
- its owner: the prefix before a colon, up to four words; "ARUN" means his; anything else is `unassigned`
- its status

Re-running the recap is safe:

- The same transcript, keyed by its hash, records nothing twice.
- His items reach him in one message and are marked as told. Re-running a recap used to ping him again; now it doesn't.
- A colleague's item is recorded and never chased. Reminding them would be speaking for him.

`meeting_actions(owner)` lists open items, where "me" means his own. `meeting_action_done(id)` closes one. Both are also `GET /api/meeting-actions` and `POST /api/meeting-actions/{id}/done`. The store keeps 200 rows, dropping the oldest finished items first; an open item is never dropped.

This closes the benchmark cases `call-prep-brief` and `call-follow-ups-recorded`.
