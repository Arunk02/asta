# Calls and meetings recorded as verified states

`app/call_states.py` records what a call or meeting verifiably went through, in order, with the evidence for each step: dialled, ringing, answered, lobby, joined, audio ready, speech delivered, ended. Each mark sits at a point where the call code already has proof. Answered comes from screen or WebRTC connection evidence, never the click. Audio ready means the browser received real microphone audio. Speech delivered means a line finished playing into an unmuted call, or WebRTC sent sound.

Fixed with it:

- **The meeting lobby.** Clicking Join was recorded as being in the meeting. `call_screen` now recognises Teams' lobby, and the join is recorded as `lobby` until the screen shows Asta was let in (`joined`, "admitted from the lobby"). A meeting never let in is left after `LOBBY_MINUTES` (15). The report says "never let in from the lobby — nothing was heard" instead of giving a duration.
- **"Spoke".** The last-call summary's `spoke` was copied from `speaks`, which is the *permission* to speak. It now means at least one line was delivered, according to the record.

The existing call engine already decided ringing versus connected from layered evidence (`call_screen.call_state`, `call_rtc.wait_for_them`); this records that evidence instead of re-deciding it.

Limits: the lobby wordings are matched as Teams has shown them, and a new wording reads as `unknown`, not `joined`. Directory-backed stable identities and the sleep/wake and duplicate-join drills are not part of this change.
