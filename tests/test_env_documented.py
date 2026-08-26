"""Every setting the code reads must be written down where Arun looks for it.

This exists because the honest answer to "did you update .env?" was no, four
times running. ASTA_VERIFY, ASTA_RELEVANCE, ASTA_DEV_MCP and ASTA_TASK_SPEC all
shipped working and none of them appeared in .env.example — so the only way to
discover a feature existed was to read the source for `os.environ.get`. A flag
nobody can find is a flag that is off for ever, whatever its default says.

Asking people to remember is what produced that, so this does not ask. It reads
the settings the code actually consults and fails when one of them is missing
from .env.example, which makes forgetting a red test rather than a silent
omission discovered months later.

.env itself is deliberately NOT checked: it holds real tokens, it is gitignored,
and CI has no copy of it. .env.example is the documentation, and it is the thing
that must stay complete.
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
APP = ROOT / "app"
EXAMPLE = ROOT / ".env.example"

#: Read from the environment but genuinely not user configuration. Each one needs
#: a reason, so the list cannot quietly become a dumping ground for "too hard".
EXEMPT = {
    "ASTA_TOKEN": "the auth secret itself — documented in the setup section, never a default",
}

#: Families whose full names are BUILT at runtime and so can never be seen by a
#: regex over the source. `agent.effort_for` composes ASTA_EFFORT_<MODEL>_<STAGE>
#: from whichever model is in play, and the point of that cascade is that any
#: model works without bespoke wiring — enumerating them here would defeat it.
#: Documented examples of these are real even though no literal matches.
DYNAMIC_PREFIXES = ("ASTA_EFFORT_",)

_READ = re.compile(r"""environ(?:\.get\(|\[)\s*["'](ASTA_[A-Z0-9_]+)["']""")

#: Flag names the brain spec table carries as DATA — `"tier_env": "ASTA_..."`,
#: later read through `os.environ.get(env_var)`. The code reads these as surely
#: as a literal call does; the name simply sits one indirection away, where a
#: regex over call sites cannot see it. Deliberately narrow (a key ending in
#: `_env`) rather than "any ASTA_ string anywhere", which would let a flag
#: mentioned only in a comment pass as implemented.
_DECLARED = re.compile(r"""["']\w*_env["']\s*:\s*\(?\s*["'](ASTA_[A-Z0-9_]+)["']""")


def _flags_in_code() -> set[str]:
    found: set[str] = set()
    for path in APP.rglob("*.py"):
        text = path.read_text(encoding="utf-8", errors="replace")
        found |= set(_READ.findall(text)) | set(_DECLARED.findall(text))
    return found


def _flags_documented() -> set[str]:
    text = EXAMPLE.read_text(encoding="utf-8", errors="replace")
    return set(re.findall(r"^\s*#?\s*(ASTA_[A-Z0-9_]+)\s*=", text, re.M))


def test_every_setting_the_code_reads_is_documented():
    missing = _flags_in_code() - _flags_documented() - set(EXEMPT)
    assert not missing, (
        "These are read by app/ but missing from .env.example:\n  "
        + "\n  ".join(sorted(missing))
        + "\n\nAdd each one with a line saying what it does and what the default "
          "means. A setting nobody can find is a setting that stays off for ever."
    )


def test_the_example_does_not_advertise_settings_that_do_not_exist():
    """The other direction. A documented flag the code never reads is worse than
    an undocumented one: it reads as a working switch, and flipping it does
    nothing at all."""
    stale = {f for f in _flags_documented() - _flags_in_code() - set(EXEMPT)
             if not f.startswith(DYNAMIC_PREFIXES)}
    assert not stale, (
        "Documented in .env.example but read nowhere in app/:\n  "
        + "\n  ".join(sorted(stale))
        + "\n\nEither the code stopped using it, or it is a typo. Both mislead."
    )


def test_every_exemption_carries_its_reason():
    assert all(reason.strip() for reason in EXEMPT.values())


def test_this_sessions_flags_are_all_present():
    """A named check for the six built today, so a bad regex above cannot make
    the general test vacuously pass."""
    documented = _flags_documented()
    for flag in ("ASTA_ATTENTION", "ASTA_CONTACTS", "ASTA_DELIVERY", "ASTA_MEET2",
                 "ASTA_QUIET_HOURS", "ASTA_STALE_AFTER_MINUTES", "ASTA_EOD_HOUR",
                 "ASTA_URGENT_HOURS", "ASTA_CHASE_AT", "ASTA_COALESCE_SECONDS"):
        assert flag in documented, f"{flag} is missing from .env.example"


# --- the suite must not depend on what time it is ---------------------------

def test_the_wall_clock_cannot_change_a_test_result(monkeypatch):
    """Found at 23:42: ASTA_QUIET_HOURS is set in the real .env, tests load it,
    and flush_held correctly refuses to push during quiet hours — so three
    hold-window tests passed all day and failed at night with an IndexError that
    said nothing about the clock. conftest clears it for every test; this pins
    that it is actually gone rather than merely intended to be."""
    import os
    from tests.conftest import _TIME_DEPENDENT_ENV
    for name in _TIME_DEPENDENT_ENV:
        assert os.environ.get(name) is None, (
            f"{name} leaked into a test — its result now depends on the time of day")


def test_quiet_hours_is_still_settable_by_a_test_that_means_to(monkeypatch):
    """Clearing it globally must not make the feature untestable."""
    from app import delivery
    monkeypatch.setenv("ASTA_DELIVERY", "1")
    monkeypatch.setenv("ASTA_QUIET_HOURS", "22:00-07:00")
    assert delivery.quiet_window() == (22 * 60, 7 * 60)
