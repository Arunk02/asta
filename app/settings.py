"""The knobs Asta may tune on its own — and nothing else.

P6 turns the day's signal into changes, which is only safe if the surface it
may change is small, named and reversible. A knob reached through here has:

    an override he or evolution set   (kv, the only thing evolution may write)
    his own setting                   (the environment — always wins over a default)
    the code's default                (what ships)

Anything not listed in KNOBS cannot be tuned, whatever a candidate proposes, and
every knob carries the bounds a value must fall inside. So the worst a bad
proposal can do is move one number a little, inside a range, and be rolled back.
"""

from __future__ import annotations

import os

from . import store

#: name -> (default, low, high, what it does). The bounds are the safety: a
#: candidate outside them is refused before it is ever measured.
KNOBS: dict[str, tuple[float, float, float, str]] = {
    "ASTA_COALESCE_SECONDS": (120, 30, 600, "how long one buzz waits to carry more"),
    "ASTA_ATTENTION_MIN_SEEN": (10, 5, 50, "items from a source before its record counts"),
    "ASTA_ATTENTION_IGNORE_SHARE": (0.8, 0.6, 0.95, "ignored share that moves a feed to the digest"),
    "ASTA_RESPOND_MAX_PER_HOUR": (4, 1, 12, "investigations an hour"),
}


def _key(name: str) -> str:
    return f"setting:{name}"


def override(name: str) -> str:
    return store.kv_get(_key(name)) or ""


def tuned(name: str) -> float | None:
    """What evolution (or he) has pinned this knob to, or None.

    The knobs keep their own precedence — module constant, his environment,
    the shipped default — and this is added on top rather than replacing it.
    Rewriting precedence would have quietly beaten a test that pins the
    constant, and his env would have beaten a change he had just approved.
    """
    raw = override(name)
    try:
        return float(raw) if str(raw).strip() else None
    except ValueError:
        return None


def effective(name: str, fallback: float) -> float:
    """What a knob is worth where it is read: tuned › his environment › fallback.

    The bench runs in a sandbox with its own database, so a tuned value stored
    there cannot reach it — a candidate is carried in as an environment value
    instead, and this is the one place that has to understand both.
    """
    pinned = tuned(name)
    if pinned is not None:
        return pinned
    raw = os.environ.get(name, "")
    try:
        return float(raw) if str(raw).strip() else float(fallback)
    except ValueError:
        return float(fallback)


def value(name: str, default=None) -> float:
    """What this knob is worth right now: an override, else his env, else the default."""
    spec = KNOBS.get(name)
    # An explicit default from the caller wins: the module constant a caller
    # passes is what that caller means by "unset", and a test pinning it must
    # still decide (tests/test_responder.py does exactly that).
    fallback = default if default is not None else (spec[0] if spec else None)
    for raw in (override(name), os.environ.get(name, "")):
        if str(raw).strip():
            try:
                return float(raw)
            except ValueError:
                continue
    return float(fallback if fallback is not None else 0)


def within_bounds(name: str, value_: float) -> bool:
    spec = KNOBS.get(name)
    return bool(spec) and spec[1] <= float(value_) <= spec[2]


def set_override(name: str, value_: float) -> str:
    """Only evolution and he do this, and only inside the knob's bounds."""
    if name not in KNOBS:
        raise ValueError(f"{name} is not a tunable setting")
    if not within_bounds(name, value_):
        low, high = KNOBS[name][1], KNOBS[name][2]
        raise ValueError(f"{name}={value_} is outside {low}–{high}")
    store.kv_set(_key(name), str(value_))
    return f"{name} = {value_}"


def clear(name: str) -> None:
    store.kv_del(_key(name))


def overrides() -> dict[str, float]:
    return {name: value(name) for name in KNOBS if override(name)}


def summary() -> str:
    live = overrides()
    if not live:
        return "Asta is running every setting as it ships."
    return "Settings Asta has tuned:\n" + "\n".join(
        f"  {n} = {v:g} (ships as {KNOBS[n][0]:g}) — {KNOBS[n][3]}" for n, v in live.items())
