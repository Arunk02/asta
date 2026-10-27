# He approves the plan he read, and the worker builds against all of it

**Full plan before approval.** The plan card on his phone is a cut: about 1,500 characters, with structure and flow pinned and the rest marked "(the rest is in the app)". A long plan therefore asked him to approve steps he never saw. When the card is a cut, the full plan is now written to a readable `plan-<task>.md` and attached to the WhatsApp message, on both the approval path and the "going ahead" path. When he is at the laptop, the phone stays quiet and the card gives the file path instead.

Each revision is its own content-addressed file, so a revised plan never overwrites the one he read. Approved revisions and amendments are still kept by `task_spec.capture` (#170).

**Proof the worker read it all.** A plan longer than 3,000 characters reaches the implementation leg as a pointer to `plan.md`. That file now ends with a `Plan receipt: PLAN-<hash>` line that appears nowhere in the prompt, and the pointer asks the worker to quote it. If the report does not quote it, the task records it and the completion message says the change is "not confirmed against the full approved plan". Shorter plans sit in the prompt and need no receipt.

**Required parts named on the card.** The planner brief already asked for STRUCTURE (what changes), FLOW (the current flow with the change marked, as a simple diagram), checked steps, RISK, ASSUMPTIONS and CONTEXT USED. It now also asks for `ROLLBACK:` (how to undo it) and `SCOPE:` (PR, merge and who is told, exactly as he asked, or local only). The approval card names any part a plan left out ("⚠ This plan does not state: ROLLBACK, SCOPE"), so he never approves a plan blind to a missing part.

Limits: the receipt shows the file was read to the end, not that every step was implemented. Coverage of the steps is still for review and CI. Missing parts are named, not blocked: a plan without them can still be approved.
