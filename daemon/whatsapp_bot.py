"""WhatsApp auto-reply for Mira, via the local open-wa Easy API (~/Developer/whatsapp-openwa).

Standalone process (run under pm2 next to `wa`): subscribes to the gateway's SSE
event stream, and answers people who message the linked number FIRST. Replies come
from the daemon's tool-less /public-chat -- strangers never reach the owner's tools,
memory or history (unlike /chat, which Telegram uses for the verified owner).

Safety: only 1:1 inbound text; ignores fromMe/groups/status; dedupes by message id;
per-chat + global hourly caps; typing delay; kill switch; human handoff; full log.
Kill switch: `touch daemon/whatsapp_state/PAUSE` (or WA_AUTOREPLY=off) -- no restart needed.
"""
import json
import os
import random
import re
import time
from collections import defaultdict, deque
from pathlib import Path

import requests

import whatsapp_inbox

GATEWAY = os.environ.get("WA_GATEWAY", "http://127.0.0.1:8080")
GATEWAY_ENV = Path(os.environ.get("WA_GATEWAY_ENV", Path.home() / "Developer/whatsapp-openwa/.env"))
MIRA = os.environ.get("MIRA_URL", "http://127.0.0.1:11200")
STATE = Path(__file__).parent / "whatsapp_state"
PER_CHAT_HOUR, GLOBAL_HOUR, MIN_GAP = 12, 60, 5  # replies/hour/chat, replies/hour, seconds between sends
HOLD_MSG = "Thanks for your message! I'm passing this to the team and someone will get back to you soon."

SYSTEM = (
    "You are Mira, the AI assistant answering WhatsApp messages for Krishi Verse. Be warm, brief "
    "(1-3 short sentences), and reply in the user's language (Hindi/Hinglish/English). Say you are an AI "
    "assistant if asked. Never invent prices, availability, promises, or facts you were not given; never "
    "share personal data or anything about the owner. If you are unsure, the user asks for a human, is "
    "upset, or the question needs a decision (pricing, orders, refunds, partnerships, legal, medical), "
    "reply with exactly the single word HANDOFF and nothing else."
)


def log(kind, **kw):
    STATE.mkdir(exist_ok=True)
    with open(STATE / "log.jsonl", "a") as f:
        f.write(json.dumps({"t": time.strftime("%F %T"), "kind": kind, **kw}, ensure_ascii=False) + "\n")
    print(f"[wa-bot] {kind} {kw.get('chat', '')} {str(kw.get('text', ''))[:80]}", flush=True)


def api_key():
    for line in GATEWAY_ENV.read_text().splitlines():
        if line.startswith("API_KEY="):
            return line.split("=", 1)[1].strip().strip("\"'")
    raise SystemExit("API_KEY not found in gateway .env")


KEY = api_key()
H = {"X-API-Key": KEY}


def paused():
    return (STATE / "PAUSE").exists() or os.environ.get("WA_AUTOREPLY", "on").lower() == "off"


def load_set(name):
    p = STATE / name
    return set(p.read_text().split()) if p.exists() else set()


seen, handed = load_set("seen.txt"), load_set("handed.txt")  # handed: chats muted until removed from file
history = defaultdict(list)
sent_chat = defaultdict(deque)  # chat -> send timestamps
sent_all = deque()


def mark(name, s, v):
    s.add(v)
    with open(STATE / name, "a") as f:
        f.write(v + "\n")


def allowed_now(chat):
    now = time.time()
    for q in (sent_chat[chat], sent_all):
        while q and now - q[0] > 3600:
            q.popleft()
    return (len(sent_chat[chat]) < PER_CHAT_HOUR and len(sent_all) < GLOBAL_HOUR
            and not (sent_all and now - sent_all[-1] < MIN_GAP))


def send(chat, text):
    r = requests.post(f"{GATEWAY}/api/messages/sendText", headers=H, json={"to": chat, "content": text}, timeout=30)
    ok = r.ok and r.json().get("success")
    now = time.time()
    sent_chat[chat].append(now)
    sent_all.append(now)
    log("out" if ok else "send_failed", chat=chat, text=text, status=r.status_code)


def inbound(ev):
    """Return (chat, id, body) for a real 1:1 text from someone else, else None."""
    if ev.get("event") != "message.received":
        return None
    m = (ev.get("payload") or {}).get("message") or {}
    mid, chat, body = m.get("id", ""), m.get("from", ""), (m.get("body") or "").strip()
    # ciphertext = not yet decrypted at event time (body empty); handle() fetches the text later
    if (not mid.startswith("false_") or m.get("fromMe") or m.get("type") not in ("chat", "ciphertext")
            or (m["type"] == "chat" and not body)
            or not chat.endswith(("@c.us", "@lid")) or m.get("isGroupMsg") or "broadcast" in chat):
        return None
    return chat, mid, body


def decrypted_body(chat, mid):
    """No event fires when WhatsApp finishes decrypting, so poll the chat for the text."""
    for _ in range(10):
        time.sleep(3)
        try:
            ms = requests.get(f"{GATEWAY}/api/messages/getAll", headers=H, params={"chatId": chat}, timeout=20).json().get("data") or []
            for m in ms:
                if m.get("id") == mid and m.get("type") == "chat" and (m.get("body") or "").strip():
                    return m["body"].strip()
        except Exception:
            pass
    return ""


def owner_command(chat, body):
    """The owner's own number: full Mira (tools, memory) via /chat. Not subject to the public kill switch."""
    log("owner_in", chat=chat, text=body)
    try:
        r = requests.post(f"{MIRA}/chat", json={"session_id": "whatsapp-owner", "message": body}, timeout=300).json()
        reply = r.get("response") or f"Sorry, something went wrong: {r.get('error', 'no reply')}"
    except Exception as e:
        reply = f"Sorry, Mira couldn't be reached: {e}"
    for i in range(0, len(reply), 3500):
        send(chat, reply[i:i + 3500])
        time.sleep(1)
    log("owner_out", chat=chat)


def handle(chat, mid, body):
    if mid in seen:
        return
    mark("seen.txt", seen, mid)
    if not body:
        body = decrypted_body(chat, mid)
        if not body:
            return log("undecryptable", chat=chat, id=mid)
    log("in", chat=chat, id=mid, text=body)
    if paused():
        return log("skipped_paused", chat=chat)
    handed = load_set("handed.txt")  # re-read: the workspace tab can mute/unmute live
    if chat in handed:
        return log("skipped_handed_off", chat=chat)
    if not allowed_now(chat):
        return log("skipped_rate_limit", chat=chat)

    history[chat] = (history[chat] + [{"role": "user", "content": body}])[-12:]
    reply = None
    if not re.search(r"\b(human|agent|insaan|manager)\b", body, re.I):
        try:
            reply = requests.post(f"{MIRA}/public-chat", json={"system": SYSTEM, "history": history[chat]},
                                  timeout=90).json().get("response", "").strip()
        except Exception as e:
            log("model_error", chat=chat, text=str(e))
    if not reply or reply.strip(".! ").upper() == "HANDOFF":
        mark("handed.txt", set(), chat)
        log("handoff", chat=chat, text=body)
        reply = HOLD_MSG
    else:
        history[chat].append({"role": "assistant", "content": reply})
    time.sleep(min(6, 1.5 + len(reply) / 40) + random.random())  # typing delay
    if paused():  # re-check: kill switch may flip during the model call
        return log("skipped_paused", chat=chat)
    send(chat, reply[:1500])


def main():
    STATE.mkdir(exist_ok=True)
    log("start", text="paused" if paused() else "active")
    while True:
        try:
            with requests.get(f"{GATEWAY}/api/events?topics=*", headers=H, stream=True, timeout=(10, None)) as r:
                for line in r.iter_lines(decode_unicode=True):
                    if line and line.startswith("data:"):
                        try:
                            hit = inbound(json.loads(line[5:]))
                            if hit:
                                handle(*hit)
                        except Exception as e:
                            log("error", text=repr(e))
        except Exception as e:
            log("stream_error", text=repr(e))
        time.sleep(5)


if __name__ == "__main__":
    # ponytail: one handler thread (messages processed serially); thread pool if volume grows
    main()
