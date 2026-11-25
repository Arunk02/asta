# Durable workflows (Temporal) — changes that must not break running instances

- Workflows are replayed from history. Any change to the commands a workflow
  issues (activities, timers, child workflows, signals handled) breaks replay of
  instances already running, unless it is versioned.
- Version every such change with `Workflow.getVersion("<change-id>", DEFAULT_VERSION, 1)`
  and keep the old branch for `DEFAULT_VERSION`. Name the change id after the ticket.
- No versioning in place yet, and replay still must not break: gate new logic on
  a value an ACTIVITY computes (activities are recorded, so replay sees the same
  value), not on code-side conditions that change with the deploy.
- Before `continueAsNew`, drain pending signals, or they are lost.
- Add a replay or workflow test covering the old and the new path.
