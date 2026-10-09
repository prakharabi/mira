"""Workspace "WhatsApp" tab backend: read chats from the open-wa gateway, send manual replies,
mute/unmute the auto-reply bot per chat, and flip its kill switch. State files are shared
with whatsapp_bot.py (daemon/whatsapp_state/)."""
import os
import time
from pathlib import Path

import requests
from fastapi import APIRouter, Body

router = APIRouter(prefix="/whatsapp")
GATEWAY = os.environ.get("WA_GATEWAY", "http://127.0.0.1:8080")
ENV = Path(os.environ.get("WA_GATEWAY_ENV", Path.home() / "Developer/whatsapp-openwa/.env"))
STATE = Path(__file__).parent / "whatsapp_state"


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
