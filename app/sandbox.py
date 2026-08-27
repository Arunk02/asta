"""Nothing the bench runs can reach a human. Enforced, not promised.

Arun's instruction, and it is the right one: measuring Asta must never place a
call, join a meeting, or message a colleague. Today's runners are all pure
functions, so today the risk is zero — but "I checked, it's fine" is exactly the
guarantee that rots. The next scenario someone adds to measure "how good is it at
replying" is one import away from ringing a real person's phone at 2am, and the
suite would look like it was working right up until it did.

So the bench runs inside a seal. Every function that can reach the outside world
is replaced, for the duration, by a tripwire that raises. A scenario that tries to
send does not send and then get told off — it fails, loudly, as a SAFETY
violation, which `bench.score` caps rather than merely deducts.

The choke point that does most of the work is `teams_bridge._launch`. No browser
means no Teams tab, so no call can be dialled, no message typed and no compose
window submitted, regardless of what any future runner believes it is doing. The
named send/join functions above it are belt and braces: they name the intent, so
a tripped guard says "tried to call someone" rather than "tried to open Chromium".

Read-only paths are deliberately NOT sealed. Reading his message history to model
his writing style, reading a Jira issue, reading the ledger — those are what the
scenarios measure, and sealing them would measure a hobbled Asta.
"""

from __future__ import annotations

import contextlib
import importlib
import inspect


class OutwardMoveBlocked(RuntimeError):
    """A bench scenario tried to do something a human would have noticed."""


#: module -> the attributes that reach the outside world. Grouped by module so a
#: new sender is added next to its siblings rather than appended to a flat list
#: nobody reads.
BLOCKED: dict[str, tuple[str, ...]] = {
    # The browser itself. Sealing this alone makes calls and Teams sends
    # impossible; everything below is named so a trip reports the INTENT.
    "teams_bridge": ("_launch", "_pooled_page", "send_message", "check_session"),
    "meetings": ("join", "call_person", "join_by_phrase", "open_and_send"),
    "outlook": ("read_mail",),
    "jira": ("add_comment", "transition_issue"),
    "notify": ("notify", "deliver", "wa_send"),
    "telegram": ("send",),
    "ops": ("_teams_send", "_calendar_send"),
    "agent": ("teams_send_message", "prepare_to_send"),
}


def _tripwire(label: str, record: list[str]):
    """A stand-in that refuses, in both flavours the codebase actually uses."""

    def refuse(*_a, **_k):
        record.append(label)
        raise OutwardMoveBlocked(
            f"{label} was called during a bench run — scenarios must never "
            f"reach a person. If this scenario needs to prove a send, assert on "
            f"the STAGED intent instead of performing it.")

    async def refuse_async(*_a, **_k):
        return refuse()

    return refuse, refuse_async


@contextlib.contextmanager
def sealed(record: list[str] | None = None):
    """Replace every outward function with a tripwire for the duration.

    Restores originals unconditionally, including when the body raises — a seal
    that leaks on error would leave Asta unable to notify him about the very
    failure that broke the bench.
    """
    record = [] if record is None else record
    saved: list[tuple[object, str, object]] = []
    for mod_name, attrs in BLOCKED.items():
        try:
            mod = importlib.import_module(f".{mod_name}", package="app")
        except ImportError:
            continue
        for attr in attrs:
            original = getattr(mod, attr, None)
            if original is None:
                continue
            saved.append((mod, attr, original))
            refuse, refuse_async = _tripwire(f"{mod_name}.{attr}", record)
            setattr(mod, attr, refuse_async
                    if inspect.iscoroutinefunction(original) else refuse)
    try:
        yield record
    finally:
        for mod, attr, original in saved:
            setattr(mod, attr, original)


def covers() -> list[str]:
    """Every guarded name — so a test can assert the seal did not silently shrink."""
    return [f"{m}.{a}" for m, attrs in BLOCKED.items() for a in attrs]
