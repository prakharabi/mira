"""Telegram access for Mira -- talk to it from your phone, works whenever this
Mac is reachable (awake), independent of the Electron UI being open.

Uses long-polling (Bot API `getUpdates`), not a webhook: the daemon makes
outbound connections to Telegram's servers, so this needs no public IP, no
port forwarding, and no tunnel. If the Mac is asleep, Telegram simply holds
delivery -- nothing is lost, it's just not instant until the daemon polls again.

Security model: Mira ships no bot of its own (same reasoning as the Google
integration -- no shared secret to leak). The user creates their own bot via
@BotFather and pastes the token into Settings. Beyond that, a bot token alone
lets ANYONE who finds the bot's username message it, so the daemon only ever
acts on messages from one pre-approved chat: the first person to message the
bot is told their chat ID and asked to save it into Settings themselves. Until
that chat ID is saved, the bot is fully inert -- it will not run automations,
read the Dump Box, or talk to any model on anyone's behalf.
"""

import json
import re
import threading
import time
import uuid
import datetime
import mimetypes
from pathlib import Path

import requests

API_ROOT = "https://api.telegram.org/bot{token}"
POLL_TIMEOUT = 30  # seconds -- Telegram holds the long-poll connection open this long
SESSION_ID = "telegram"
VOICE_DIR = Path.home() / "Mira" / "daemon" / "telegram_voice"

RUN_VERBS_HINT = "run|trigger|start|execute|fire|launch|kick off"


def log(message: str):
    print(f"[telegram] {message}", flush=True)


class TelegramController:
    def __init__(self):
        self._running = threading.Event()
        self._stop_requested = threading.Event()
        self._thread = None
        self._offset = 0
        self._last_error = ""

    def start(self):
        if self._running.is_set():
            return
        self._stop_requested.clear()
        self._running.set()
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()
        log("polling started")

    def stop(self):
        self._stop_requested.set()
        self._running.clear()
        log("polling stop requested")

    def is_running(self):
        return self._running.is_set()

    def last_error(self):
        return self._last_error

    def _loop(self):
        from main import load_settings  # deferred: avoids a circular import at module load

        while not self._stop_requested.is_set():
            settings = load_settings()
            token = settings.get("telegram_bot_token")
            if not token:
                # Nothing to poll with. Sleep and recheck rather than exiting --
                # the user may add a token in Settings without restarting Mira.
                time.sleep(5)
                continue

            try:
                url = API_ROOT.format(token=token) + "/getUpdates"
                resp = requests.get(url, params={
                    "offset": self._offset,
                    "timeout": POLL_TIMEOUT,
                    "allowed_updates": json.dumps(["message"]),
                }, timeout=POLL_TIMEOUT + 10)
                data = resp.json()

                if not data.get("ok"):
                    self._last_error = str(data.get("description", "unknown error"))
                    log(f"getUpdates error: {self._last_error}")
                    time.sleep(5)
                    continue

                self._last_error = ""
                for update in data.get("result", []):
                    self._offset = update["update_id"] + 1
                    try:
                        _handle_update(token, update)
                    except Exception as e:
                        log(f"error handling update: {e}")

            except requests.RequestException as e:
                # No internet, DNS hiccup, Telegram unreachable, etc. -- this is
                # the expected, harmless case whenever the Mac just woke up or
                # network flapped; keep retrying rather than giving up.
                self._last_error = str(e)
                time.sleep(5)

        self._running.clear()


_controller = TelegramController()


def start_telegram_bot_background():
    from main import load_settings
    settings = load_settings()
    if settings.get("telegram_enabled") and settings.get("telegram_bot_token"):
        _controller.start()


def set_telegram_enabled(enabled: bool):
    if enabled:
        _controller.start()
    else:
        _controller.stop()


def status():
    from main import load_settings
    settings = load_settings()
    return {
        "enabled": bool(settings.get("telegram_enabled")),
        "token_set": bool(settings.get("telegram_bot_token")),
        "owner_chat_id": settings.get("telegram_owner_chat_id", ""),
        "linked": bool(settings.get("telegram_owner_chat_id")),
        "polling": _controller.is_running(),
        "last_error": _controller.last_error(),
    }


# ---------- Telegram API helpers ----------

def _send_message(token: str, chat_id, text: str):
    try:
        requests.post(API_ROOT.format(token=token) + "/sendMessage", json={
            "chat_id": chat_id,
            "text": text[:4096],  # Telegram's own message length cap
        }, timeout=20)
    except requests.RequestException as e:
        log(f"sendMessage failed: {e}")


def _send_chat_action(token: str, chat_id, action: str = "typing"):
    try:
        requests.post(API_ROOT.format(token=token) + "/sendChatAction",
                      json={"chat_id": chat_id, "action": action}, timeout=10)
    except requests.RequestException:
        pass


class _TypingLoop:
    """Keeps Telegram's "typing..." indicator alive during a slow model call.

    Telegram clears the indicator after ~5s, so a single sendChatAction before
    a 20-second local-model reply would show it for a moment and then silently
    stop looking like anything is happening.
    """

    def __init__(self, token, chat_id):
        self._token, self._chat_id = token, chat_id
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)

    def _run(self):
        while not self._stop.is_set():
            _send_chat_action(self._token, self._chat_id, "typing")
            self._stop.wait(4)

    def __enter__(self):
        self._thread.start()
        return self

    def __exit__(self, *exc):
        self._stop.set()


def _download_file(token: str, file_id: str) -> Path:
    info = requests.get(API_ROOT.format(token=token) + "/getFile",
                        params={"file_id": file_id}, timeout=20).json()
    file_path = info["result"]["file_path"]
    suffix = Path(file_path).suffix or ".oga"

    VOICE_DIR.mkdir(parents=True, exist_ok=True)
    dest = VOICE_DIR / f"{uuid.uuid4().hex}{suffix}"

    url = f"https://api.telegram.org/file/bot{token}/{file_path}"
    resp = requests.get(url, timeout=60)
    dest.write_bytes(resp.content)
    return dest


# ---------- Update handling ----------

def _handle_update(token: str, update: dict):
    from main import load_settings

    message = update.get("message")
    if not message:
        return

    chat_id = message.get("chat", {}).get("id")
    if chat_id is None:
        return

    settings = load_settings()
    owner_chat_id = settings.get("telegram_owner_chat_id", "").strip()

    # Bootstrap: no owner linked yet. Tell WHOEVER messages the bot their chat
    # ID and stop -- the bot does nothing else until that ID is deliberately
    # saved into Settings by the user. This is trust-on-first-use made safe:
    # the "trust" step is a manual copy-paste into an interface an attacker
    # doesn't have, not an automatic acceptance of the first sender.
    if not owner_chat_id:
        _send_message(token, chat_id,
            f"This is Mira. Your chat ID is {chat_id}.\n\n"
            "To link this chat, open Mira -> Settings -> Telegram and paste that "
            "ID into \"Owner chat ID\". Nothing else will happen until you do.")
        return

    if str(chat_id) != str(owner_chat_id):
        # Not the linked owner. Stay silent rather than confirming the bot is
        # active/personal to an arbitrary sender.
        return

    text = message.get("text", "")
    voice = message.get("voice") or message.get("audio")

    with _TypingLoop(token, chat_id):
        if voice:
            reply = _handle_voice(token, voice)
        elif text.strip().lower().startswith("/dump"):
            reply = _handle_dump(text[len("/dump"):].strip())
        elif text.strip():
            reply = _handle_text(text.strip())
        else:
            reply = None

    if reply:
        _send_message(token, chat_id, reply)


def _handle_text(text: str) -> str:
    import main as _main
    result = _main.chat(session_id=SESSION_ID, message=text, model="auto")
    if isinstance(result, dict) and result.get("error"):
        return f"Sorry, something went wrong: {result['error']}"
    return result.get("response", "") if isinstance(result, dict) else str(result)


def _handle_voice(token: str, voice: dict) -> str:
    import main as _main
    try:
        filepath = _download_file(token, voice["file_id"])
    except (requests.RequestException, KeyError) as e:
        return f"Couldn't download that voice message: {e}"

    try:
        text, error = _main.transcribe_smart(filepath)
    finally:
        filepath.unlink(missing_ok=True)
        converted = filepath.with_suffix(".converted.wav")
        converted.unlink(missing_ok=True)
        Path(str(converted) + ".txt").unlink(missing_ok=True)

    if not text:
        return f"Couldn't transcribe that: {error}"

    reply = _handle_text(text)
    return f"\U0001F3A4 “{text}”\n\n{reply}"


def _handle_dump(text: str) -> str:
    import dumpbox as _dumpbox
    import main as _main

    if not text:
        return "Usage: /dump <whatever's on your mind>"

    entry = _dumpbox.add_entry(text)

    def llm_call(prompt: str) -> str:
        reply, _used = _main.route_prompt(prompt, "auto")
        return reply

    try:
        processed = _dumpbox.process_entry(entry["id"], llm_call)
    except Exception as e:
        return f"Saved to Dump Box, but couldn't process it yet: {e}"

    items = processed.get("action_items") or []
    if not items:
        return f"Saved to Dump Box.\n\n{processed.get('summary', '')}"

    lines = "\n".join(f"• {i['title']}" for i in items)
    return f"Saved to Dump Box.\n\n{processed.get('summary', '')}\n\n{lines}"
