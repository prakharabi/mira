"""Workspace "WhatsApp" tab backend: read chats from the open-wa gateway, send manual replies,
mute/unmute the auto-reply bot per chat, and flip its kill switch. State files are shared
with whatsapp_bot.py (daemon/whatsapp_state/)."""
import json
import os
import subprocess
import threading
import time
from pathlib import Path

import requests
from fastapi import APIRouter, Body

router = APIRouter(prefix="/whatsapp")
GATEWAY = os.environ.get("WA_GATEWAY", "http://127.0.0.1:8080")
ENV = Path(os.environ.get("WA_GATEWAY_ENV", Path.home() / "Developer/whatsapp-openwa/.env"))
STATE = Path(__file__).parent / "whatsapp_state"
SETTINGS = Path(__file__).parent / "settings.json"


def _gw(method, path, **kw):
    key = next(l.split("=", 1)[1].strip().strip("\"'") for l in ENV.read_text().splitlines() if l.startswith("API_KEY="))
    r = requests.request(method, GATEWAY + path, headers={"X-API-Key": key}, timeout=25, **kw)
    return r.json()


def _handed():
    p = STATE / "handed.txt"
    return set(p.read_text().split()) if p.exists() else set()


@router.get("/status")
def status():
    try:
        connected = bool(requests.get(GATEWAY + "/health", timeout=3).json().get("connected"))
    except Exception:
        connected = False
    return {"connected": connected, "auto_reply": not (STATE / "PAUSE").exists()}


def restart_gateway():
    """pm2-restart the open-wa gateway (the saved session survives; a logged-out one comes back showing a fresh QR)."""
    pm2_dir = ENV.parent
    node = subprocess.check_output(["/bin/zsh", "-lc", "command -v node"], text=True).strip()
    env = {**os.environ, "PATH": f"{Path(node).parent}:{os.environ.get('PATH', '')}"}
    subprocess.run([str(pm2_dir / "node_modules/.bin/pm2"), "restart", "wa", "wa-hook"], cwd=pm2_dir, env=env, capture_output=True, timeout=60)


def live_qr():
    """The QR drawn right now on the gateway's WhatsApp Web page (it rotates every ~20 s), read over Chrome's debug port. The gateway's
    own /qr only ever holds the first QR, which WhatsApp invalidates after about a minute, so scanning it fails with "could not link"."""
    import websocket
    port = next(ENV.parent.glob("_IGNORE_*/DevToolsActivePort")).read_text().split("\n")[0]
    page = next(t for t in requests.get(f"http://127.0.0.1:{port}/json/list", timeout=3).json()
                if t["type"] == "page" and "web.whatsapp.com" in t["url"])
    ws = websocket.create_connection(page["webSocketDebuggerUrl"], timeout=5, suppress_origin=True)
    try:
        ws.send(json.dumps({"id": 1, "method": "Runtime.evaluate", "params": {"returnByValue": True,
                "expression": "(document.querySelector('[data-ref]') || {getAttribute: () => null}).getAttribute('data-ref')"}}))
        while True:
            d = json.loads(ws.recv())
            if d.get("id") == 1:
                return d["result"]["result"].get("value")
    finally:
        ws.close()


_qr_restart = 0.0  # when we last restarted the gateway to get a fresh QR


@router.get("/qr")
def qr():
    """QR for linking the number (WhatsApp > Linked devices); the UI polls this. The gateway shows a QR for only ~3 minutes, then
    goes DISCONNECTED and keeps serving the dead code. So a request in that state restarts it (at most every 2 minutes) for a new one."""
    global _qr_restart
    import segno
    try:
        health = requests.get(GATEWAY + "/health", timeout=3).json()
    except Exception:
        return {"connected": False, "svg": None, "state": "GATEWAY_DOWN"}
    state = (health.get("session") or {}).get("state")
    if health.get("connected"):
        return {"connected": True, "svg": None, "state": state}
    code = None
    if state == "AUTHENTICATING":  # only then is the QR live
        try:
            code = live_qr()
        except Exception:
            try:
                code = _gw("GET", "/qr").get("qr")  # fallback: only the first QR, valid for about a minute
            except Exception:
                pass
    elif state == "DISCONNECTED" and time.time() - _qr_restart > 120:
        _qr_restart = time.time()
        threading.Thread(target=restart_gateway, daemon=True).start()
        state = "REFRESHING"
    svg = segno.make(code, error="l").svg_data_uri(scale=6, border=2, dark="#000", light="#fff") if code else None
    return {"connected": False, "svg": svg, "state": state}


@router.post("/auto-reply")
def auto_reply(enabled: bool = Body(..., embed=True)):
    STATE.mkdir(exist_ok=True)
    (STATE / "PAUSE").unlink(missing_ok=True) if enabled else (STATE / "PAUSE").touch()
    return status()


@router.get("/chats")
def chats():
    handed = _handed()
    out = []
    for c in _gw("GET", "/api/chats/getAll").get("data") or []:
        cid = c["id"] if isinstance(c.get("id"), str) else (c.get("id") or {}).get("_serialized", "")
        if not cid.endswith(("@c.us", "@lid")) or cid.startswith("0@"):
            continue  # skip groups, broadcasts, WhatsApp's own system chat
        out.append({"id": cid, "name": c.get("name") or c.get("formattedTitle") or cid.split("@")[0],
                    "unread": c.get("unreadCount") or 0, "t": c.get("t") or 0, "muted": cid in handed})
    return sorted(out, key=lambda c: -c["t"])


@router.get("/messages")
def messages(chat: str):
    ms = _gw("GET", "/api/messages/getAll", params={"chatId": chat}).get("data") or []
    return [{"id": m["id"], "from_me": m["id"].startswith("true_"), "body": m.get("body") or "", "t": m.get("t")}
            for m in ms if m.get("type") == "chat" and (m.get("body") or "").strip()][-100:]


@router.post("/send")
def send(chat: str = Body(...), text: str = Body(...)):
    # ponytail: user-typed reply only; sending to arbitrary numbers isn't exposed on purpose
    if not text.strip() or not chat.endswith(("@c.us", "@lid")):
        return {"error": "bad request"}
    r = _gw("POST", "/api/messages/sendText", json={"to": chat, "content": text.strip()[:4000]})
    with open(STATE / "log.jsonl", "a") as f:
        f.write('{"t": "%s", "kind": "manual_out", "chat": "%s"}\n' % (time.strftime("%F %T"), chat))
    return {"ok": bool(r.get("success")), "error": None if r.get("success") else str(r)[:200]}


@router.post("/mute")
def mute(chat: str = Body(...), muted: bool = Body(...)):
    ids = _handed()
    ids.add(chat) if muted else ids.discard(chat)
    STATE.mkdir(exist_ok=True)
    (STATE / "handed.txt").write_text("".join(i + "\n" for i in sorted(ids)))
    return {"ok": True}


# ---- owner channel: the one number Mira takes commands from and may message unprompted ----
def owner_ids() -> set:
    """Chat ids of the owner. The number comes from whatsapp_state/owner.txt (gitignored); WhatsApp's
    own server maps it to a chat id, so a sender cannot claim to be the owner by name or profile."""
    f = STATE / "owner.txt"
    if not f.exists():
        return set()
    num = f.read_text().strip()
    cache = STATE / "owner_lid.txt"
    ids = {num + "@c.us"}
    if cache.exists() and cache.read_text().split()[:1] == [num]:
        ids.add(cache.read_text().split()[1])
        return ids
    try:
        lid = _gw("GET", "/api/contacts/checkNumberStatus", params={"contactId": num + "@c.us"})["data"]["id"]
        cache.write_text(f"{num} {lid}")
        ids.add(lid)
    except Exception:
        pass
    return ids


def _telegram_owner(text: str) -> bool:
    """Telegram fallback. Reads settings.json directly: this module also runs inside the bot process, which must not import main."""
    try:
        s = json.loads(SETTINGS.read_text())
        token, chat = s.get("telegram_bot_token"), s.get("telegram_owner_chat_id")
        return bool(token and chat and requests.post(f"https://api.telegram.org/bot{token}/sendMessage",
                                                     json={"chat_id": chat, "text": text[:4096]}, timeout=20).ok)
    except Exception:
        return False


def send_to_owner(text: str) -> bool:
    """Alerts / blog notices / handoffs to the owner: Telegram first (WhatsApp keeps getting logged out); WhatsApp only as a
    fallback while it is connected. OWNER_OFF disables that fallback."""
    if _telegram_owner(text):
        return True
    if not (STATE / "OWNER_OFF").exists():
        try:
            if requests.get(GATEWAY + "/health", timeout=3).json().get("connected"):
                ids = [i for i in owner_ids() if i.endswith("@lid")] or [i for i in owner_ids()]
                if _gw("POST", "/api/messages/sendText", json={"to": ids[0], "content": text[:3500]}).get("success"):
                    return True
        except Exception:
            pass
    return False
