"""A queue for work the daemon cannot do itself.

A few capabilities need Apple Events (creating Reminders, most notably), and a
headless LaunchAgent cannot obtain an Automation consent grant -- its osascript
call just hangs on a dialog nobody ever sees. Electron, being a foreground GUI
app, can. So the daemon enqueues those here, Electron polls and executes them,
and posts the result back.

Kept in memory rather than on disk on purpose: these are all "do this now"
requests tied to a live conversation. A queued reminder that survived a daemon
restart and fired an hour later, out of context, would be worse than one that
was simply reported as failed.
"""

import threading
import time
import uuid

_lock = threading.Lock()
_actions = {}          # id -> action dict
_results = {}          # id -> result dict
_events = {}           # id -> threading.Event

MAX_AGE_SECONDS = 120


def _prune_locked():
    now = time.time()
    stale = [aid for aid, a in _actions.items() if now - a["created_at"] > MAX_AGE_SECONDS]
    for aid in stale:
        _actions.pop(aid, None)
        _results.pop(aid, None)
        ev = _events.pop(aid, None)
        if ev:
            ev.set()  # unblock anything still waiting rather than leave it hanging


def enqueue(action_type: str, params: dict) -> str:
    action_id = uuid.uuid4().hex[:12]
    with _lock:
        _prune_locked()
        _actions[action_id] = {
            "id": action_id,
            "type": action_type,
            "params": params or {},
            "created_at": time.time(),
            "claimed": False,
        }
        _events[action_id] = threading.Event()
    return action_id


def pending() -> list:
    """Actions Electron hasn't picked up yet. Claiming them here means two
    polls in flight can't execute the same reminder twice."""
    with _lock:
        _prune_locked()
        out = []
        for a in _actions.values():
            if not a["claimed"]:
                a["claimed"] = True
                out.append({"id": a["id"], "type": a["type"], "params": a["params"]})
        return out


def complete(action_id: str, result: dict) -> bool:
    with _lock:
        if action_id not in _actions:
            return False
        _results[action_id] = result or {}
        ev = _events.get(action_id)
    if ev:
        ev.set()
    return True


def wait_for(action_id: str, timeout: float = 8.0):
    """Block until Electron reports back, or give up.

    Callers are mid-conversation, so the timeout is short and a miss returns
    None rather than raising -- the tool layer turns that into an honest "it
    was queued but didn't confirm in time" instead of claiming success.
    """
    with _lock:
        ev = _events.get(action_id)
    if ev is None:
        return None
    if not ev.wait(timeout):
        return None
    with _lock:
        return _results.get(action_id)
