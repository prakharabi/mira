"""Proactive alerts -- the thing that makes Mira an assistant rather than a chatbot.

Watches for a few situations worth interrupting about (a meeting about to
start, mail that looks like it needs an answer) and pushes them out. Delivery
prefers Telegram because that reaches the user with the laptop shut, which is
exactly when a "your call starts in 10 minutes" alert is worth anything; it
falls back to a local notification otherwise.

Two things keep this from becoming noise, which is the failure mode that makes
people switch alerting off and never turn it back on:

  * every alert is keyed and remembered, so the same meeting is never announced
    twice, and
  * it only speaks when it has something specific to say. There is no "nothing
    to report" heartbeat.
"""

import datetime
import json
import subprocess
import threading
import time
from pathlib import Path

SEEN_PATH = Path.home() / "Mira" / "daemon" / "memory_store" / "alerts_seen.json"
CHECK_INTERVAL_SECONDS = 300           # 5 minutes
MEETING_LEAD_MINUTES = 15              # how far ahead a meeting is worth flagging
MAX_SEEN_KEYS = 500

_thread = None
_stop = threading.Event()


def log(message: str):
    print(f"[proactive] {message}", flush=True)


def _load_seen() -> set:
    if not SEEN_PATH.exists():
        return set()
    try:
        return set(json.loads(SEEN_PATH.read_text()))
    except (json.JSONDecodeError, OSError):
        return set()


def _save_seen(seen: set):
    # Trimmed, because this file would otherwise grow forever for no benefit --
    # an alert from last month can't fire again anyway.
    trimmed = list(seen)[-MAX_SEEN_KEYS:]
    SEEN_PATH.parent.mkdir(parents=True, exist_ok=True)
    SEEN_PATH.write_text(json.dumps(trimmed))


def is_screen_locked() -> bool:
    """True while the display is locked (lock screen, or fast user switching).

    `ioreg -a` dumps IORegistry's Root node as an XML property list, which
    plistlib parses directly -- no PyObjC/Quartz dependency needed just for
    one boolean. IOConsoleLocked is the key macOS itself sets; it is absent
    (not just false) when unlocked, hence the permissive .get().
    """
    try:
        out = subprocess.run(["ioreg", "-n", "Root", "-d1", "-a"],
                             capture_output=True, timeout=5)
        import plistlib
        return bool(plistlib.loads(out.stdout).get("IOConsoleLocked", False))
    except Exception:
        # Fail toward the safer assumption: if we can't tell, prefer Telegram
        # (a message waiting to be read) over speaking into a room where we
        # don't actually know anyone can hear it.
        return True


def _notify_mac(title: str, body: str):
    """Local fallback. Passed via argv, never interpolated into the script."""
    script = ('on run argv\n'
              '  display notification (item 2 of argv) with title (item 1 of argv)\n'
              'end run')
    try:
        subprocess.run(["osascript", "-e", script, "--", title, body],
                       capture_output=True, timeout=10)
    except (subprocess.SubprocessError, OSError):
        pass


def _send_telegram(text: str) -> bool:
    try:
        import telegram_bot
        status = telegram_bot.status()
        if not status.get("linked"):
            return False
        from main import load_settings
        settings = load_settings()
        telegram_bot._send_message(
            settings.get("telegram_bot_token"),
            settings.get("telegram_owner_chat_id"),
            text,
        )
        return True
    except Exception as e:
        log(f"telegram delivery failed: {e}")
        return False


def _speak(text: str) -> bool:
    """Say it aloud, the same way a wake-word reply does."""
    try:
        from wakeword_listener import speak_reply
        speak_reply(text)
        return True
    except Exception as e:
        log(f"speaking failed: {e}")
        return False


def deliver(text: str) -> str:
    """Send an alert wherever it will actually be seen.

    Delivery mode is a setting, "auto" by default: locked screen means
    Telegram (so it's waiting when the lid opens), unlocked means spoken
    aloud (so it doesn't interrupt whatever's on screen with a written
    message you have to go read). "telegram" and "voice" pin one or the
    other regardless of lock state, for anyone who'd rather choose than have
    Mira guess.
    """
    from main import load_settings
    mode = load_settings().get("proactive_delivery", "auto")

    if mode == "telegram":
        if _send_telegram(text):
            return "telegram"
    elif mode == "voice":
        if _speak(text):
            return "voice"
    else:  # auto
        if is_screen_locked():
            if _send_telegram(text):
                return "telegram"
        else:
            if _speak(text):
                return "voice"
            if _send_telegram(text):  # spoken failed (e.g. muted) -- still get it to them
                return "telegram"

    _notify_mac("Mira", text)
    return "notification"


def _check_calendar(seen: set) -> list:
    """Meetings starting inside the lead window."""
    try:
        import google_integration as g
        if not g.is_connected():
            return []
        events = g.calendar_list_events(days=1, max_results=20)
    except Exception as e:
        log(f"calendar check failed: {e}")
        return []

    now = datetime.datetime.now(datetime.timezone.utc)
    alerts = []

    for ev in events:
        start = ev.get("start") or ""
        if ev.get("all_day") or not start:
            continue
        try:
            start_dt = datetime.datetime.fromisoformat(start.replace("Z", "+00:00"))
        except ValueError:
            continue

        minutes_away = (start_dt - now).total_seconds() / 60
        if not (0 <= minutes_away <= MEETING_LEAD_MINUTES):
            continue

        key = f"cal:{ev.get('id')}"
        if key in seen:
            continue
        seen.add(key)

        when = start_dt.astimezone().strftime("%I:%M %p").lstrip("0")
        line = f"📅 {ev.get('summary', 'Meeting')} starts at {when} ({int(minutes_away)} min)"
        if ev.get("location"):
            line += f"\n{ev['location']}"
        alerts.append(line)

    return alerts


def _check_email(seen: set) -> list:
    """Unread mail addressed directly to the user.

    Deliberately narrow: `to:me` filters out the newsletters and CCs that make
    up most of an inbox, so this stays an alert rather than a feed.
    """
    try:
        import google_integration as g
        if not g.is_connected():
            return []
        messages = g.gmail_list("is:unread to:me newer_than:1d", max_results=5)
    except Exception as e:
        log(f"email check failed: {e}")
        return []

    alerts = []
    for m in messages:
        key = f"mail:{m.get('id')}"
        if key in seen:
            continue
        seen.add(key)
        sender = (m.get("from") or "").split("<")[0].strip().strip('"')
        alerts.append(f"✉️ {sender}: {m.get('subject', '(no subject)')}")

    return alerts



def _check_tasks(seen: set) -> list:
    """Open commitments that have come due or gone quiet.

    Keyed by task id AND nudge day, so a task that stays open can be raised
    again after the cooloff in tasks.py without the seen-set silencing it
    forever -- but never twice in the same day.
    """
    try:
        import tasks
        candidates = tasks.due_or_stale()
    except Exception as e:
        log(f"task check failed: {e}")
        return []

    today = datetime.date.today().isoformat()
    alerts, nudged = [], []

    for task, line in candidates:
        key = f"task:{task['id']}:{today}"
        if key in seen:
            continue
        seen.add(key)
        alerts.append(line)
        nudged.append(task["id"])

    if nudged:
        try:
            tasks.mark_nudged(nudged)
        except Exception as e:
            log(f"could not stamp nudged tasks: {e}")

    return alerts


def run_checks(force: bool = False) -> dict:
    """One pass. Returns what it found, so this is testable without waiting."""
    from main import load_settings
    settings = load_settings()

    if not force and not settings.get("proactive_enabled", False):
        return {"ran": False, "reason": "disabled"}

    seen = _load_seen()
    alerts = []

    if settings.get("proactive_calendar", True):
        alerts.extend(_check_calendar(seen))
    if settings.get("proactive_email", False):
        alerts.extend(_check_email(seen))
    if settings.get("proactive_tasks", True):
        alerts.extend(_check_tasks(seen))

    delivered_via = None
    if alerts:
        # One message, not one per item -- five separate pings for five emails
        # is how an assistant becomes something you mute.
        delivered_via = deliver("\n\n".join(alerts))
        _save_seen(seen)
        log(f"delivered {len(alerts)} alert(s) via {delivered_via}")
    else:
        _save_seen(seen)

    return {"ran": True, "alerts": alerts, "delivered_via": delivered_via}


def _loop():
    log("proactive alert loop started")
    while not _stop.is_set():
        try:
            run_checks()
        except Exception as e:
            log(f"check failed: {e}")
        _stop.wait(CHECK_INTERVAL_SECONDS)
    log("proactive alert loop stopped")


def start_proactive_background():
    global _thread
    if _thread and _thread.is_alive():
        return
    _stop.clear()
    _thread = threading.Thread(target=_loop, daemon=True)
    _thread.start()


def stop_proactive():
    _stop.set()


def status() -> dict:
    from main import load_settings
    settings = load_settings()
    return {
        "enabled": bool(settings.get("proactive_enabled", False)),
        "running": bool(_thread and _thread.is_alive()),
        "calendar": bool(settings.get("proactive_calendar", True)),
        "email": bool(settings.get("proactive_email", False)),
        "tasks": bool(settings.get("proactive_tasks", True)),
        "delivery": settings.get("proactive_delivery", "auto"),
        "screen_locked": is_screen_locked(),
        "interval_seconds": CHECK_INTERVAL_SECONDS,
    }
