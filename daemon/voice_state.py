"""Shared "what is Mira's voice doing right now" state.

A tiny standalone module rather than living inside wakeword_listener.py: both
that module and proactive.py's spoken delivery need to set it, and main.py's
/voice/status endpoint needs to read it -- putting it in any one of those
would create an import cycle with the others.

Electron long-polls /voice/status to drive the notch's listening/speaking
animation, which is the whole reason this exists: the daemon is headless and
can't push to the renderer, so this is the one place it writes down what's
happening. Every change bumps `version` and wakes any waiting request, so
the notch reacts the moment the state moves instead of on a 400ms tick --
which also took idle Mira from ~2.5 requests a second to one every 25s.
"""

import threading

_lock = threading.Lock()
_changed = threading.Condition(_lock)
_state = {"state": "idle", "text": "", "duration": 0, "version": 0}  # state: idle | listening | thinking | speaking


def set_state(state: str, text: str = "", duration: float = 0):
    with _lock:
        _state["state"] = state
        _state["text"] = text
        # Real playback length in seconds, when known (speak_reply reads it
        # from the synthesized audio file via afinfo) -- lets the notch pace
        # a word-by-word caption reveal against actual speech, not a guess.
        _state["duration"] = duration
        _state["version"] += 1
        _changed.notify_all()


def get_state() -> dict:
    with _lock:
        return dict(_state)


def wait_for_change(since: int, timeout: float) -> dict:
    """The state once its version differs from `since`, or the current state
    after `timeout` seconds -- whichever comes first."""
    with _lock:
        _changed.wait_for(lambda: _state["version"] != since, timeout=timeout)
        return dict(_state)
