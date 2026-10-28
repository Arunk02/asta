"""What a call or meeting verifiably went through, in order, with the evidence.

Each state is recorded only where something was actually observed — never
from an intention or a click:

  dialled           the call button was pressed and a call window opened
  ringing           the screen says it is ringing
  answered          positive connection evidence (timer, live remote stream,
                    captions, WebRTC media) — never the click, never the text alone
  lobby             Teams says someone has to let Asta in
  joined            in the meeting itself (no lobby on screen, or admitted from it)
  audio_ready       the browser received real audio from the call microphone
  speech_delivered  a line finished playing into an unmuted call
  ended             the call ended or Asta left

The summary is what the report after the call is allowed to claim: a call that
never reached `answered` was not answered, a meeting that never left `lobby`
was never attended, and "spoke" means a line was delivered, not that Asta was
allowed to speak.
"""
from __future__ import annotations

import time

STATES = ("dialled", "ringing", "answered", "lobby", "joined", "audio_ready",
          "speech_delivered", "ended")
#: How long to sit in a meeting lobby before leaving and saying so.
LOBBY_MINUTES = 15


def mark(call: dict, state: str, evidence: str = "", now: float | None = None) -> None:
    if state not in STATES:
        raise ValueError(f"unknown call state {state!r}")
    log = call.setdefault("states", [])
    if state not in ("speech_delivered",) and log and log[-1]["state"] == state:
        return                                   # a repeat observation, not a transition
    log.append({"state": state, "at": time.time() if now is None else now,
                "evidence": (evidence or "")[:160]})


def reached(call: dict, state: str) -> bool:
    return any(s["state"] == state for s in call.get("states") or [])


def summary(call: dict) -> dict:
    """What may be claimed afterwards, from the record alone."""
    seen = [s["state"] for s in call.get("states") or []]
    lobby_only = "lobby" in seen and "joined" not in seen
    return {"answered": "answered" in seen or "joined" in seen,
            "admitted": not lobby_only,
            "lines_delivered": seen.count("speech_delivered"),
            "audio_ready": "audio_ready" in seen,
            "states": list(call.get("states") or [])}
