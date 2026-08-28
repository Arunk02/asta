"""In a `_click_first` list, ordering IS behaviour.

Found by probing a live call. The Teams call screen carries a bare
`button[aria-label="More"]` belonging to the APP BAR — it sits beside "View more
apps" — as well as the call toolbar's own `callingButtons-showMoreBtn`. The
generic one was listed first, so every attempt to open the call's More menu
clicked the app bar, found no "Language and speech" inside it, and
`start_captions` returned False.

The visible symptom was not an error. Asta placed the call, connected, spoke
thirteen seconds of real audio into it, and heard nothing at all — twice, in
front of a colleague, with no failure anywhere in the logs.
"""

from __future__ import annotations

import re

from app import meetings


def _specificity(selector: str) -> int:
    """Lower is more specific. A data-tid names one element; an aria-label may
    name several, and a bare word like "More" almost certainly does."""
    if "data-tid" in selector:
        return 0
    if "*=" in selector:                       # aria-label*="More actions"
        return 1
    return 2                                   # aria-label="More" — bare, generic


def test_the_call_more_menu_prefers_the_call_toolbar_button():
    ranks = [_specificity(s) for s in meetings._MORE_MENU]
    assert ranks == sorted(ranks), (
        "generic selector before a specific one in _MORE_MENU: "
        f"{meetings._MORE_MENU}. _click_first takes the FIRST match, so a bare "
        f"aria-label=\"More\" will shadow the call toolbar's own button.")
    assert "callingButtons-showMoreBtn" in meetings._MORE_MENU[0], (
        "the call toolbar's own More button must be tried first")


def test_every_click_first_list_goes_specific_to_generic():
    """The same trap in every other selector list in the module."""
    offenders = []
    for name in dir(meetings):
        if not name.isupper():
            continue
        value = getattr(meetings, name)
        if not isinstance(value, list) or not value:
            continue
        if not all(isinstance(v, str) and ("[" in v or "." in v) for v in value):
            continue
        ranks = [_specificity(v) for v in value]
        if ranks != sorted(ranks):
            offenders.append(f"{name}={value}")
    assert not offenders, (
        "selector lists ordered generic-first — _click_first will take the "
        "generic match and never reach the specific one:\n  " + "\n  ".join(offenders))


def test_a_bare_aria_more_is_recognised_as_generic():
    """The check has to actually classify the selector that caused this."""
    assert _specificity('button[aria-label="More"]') > _specificity(
        '[data-tid="callingButtons-showMoreBtn"]')
