"""WorkWorld — Asta's own work, simulated, so a change can be proven to help.

The gap this closes is the whole of P1 in the Astra-class plan. On 11 September
Asta had 2,704 unit tests passing and a scenario bench scoring 81 of 81, while
in production a plan was announced as done, a finished task swallowed every new
message, the same logs were fetched three times and eight analyses died on a
usage limit. Everything green, nothing working: the tests measured PARTS, and
every failure lived in the space between them.

So this runs the real Asta — the real dispatcher, the real task engine, the real
responder, the real notification path — over a FAKE world:

    world.py      the world and the sandbox: an isolated database, recorders in
                  place of every outward act, scripted brains, fake tools
    scenario.py   a scenario as data (setup → steps → checks) plus the checks
                  themselves, including the constitution every scenario obeys
    twin.py       Arun, simulated: his phrasing, from his own messages, so a
                  scenario is not only passed in the words a developer chose
    day.py        one simulated working day, ~150 events, scored on interrupts
    runner.py     sets, pass^k, the baseline written where the scorecard reads it
    nightly.py    when the live tier may spend a brain, and how much

Three rules hold the thing honest:

  NOTHING REAL HAPPENS. The database is a temp file, every send is a recorder,
  and the guard fails a scenario that reaches for a real one. A test suite that
  can message a colleague is not a test suite.

  THE CODE UNDER TEST IS THE REAL CODE. Only the edges are doubles. A scenario
  that passes against a mock of `tasks` proves the mock works.

  A FAILING SCENARIO IS THE POINT. Scenarios are written from incidents Arun
  actually hit, so at the baseline many of them fail by construction. Each one
  that does carries `gap:` naming the phase that will close it — a to-do list
  with an exit criterion, not a red build.
"""

# Deliberately no imports here: `python -m app.workworld` loads `__main__`,
# which imports `runner`, which imports the rest. Re-exporting them from the
# package would make that a circular import.
