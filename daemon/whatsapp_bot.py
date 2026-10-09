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
import threading
import re
import subprocess
import time
from collections import defaultdict, deque
from pathlib import Path

import requests

import whatsapp_inbox

GATEWAY = os.environ.get("WA_GATEWAY", "http://127.0.0.1:8080")
GATEWAY_ENV = Path(os.environ.get("WA_GATEWAY_ENV", Path.home() / "Developer/whatsapp-openwa/.env"))
MIRA = os.environ.get("MIRA_URL", "http://127.0.0.1:11200")
STATE = Path(__file__).parent / "whatsapp_state"
PER_CHAT_HOUR, GLOBAL_HOUR, MIN_GAP = 30, 150, 5  # replies/hour/chat, replies/hour, seconds between sends
INTRO = "Welcome to KrishiVerse! My name is ParthAI, KrishiVerse's AI assistant."
HOLD_MSG = "Thanks for your message! I'm passing this to the team and someone will get back to you soon."
FIRST = ("This is your FIRST reply in this chat: begin with a short welcome and introduce yourself exactly in this spirit, in the "
         f"customer's language: \"{INTRO}\" Then answer their question in the same message (unless you must reply HANDOFF, in which case reply only HANDOFF).")

SYSTEM = (
    "You are ParthAI, the AI assistant of KrishiVerse, answering WhatsApp messages. Be warm, brief "
    "(1-3 short sentences), and reply in the SAME language and script the customer wrote in (English; Hindi in Devanagari; Hinglish, i.e. Hindi written in Roman letters, must be answered in Roman letters and never Devanagari; or any other language); the knowledge below may be in English only, so understand the question in any language, find the answer in the English data, and translate it, keeping prices, numbers, product names and the \u20b9 symbol exactly. Say you are an AI "
    "assistant if asked. The earlier messages of this chat are included: remember what the customer already told you and do not ask again. Never invent prices, availability, promises, or facts you were not given; never "
    "share personal data or anything about the owner. If you are unsure, the user asks for a human, is "
    "upset, or the question needs a decision (pricing, orders, refunds, partnerships, legal, medical), "
    "reply with exactly the single word HANDOFF and nothing else."
)


def knowledge():
    """Owner-provided notes (kb.md, your shared database) + answers the owner gave to past handoffs (learned.jsonl)."""
    parts = []
    kb = STATE / "kb.md"
    if kb.exists():
        parts.append(kb.read_text()[:16000])
    ln = STATE / "learned.jsonl"
    if ln.exists():
        for l in ln.read_text().splitlines()[-80:]:
            e = json.loads(l)
            parts.append(f"Q: {e['q']}\nA: {e['a']}")
    if not parts:
        return SYSTEM
    return SYSTEM + "\n\nKnowledge you may use. Answer ONLY from this. If entries conflict, the later Q/A pairs (given by the owner) win. If it does not cover the question, reply HANDOFF:\n\n" + "\n\n".join(parts)


def ask(system, history):
    for attempt in range(3):  # Groq's per-minute token cap: wait it out rather than hand off
        try:
            r = requests.post(f"{MIRA}/public-chat", json={"system": system, "history": history}, timeout=90).json()
            if r.get("response"):
                return r["response"].strip()
            log("model_error", text=str(r.get("error"))[:120])
        except Exception as e:
            log("model_error", text=str(e))
        time.sleep(20)
    return ""


def detect_language(text):
    """One cheap call so the reply language is explicit instead of left to the model's guess."""
    out = ask("Identify the language and script of the message. Answer with exactly one of: English | Hindi (Devanagari script) | "
              "Hinglish (Hindi written in Roman letters) | or the language name with its script (e.g. Gujarati in Gujarati script). "
              "No other words.", [{"role": "user", "content": text}]).strip()
    return out.splitlines()[0][:60] if out else ""


def in_their_language(chat_msg, text, fallback):
    """Rewrite `text` in the language/script of `chat_msg`. Falls back to the original on any failure."""
    out = ask("Rewrite the TEXT in the same language and script as the CUSTOMER MESSAGE (Hinglish stays in Roman letters). "
              "Keep all numbers, prices, names and the \u20b9 symbol exactly; add nothing. Output only the rewritten text.",
              [{"role": "user", "content": f"CUSTOMER MESSAGE: {chat_msg}\n\nTEXT: {text}"}])
    return out or fallback


def compose_reply(question, note):
    """The owner's #N text is a note to Mira, not a message to the customer. Turn it into a proper reply."""
    return ask("You are ParthAI, KrishiVerse's AI assistant, answering a customer on WhatsApp. The OWNER gave you a short internal NOTE answering the "
               "customer's QUESTION. Write the reply to the customer: warm, clear, 1-3 short sentences, in the same language and script "
               "as the question (Hinglish stays in Roman letters), friendly and polite (a short greeting or thanks is fine). Use ONLY facts from the note; keep numbers and names exact and write rupee amounts with the \u20b9 symbol (never rs/Rs/INR); include EVERY fact the note gives (price, where to buy, delivery, etc.) and nothing it does not give. A stray '#' before an amount in the note means \u20b9. Output only the message to send.",
               [{"role": "user", "content": f"QUESTION: {question}\n\nNOTE: {note}"}])


def tickets():
    p = STATE / "tickets.json"
    return json.loads(p.read_text()) if p.exists() else {}


def save_tickets(t):
    (STATE / "tickets.json").write_text(json.dumps(t, ensure_ascii=False, indent=1))


names = {}  # chat -> WhatsApp display name, filled from inbound events


def open_ticket(chat, question):
    t = tickets()
    tid = str(max([int(k) for k in t] + [0]) + 1)
    t[tid] = {"chat": chat, "name": names.get(chat, ""), "q": question, "open": True}
    save_tickets(t)
    who = f"{names.get(chat) or 'Someone'} (+{chat.split('@')[0]})"
    whatsapp_inbox.send_to_owner(
        f"Handoff #{tid} from {who}:\n\n\"{question}\"\n\nReply with \"#{tid} your answer\" and I'll send it to them and learn it. "
        f"\"#{tid} close\" lets me resume without replying.")
    log("ticket_opened", chat=chat, id=tid, text=question)


def answer_ticket(tid, answer):
    t = tickets()
    tk = t.get(tid)
    if not tk or not tk["open"]:
        return f"No open handoff #{tid}."
    tk["open"] = False
    save_tickets(t)
    whatsapp_inbox.mute(tk["chat"], False)  # bot resumes for this chat
    if answer.strip().lower() == "close":
        return f"Closed #{tid}; I'll answer {tk['name'] or 'them'} myself from now on."
    final = compose_reply(tk["q"], answer) or in_their_language(tk["q"], answer, answer)
    send(tk["chat"], final)
    with open(STATE / "learned.jsonl", "a") as f:
        f.write(json.dumps({"q": tk["q"], "a": answer, "t": time.strftime("%F")}, ensure_ascii=False) + "\n")
    log("ticket_answered", chat=tk["chat"], id=tid)
    return f"Sent to {tk['name'] or tk['chat']} and saved to my knowledge. (#{tid})\n\nI wrote:\n{final}"


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
def chat_history(chat, mids, body, n=30):
    """The whole conversation, read from the gateway (survives restarts; includes replies Mira and the owner sent)."""
    out = []
    try:
        ms = requests.get(f"{GATEWAY}/api/messages/getAll", headers=H, params={"chatId": chat}, timeout=20).json().get("data") or []
        for m in sorted(ms, key=lambda m: m.get("t") or 0):
            t = (m.get("body") or "").strip()
            if m.get("type") == "chat" and t and m.get("id") not in mids:
                out.append({"role": "assistant" if m["id"].startswith("true_") else "user", "content": t})
    except Exception as e:
        log("history_error", chat=chat, text=repr(e))
    return out[-n:] + [{"role": "user", "content": body}]
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
    return len(sent_chat[chat]) < PER_CHAT_HOUR and len(sent_all) < GLOBAL_HOUR


def send(chat, text):
    if sent_all:  # keep a gap between sends by waiting, never by dropping the message
        time.sleep(max(0, MIN_GAP - (time.time() - sent_all[-1])))
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
    if m.get("notifyName"):
        names[chat] = m["notifyName"]
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
    m = re.match(r"#(\d+)\s+(.+)", body, re.S)
    if m:  # answer to a handoff ticket
        send(chat, answer_ticket(m.group(1), m.group(2).strip()))
        return log("owner_out", chat=chat)
    try:
        r = requests.post(f"{MIRA}/chat", json={"session_id": "whatsapp-owner", "message": body}, timeout=300).json()
        reply = r.get("response") or f"Sorry, something went wrong: {r.get('error', 'no reply')}"
    except Exception as e:
        reply = f"Sorry, Mira couldn't be reached: {e}"
    for i in range(0, len(reply), 3500):
        send(chat, reply[i:i + 3500])
        time.sleep(1)
    log("owner_out", chat=chat)


DEBOUNCE = 4  # seconds of quiet before answering, so messages sent together are answered together
pending = defaultdict(list)  # chat -> [(mid, body)]
timers = {}
limit_notice = {}  # chat -> when we last told them the limit was reached
work = threading.Lock()  # one conversation is processed at a time


def handle(chat, mid, body):
    """Queue an inbound message; answer once the sender has paused (owner commands go right away)."""
    if mid in seen:
        return
    mark("seen.txt", seen, mid)
    pending[chat].append((mid, body))
    if timers.get(chat):
        timers[chat].cancel()
    wait = 0 if chat in whatsapp_inbox.owner_ids() else DEBOUNCE
    timers[chat] = threading.Timer(wait, flush, [chat])
    timers[chat].start()


def queue_add(chat, mids, body):
    q = load_queue()
    q.setdefault(chat, []).append({"mids": mids, "body": body})
    (STATE / "queue.json").write_text(json.dumps(q, ensure_ascii=False))
    log("queued_rate_limit", chat=chat, text=body)


def load_queue():
    p = STATE / "queue.json"
    return json.loads(p.read_text()) if p.exists() else {}


def queue_worker():
    """Answer messages that arrived while a chat was over its hourly cap, once the cap frees up."""
    while True:
        time.sleep(30)
        try:
            with work:
                q = load_queue()
                for chat in list(q):
                    if allowed_now(chat) and not paused():
                        items = q.pop(chat)
                        (STATE / "queue.json").write_text(json.dumps(q, ensure_ascii=False))
                        process(chat, [m for it in items for m in it["mids"]], "\n".join(it["body"] for it in items))
        except Exception as e:
            log("error", text="queue: " + repr(e))


PM2_DIR = GATEWAY_ENV.parent


def gateway_ok():
    """getMe exercises the real browser page; /health stays 'ready' even when the page has crashed."""
    try:
        return bool(requests.get(f"{GATEWAY}/api/session/getMe", headers=H, timeout=20).json().get("success"))
    except Exception:
        return False


def restart_gateway():
    env = {**os.environ, "PATH": f"{Path(subprocess.check_output(['/bin/zsh', '-lc', 'command -v node'], text=True).strip()).parent}:{os.environ.get('PATH', '')}"}
    subprocess.run([str(PM2_DIR / "node_modules/.bin/pm2"), "restart", "wa", "wa-hook"], cwd=PM2_DIR, env=env, capture_output=True, timeout=60)


def watchdog():
    """Restart the gateway after 3 failed checks in a row, then tell the owner on WhatsApp. At most one restart per 10 minutes."""
    fails, last_restart = 0, 0
    while True:
        time.sleep(60)
        fails = 0 if gateway_ok() else fails + 1
        if fails >= 3 and time.time() - last_restart > 600:
            log("watchdog_restart", text="gateway unresponsive; restarting wa")
            try:
                restart_gateway()
            except Exception as e:
                log("watchdog_error", text=repr(e))
            last_restart, fails = time.time(), 0
            time.sleep(120)  # let it log back in
            ok = gateway_ok()
            log("watchdog_result", text="recovered" if ok else "still down - may need a QR re-link")
            if ok:
                whatsapp_inbox.send_to_owner("Heads-up: the WhatsApp gateway had stopped responding; I restarted it and it is back. Messages sent while it was down may not have been answered.")


def flush(chat):
    with work:
        items, pending[chat] = pending[chat], []
        try:
            bodies = []
            for mid, body in items:
                body = body or decrypted_body(chat, mid)
                if body:
                    bodies.append(body)
                else:
                    log("undecryptable", chat=chat, id=mid)
            if bodies:
                process(chat, [m for m, _ in items], "\n".join(bodies))
        except Exception as e:
            log("error", chat=chat, text=repr(e))


def process(chat, mids, body):
    if chat in whatsapp_inbox.owner_ids():
        return owner_command(chat, body)
    log("in", chat=chat, id=mids[-1], text=body)
    if paused():
        return log("skipped_paused", chat=chat)
    handed = load_set("handed.txt")  # re-read: the workspace tab can mute/unmute live
    if chat in handed:
        for tid, tk in tickets().items():  # pass follow-ups from a waiting person on to the owner
            if tk["open"] and tk["chat"] == chat:
                whatsapp_inbox.send_to_owner(f"Follow-up on #{tid} from {tk['name'] or chat.split('@')[0]}: \"{body}\"")
        return log("skipped_handed_off", chat=chat)
    if not allowed_now(chat):
        queue_add(chat, mids, body)
        if time.time() - limit_notice.get(chat, 0) > 3600:  # tell them once an hour instead of going silent
            limit_notice[chat] = time.time()
            msg = "We've reached the message limit for this chat right now. I've saved your message and ParthAI will reply automatically in about an hour."
            send(chat, in_their_language(body, msg, msg))
        return

    hist = chat_history(chat, mids, body)
    first = not any(m["role"] == "assistant" for m in hist)
    reply = None
    if not re.search(r"\b(human|agent|insaan|manager)\b", body, re.I):
        lang = detect_language(body)
        english = not lang or lang.lower().startswith("english")
        # Answer from the English data in English (most reliable), then translate explicitly into the customer's language/script.
        reply = ask(knowledge() + "\n\nWrite your reply in English only; it will be translated afterwards." + ("\n\n" + FIRST if first else ""), hist)
        if reply and reply.strip(".! ").upper() != "HANDOFF" and not english:
            reply = ask(f"Translate the text into {lang}. " + ("Hinglish means Hindi written in Roman letters: never use Devanagari. " if "inglish" in lang else "")
                        + "Keep numbers, prices, names, URLs and the \u20b9 symbol exactly. Output only the translation.",
                        [{"role": "user", "content": reply}]) or reply
    if not reply or reply.strip(".! ").upper() == "HANDOFF":
        mark("handed.txt", set(), chat)
        log("handoff", chat=chat, text=body)
        open_ticket(chat, body)
        reply = in_their_language(body, (INTRO + ' ' if first else '') + HOLD_MSG, (INTRO + ' ' if first else '') + HOLD_MSG)
    time.sleep(min(6, 1.5 + len(reply) / 40) + random.random())  # typing delay
    if paused():  # re-check: kill switch may flip during the model call
        return log("skipped_paused", chat=chat)
    send(chat, reply[:1500])


def main():
    STATE.mkdir(exist_ok=True)
    log("start", text="paused" if paused() else "active")
    threading.Thread(target=queue_worker, daemon=True).start()
    threading.Thread(target=watchdog, daemon=True).start()
    while True:
        try:
            with requests.get(f"{GATEWAY}/api/events?topics=*", headers=H, stream=True, timeout=(10, None)) as r:
                r.encoding = "utf-8"  # SSE has no charset header; requests would guess Latin-1 and mangle ₹ / Hindi
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
