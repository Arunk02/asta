"""The agent core — P2 of the Astra-class plan.

Until this, a code task was one long coroutine (`tasks._worker`) that decided
which stage a brain had reached by string-matching the last lines it printed,
and kept its state in eighteen kv keys and fifteen statuses. Every incident this
summer that was about a task — a plan announced as done, a finished task that
kept claiming messages, work stranded by a restart — lived in that design.

Here a job is a LangGraph graph with an explicit shape and a checkpoint after
every step:

    outcome.py    how a leg SAYS what state it left the work in (a tool call,
                  not a sentinel), and the inference used when a brain cannot
    notes.py      job notes that outlive a context window or a brain switch
    code_graph.py the code-change graph: plan → gate → implement → verify ⇄ fix
                  → complete, each gate an `interrupt()`
    runner.py     start, answer a gate, carry on after a pause or restart

The old engine stays. `ASTA_GRAPH=1` sends new code tasks here; a task started on
one engine finishes on it, so switching the flag never strands anything.
"""
