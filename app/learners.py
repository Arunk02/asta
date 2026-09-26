"""Four bounded learners — Round 3, P12.

The ledger records what he decided; these turn it into one bounded change each.

**They do not promote anything.** `evolve` (P6) already proves a candidate
against the version running now, promotes only on a measured gain, checks the
constitution and can roll back. A learner here produces evidence and a proposal
and hands it to that fence. A learner that pinned its own setting would be a way
around the fence rather than a user of it, which is why the tests assert this
module never calls `settings.pin` or `promote`.

What each learner refuses to do matters more than what it does:

  NOTHING MOVES ON THIN EVIDENCE. Every one has a minimum, and below it proposes
  nothing. A learner acting on five judgements is a random number generator with
  a changelog.

  NOTHING MOVES FAR. One step per run, inside the knob's own bounds. The worst a
  wrong learner can do is be slightly wrong, once, reversibly.

  NOTHING LEARNS FROM SILENCE. "ignored" means he never looked. That is evidence
  about the interruption, not about the words — see `ledger`.

The four:

  1  CALIBRATION   does 0.9 mean 0.9? Every gate downstream reads that number,
                   so a claim that overstates itself is the most load-bearing
                   thing on this list.
  2  SEND BAR      the confidence a draft must clear to go without asking,
                   earned from how often his answer was "as is".
  3  DEMOTION      how readily a feed he never looks at moves to the digest.
  4  DRAFTING      what he keeps rewriting by hand — a habit he repeats is a
                   rule Asta could have followed before he had to. This one is
                   TEXT, not a number, so it is a rule for the drafter rather
                   than a knob for evolve.
"""

from __future__ import annotations

#: Judgements needed before a learner will say anything at all.
LEAST = 10
#: Times he has to make the same edit before it counts as a habit.
HABIT = 5
#: How far from reality a stated confidence may sit before it is corrected.
SLACK = 0.1


def propose() -> list:
    """Bounded candidates for `evolve` to prove. [] when the evidence is thin."""
    from . import evolve, ledger, settings
    out = []

    # 1 · calibration. The highest bucket with enough evidence decides: that is
    # the one every send gate is reading.
    for bucket in sorted(ledger.calibration(), key=lambda b: -b["claimed"]):
        if bucket["n"] < LEAST or not bucket["claimed"]:
            continue
        gap = bucket["actual"] - bucket["claimed"]
        if abs(gap) <= SLACK:
            break                      # it means what it says; leave it alone
        now = settings.value("ASTA_CONFIDENCE_SHIFT")
        step = 0.05 if gap > 0 else -0.05
        out.append(evolve.Candidate(
            "L1", "ASTA_CONFIDENCE_SHIFT", round(now + step, 2),
            "confidence does not mean what it says",
            f"claimed {bucket['claimed']:g}, actually taken as is "
            f"{bucket['actual'] * 100:.0f}% of {bucket['n']}"))
        break

    # 2 · the bar for sending unasked, from his own answers to sends.
    sends = ledger.rate("send", least=LEAST)
    if sends is not None:
        now = settings.value("ASTA_SEND_MIN_CONFIDENCE")
        if sends < 0.8:
            out.append(evolve.Candidate(
                "L1", "ASTA_SEND_MIN_CONFIDENCE", round(min(1.0, now + 0.02), 2),
                "drafts are not going out as written",
                f"only {sends * 100:.0f}% of sends were taken as is — ask more often"))
        elif sends > 0.95:
            out.append(evolve.Candidate(
                "L1", "ASTA_SEND_MIN_CONFIDENCE", round(max(0.9, now - 0.01), 2),
                "drafts go out as written",
                f"{sends * 100:.0f}% of sends were taken as is"))

    # 3 · demotion, from pushes he never looked at. `rate` deliberately ignores
    # "ignored", so this asks the ledger directly — here it IS the signal.
    pushes = [r for r in ledger.recent(300, "push")]
    if len(pushes) >= LEAST:
        ignored = sum(1 for r in pushes if r["verdict"] == "ignored") / len(pushes)
        if ignored > 0.8:
            now = settings.value("ASTA_ATTENTION_IGNORE_SHARE")
            out.append(evolve.Candidate(
                "L1", "ASTA_ATTENTION_IGNORE_SHARE", round(max(0.6, now - 0.05), 2),
                "most of what it pushes is ignored",
                f"{ignored * 100:.0f}% of {len(pushes)} pushes were never answered"))

    seen: set[str] = set()
    kept = []
    for c in out:
        if c.knob in seen or not settings.within_bounds(c.knob, c.after) or c.after == c.before():
            continue
        seen.add(c.knob)
        kept.append(evolve.Candidate(c.level, c.knob, c.after, c.cluster, c.why,
                                     was=settings.value(c.knob)))
    return kept


def drafting_rules() -> list[str]:
    """4 · What he keeps rewriting, as rules for whatever drafts next.

    Text rather than a number, because "he cuts the greeting" is not a
    threshold. These ride in the drafting prompt: a habit he has repeated five
    times is a rule Asta could have followed before he had to.
    """
    from . import ledger
    said = []
    for shape, n in ledger.edits("send").items():
        if n < HABIT:
            continue
        if shape == "greeting removed":
            said.append(f"Do not open with a greeting — he has cut it {n} times.")
        elif shape == "shorter":
            said.append(f"Write it shorter than feels natural — he has cut it back {n} times.")
        elif shape == "longer":
            said.append(f"Say more than the bare answer — he has added to it {n} times.")
        elif shape == "question dropped":
            said.append(f"Do not end with a question — he has removed one {n} times.")
    return said


def summary() -> str:
    """One line for the morning, or '' when there is nothing learned yet."""
    from . import ledger
    bits = []
    for bucket in ledger.calibration():
        if bucket["n"] >= LEAST:
            bits.append(f"claimed {bucket['claimed']:g} → {bucket['actual'] * 100:.0f}% as is "
                        f"({bucket['n']})")
    rules = drafting_rules()
    if rules:
        bits.append(f"{len(rules)} drafting rule(s) learned")
    return " · ".join(bits)
