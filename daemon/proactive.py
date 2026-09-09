"""Proactive alerts -- the thing that makes Mira an assistant rather than a chatbot.

Watches for a few situations worth interrupting about (a meeting about to
start, mail that looks like it needs an answer) and pushes them out. Delivery
prefers Telegram because that reaches Prakhar with the laptop shut, which is
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


def deliver(text: str) -> str:
    """Send an alert wherever it will actually be seen."""
    try:
        import telegram_bot
        status = telegram_bot.status()
        if status.get("linked"):
            from main import load_settings
            settings = load_settings()
            telegram_bot._send_message(
                settings.get("telegram_bot_token"),
                settings.get("telegram_owner_chat_id"),
                text,
            )
            return "telegram"
    except Exception as e:
        log(f"telegram delivery failed, falling back to notification: {e}")

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
        "interval_seconds": CHECK_INTERVAL_SECONDS,
    }
