"""Shared "what is Mira's voice doing right now" state.

A tiny standalone module rather than living inside wakeword_listener.py: both
that module and proactive.py's spoken delivery need to set it, and main.py's
/voice/status endpoint needs to read it -- putting it in any one of those
would create an import cycle with the others.

Electron polls /voice/status to drive the notch's listening/speaking
animation, which is the whole reason this exists: the daemon is headless and
can't push to the renderer, so this is the one place it writes down what's
happening for Electron to notice on its next poll.
"""

import threading

_lock = threading.Lock()
_state = {"state": "idle", "text": "", "duration": 0}  # state: idle | listening | thinking | speaking


def set_state(state: str, text: str = "", duration: float = 0):
    with _lock:
        _state["state"] = state
        _state["text"] = text
        # Real playback length in seconds, when known (speak_reply reads it
        # from the synthesized audio file via afinfo) -- lets the notch pace
        # a word-by-word caption reveal against actual speech, not a guess.
        _state["duration"] = duration


def get_state() -> dict:
    with _lock:
        return dict(_state)
