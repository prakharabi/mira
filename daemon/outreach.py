"""Outreach -- turning a lead list into conversations.

A campaign is a lead list, the user's offer, and a short sequence of steps
("email on day 0, WhatsApp on day 1, follow-ups on days 4 and 9"). Mira writes
each message from the user's own template -- optionally personalised per
business by the model -- sends it, watches for the reply, and stops the
sequence for anyone who answers. Everything that goes out or comes back, on
any channel, lands in one log, so a business's whole conversation reads as one
timeline.

The flow the user asked for is "test first, then fully automatic":

  * Send test   -- every step, rendered for a couple of real leads, goes to the
                   user's own email/WhatsApp instead of the business.
  * Activate    -- in "auto" mode messages go out by themselves; "review" mode
                   holds each one for approval first.

Pacing lives here, not in the channels: sending only inside the configured
hours and days, a daily cap per channel (email ramps up from WARMUP_START for
a new mailbox), and a random gap between sends. The WhatsApp extension just
asks "what's next?" and gets nothing until it's time.

Storage is JSON under daemon/outreach/ (gitignored), atomic writes, one lock --
the same shape as tasks.py and leads.py, for the same reasons.
"""

import datetime
import json
import random
import re
import secrets
import threading
import uuid
from pathlib import Path

import leads
import outreach_mail as mailbox

OUTREACH_DIR = Path.home() / "Mira" / "daemon" / "outreach"
CONFIG_PATH = OUTREACH_DIR / "config.json"
CAMPAIGNS_DIR = OUTREACH_DIR / "campaigns"
LOG_PATH = OUTREACH_DIR / "log.json"
DNC_PATH = OUTREACH_DIR / "do_not_contact.json"
STATE_PATH = OUTREACH_DIR / "state.json"

TICK_SECONDS = 30
REPLY_POLL_SECONDS = 180
# A WhatsApp message handed to the extension but never confirmed either way
# (browser closed mid-send) goes back in the queue after this long.
HANDOFF_TIMEOUT_SECONDS = 600
MAX_ATTEMPTS = 3
# How many messages per channel are written ahead of being sent. Small in auto
# mode so an edit to a campaign's template still reaches most of its leads;
# larger in review mode so there's a batch to approve in one sitting.
QUEUE_AHEAD = {"auto": 3, "review": 25}
WARMUP_START = 10          # emails/day for a brand-new mailbox...
WARMUP_STEP = 5            # ...plus this many per day since its first send
AUTO_REPLY_DELAY_DAYS = 3  # an out-of-office pushes the next step back, not the whole sequence
MAX_STEPS = 8

CHANNELS = ("email", "whatsapp")
MODES = ("auto", "review")
REPLY_LABELS = ("interested", "question", "not_interested", "unsubscribe", "auto_reply", "other")
LEAD_FILTERS = ("all", "with_email", "with_phone", "no_website")

DEFAULT_CONFIG = {
    # Master switch for automatic sending. Test sends work with it off.
    "enabled": False,
    "email_address": "",
    "email_app_password": "",
    "from_name": "",
    "signature": "",
    "optout_line": "If this isn't relevant, just reply STOP and I won't write again.",
    "smtp_host": "smtp.gmail.com",
    "smtp_port": 465,
    "imap_host": "imap.gmail.com",
    "imap_port": 993,
    "email_daily_cap": 40,
    "warmup": True,
    "whatsapp_enabled": False,
    "whatsapp_daily_cap": 40,
    "whatsapp_token": "",
    "send_hours": [10, 18],          # local time, [start, end)
    "send_days": [0, 1, 2, 3, 4, 5],  # Monday..Saturday
    "min_gap_seconds": 90,
    "max_gap_seconds": 240,
    "test_email": "",
    "test_phone": "",
    "country_code": "91",
    "notify_replies": True,
}
SECRET_KEYS = ("email_app_password", "whatsapp_token")

# The sequence the user asked for. Placeholders are filled per lead; see
# PLACEHOLDERS. A follow-up email with no subject is sent as a reply in the
# same thread as the first email, the way a person would follow up.
DEFAULT_STEPS = [
    {"day": 0, "channel": "email", "subject": "Quick question for {business}",
     "body": "Hi {business} team,\n\nI came across {business} while looking at "
             "{business_type} in {area}.\n\n{offer}\n\n{proof}\n\n{cta}\n\nBest,\n{my_name}"},
    {"day": 1, "channel": "whatsapp", "subject": "",
     "body": "Hi {business} team, this is {my_name}. {offer} {cta}"},
    {"day": 4, "channel": "email", "subject": "",
     "body": "Hi again, just bringing this back to the top of your inbox in case it "
             "got buried.\n\n{cta}\n\n{my_name}"},
    {"day": 9, "channel": "email", "subject": "",
     "body": "Hi, I'll leave it here so I don't crowd your inbox. If this becomes a "
             "priority for {business} later, just reply to this email and I'll pick "
             "it up.\n\nBest,\n{my_name}"},
]

PLACEHOLDERS = {
    "business": "the business's name",
    "business_type": "what you searched for, e.g. dentists",
    "category": "its Google Maps category, e.g. Dental clinic",
    "area": "the area you searched",
    "rating": "its Google rating, e.g. 4.6",
    "reviews": "its number of Google reviews",
    "website": "its website",
    "my_name": "your name (Setup > From name)",
    "offer": "the campaign's offer",
    "proof": "the campaign's proof point",
    "cta": "the campaign's call to action",
}

_lock = threading.RLock()
_stop = threading.Event()
_thread = None
_RENDERING = "__rendering__"


def log(message: str):
    print(f"[outreach] {message}", flush=True)


def _now() -> datetime.datetime:
    return datetime.datetime.now()


def _iso(dt: datetime.datetime) -> str:
    return dt.isoformat(timespec="seconds")


def _parse(value: str):
    try:
        return datetime.datetime.fromisoformat(value) if value else None
    except (TypeError, ValueError):
        return None


def _read(path: Path, default):
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text())
    except (json.JSONDecodeError, OSError):
        return default


def _write(path: Path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False))
    tmp.replace(path)  # atomic -- a crash mid-write must not lose who was already contacted


# ---------- config ----------

def load_config() -> dict:
    cfg = dict(DEFAULT_CONFIG)
    cfg.update(_read(CONFIG_PATH, {}))
    return cfg


def public_config() -> dict:
    """Config for the UI: secrets replaced by whether they're set."""
    cfg = load_config()
    for key in SECRET_KEYS:
        cfg[key + "_set"] = bool(cfg.get(key))
        cfg.pop(key, None)
    return cfg


def update_config(changes: dict) -> dict:
    with _lock:
        cfg = load_config()
        for key, value in (changes or {}).items():
            if key not in DEFAULT_CONFIG or key == "whatsapp_token":
                continue
            if value is None:
                continue
            if key in SECRET_KEYS and value == "":
                continue  # blank means "keep the saved one", as in every other key field
            default = DEFAULT_CONFIG[key]
            if isinstance(default, bool):
                value = bool(value)
            elif isinstance(default, int):
                value = int(value)
            elif isinstance(default, str):
                value = str(value).strip()
            cfg[key] = value

        start, end = (list(cfg.get("send_hours") or [10, 18]) + [18])[:2]
        start, end = max(0, min(int(start), 23)), max(1, min(int(end), 24))
        cfg["send_hours"] = [start, end] if start < end else [10, 18]
        cfg["send_days"] = sorted({int(d) for d in cfg.get("send_days", []) if 0 <= int(d) <= 6}) \
            or [0, 1, 2, 3, 4, 5]
        cfg["email_daily_cap"] = max(1, min(cfg["email_daily_cap"], 500))
        cfg["whatsapp_daily_cap"] = max(1, min(cfg["whatsapp_daily_cap"], 500))
        cfg["min_gap_seconds"] = max(10, cfg["min_gap_seconds"])
        cfg["max_gap_seconds"] = max(cfg["min_gap_seconds"], cfg["max_gap_seconds"])
        cfg["country_code"] = re.sub(r"\D", "", cfg["country_code"]) or "91"
        if cfg["email_address"] and not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", cfg["email_address"]):
            raise ValueError("that doesn't look like an email address")
        _write(CONFIG_PATH, cfg)
    return public_config()


def whatsapp_token(rotate: bool = False) -> str:
    """The shared secret the extension sends as X-Mira-Token. The daemon
    listens on localhost with no auth of its own, and without this any web page
    open in the browser could post fake replies into the log."""
    with _lock:
        cfg = load_config()
        if rotate or not cfg.get("whatsapp_token"):
            cfg["whatsapp_token"] = secrets.token_urlsafe(24)
            _write(CONFIG_PATH, cfg)
        return cfg["whatsapp_token"]


def check_token(token: str) -> bool:
    expected = load_config().get("whatsapp_token") or ""
    return bool(expected) and secrets.compare_digest(expected, token or "")


def _load_state() -> dict:
    return _read(STATE_PATH, {})


def _save_state(state: dict):
    _write(STATE_PATH, state)


# ---------- people: phones, do-not-contact ----------

def normalize_phone(raw: str, country_code: str = "91") -> str:
    """Digits in international form without the '+', e.g. 919845012345 --
    what WhatsApp addresses a number by. Maps gives '080 2222 3333' or
    '098450 12345'; a leading 0 there is the domestic trunk prefix."""
    raw = (raw or "").strip()
    digits = re.sub(r"\D", "", raw)
    if not digits:
        return ""
    if raw.startswith("+"):
        return digits
    digits = digits.lstrip("0")
    if len(digits) == 10:
        return country_code + digits
    return digits


def phone_key(raw: str) -> str:
    digits = re.sub(r"\D", "", raw or "")
    return digits[-10:] if len(digits) >= 8 else ""


def _load_dnc() -> dict:
    data = _read(DNC_PATH, {})
    return {"emails": data.get("emails", []), "phones": data.get("phones", [])}


def is_suppressed(lead: dict, dnc: dict = None) -> bool:
    dnc = dnc or _load_dnc()
    if phone_key(lead.get("phone")) and phone_key(lead.get("phone")) in dnc["phones"]:
        return True
    return any(e.lower() in dnc["emails"] for e in lead.get("emails") or [])


def suppress(lead: dict):
    """Never contact this business again, from any list, on any channel."""
    with _lock:
        dnc = _load_dnc()
        key = phone_key(lead.get("phone"))
        if key and key not in dnc["phones"]:
            dnc["phones"].append(key)
        for e in lead.get("emails") or []:
            if e.lower() not in dnc["emails"]:
                dnc["emails"].append(e.lower())
        _write(DNC_PATH, dnc)


# ---------- the log ----------

def _load_log() -> list:
    data = _read(LOG_PATH, [])
    return data if isinstance(data, list) else []


def _save_log(entries: list):
    _write(LOG_PATH, entries)


def _log_add(entry: dict) -> dict:
    with _lock:
        entries = _load_log()
        entry.setdefault("id", uuid.uuid4().hex[:16])
        entry.setdefault("created_at", _iso(_now()))
        entries.append(entry)
        _save_log(entries)
    return entry


def _log_update(msg_id: str, **fields):
    with _lock:
        entries = _load_log()
        for e in entries:
            if e["id"] == msg_id:
                e.update(fields)
                _save_log(entries)
                return e
    return None


def _log_get(msg_id: str):
    return next((e for e in _load_log() if e["id"] == msg_id), None)


def _sent_today(channel: str) -> int:
    today = _now().date().isoformat()
    return sum(1 for e in _load_log()
               if e.get("direction") == "out" and e.get("channel") == channel
               and e.get("status") == "sent" and not e.get("test")
               and (e.get("sent_at") or "").startswith(today))


def _email_cap(cfg: dict, state: dict) -> int:
    cap = cfg["email_daily_cap"]
    first = _parse(state.get("first_email_at", ""))
    if cfg.get("warmup"):
        days = (_now() - first).days if first else 0
        cap = min(cap, WARMUP_START + WARMUP_STEP * days)
    return cap


# ---------- campaigns ----------

def _campaign_path(campaign_id: str) -> Path:
    if not re.fullmatch(r"[a-f0-9]{8,32}", campaign_id or ""):
        raise ValueError("invalid campaign id")
    return CAMPAIGNS_DIR / f"{campaign_id}.json"


def _load_campaign(campaign_id: str):
    try:
        return _read(_campaign_path(campaign_id), None)
    except ValueError:
        return None


def _save_campaign(c: dict):
    c["updated_at"] = _iso(_now())
    _write(_campaign_path(c["id"]), c)


def _all_campaigns() -> list:
    if not CAMPAIGNS_DIR.exists():
        return []
    out = []
    for p in CAMPAIGNS_DIR.glob("*.json"):
        c = _read(p, None)
        if c:
            out.append(c)
    out.sort(key=lambda c: c.get("created_at", ""), reverse=True)
    return out


def _validate_steps(steps) -> list:
    if not isinstance(steps, list) or not steps:
        raise ValueError("a campaign needs at least one step")
    if len(steps) > MAX_STEPS:
        raise ValueError(f"at most {MAX_STEPS} steps")
    clean, last_day, seen_email_subject = [], 0, False
    for i, s in enumerate(steps):
        channel = (s.get("channel") or "").strip().lower()
        if channel not in CHANNELS:
            raise ValueError(f"step {i + 1}: channel must be email or whatsapp")
        day = int(s.get("day") or 0)
        if day < last_day:
            raise ValueError(f"step {i + 1}: days must not go backwards")
        body = (s.get("body") or "").strip()
        if not body:
            raise ValueError(f"step {i + 1}: the message is empty")
        subject = (s.get("subject") or "").strip() if channel == "email" else ""
        unknown = sorted(set(_PLACEHOLDER_RE.findall(body + " " + subject)) - set(PLACEHOLDERS))
        if unknown:
            raise ValueError(f"step {i + 1}: unknown placeholder {{{unknown[0]}}} -- "
                             f"available: {', '.join('{' + k + '}' for k in PLACEHOLDERS)}")
        if channel == "email" and not subject and not seen_email_subject:
            raise ValueError(f"step {i + 1}: the first email needs a subject "
                             "(later ones can leave it blank to reply in the same thread)")
        seen_email_subject = seen_email_subject or bool(subject)
        clean.append({"day": day, "channel": channel, "subject": subject, "body": body})
        last_day = day
    return clean


_EDITABLE = ("name", "list_id", "mode", "lead_filter", "min_rating", "personalize",
             "offer", "proof", "cta", "instructions", "steps")


def _apply_fields(c: dict, data: dict):
    for key in _EDITABLE:
        if key not in data or data[key] is None:
            continue
        value = data[key]
        if key == "steps":
            value = _validate_steps(value)
        elif key == "mode":
            if value not in MODES:
                raise ValueError("mode must be auto or review")
        elif key == "lead_filter":
            if value not in LEAD_FILTERS:
                raise ValueError("unknown lead filter")
        elif key == "min_rating":
            value = float(value or 0)
        elif key == "personalize":
            value = bool(value)
        elif key == "list_id":
            if c.get("enrollments") and value != c.get("list_id"):
                raise ValueError("the lead list can't change once a campaign has started")
            if not leads.get_list(value):
                raise ValueError("that lead list doesn't exist")
        else:
            value = str(value).strip()
            if key in ("offer", "proof", "cta"):
                unknown = sorted(set(_PLACEHOLDER_RE.findall(value)) - set(PLACEHOLDERS)
                                 | ({"offer", "proof", "cta"} & set(_PLACEHOLDER_RE.findall(value))))
                if unknown:
                    raise ValueError(f"{key}: {{{unknown[0]}}} can't be used here")
        c[key] = value
    if not c.get("name"):
        raise ValueError("give the campaign a name")


def create_campaign(data: dict) -> dict:
    c = {
        "id": uuid.uuid4().hex[:12], "name": "", "list_id": "", "status": "draft",
        "mode": "auto", "lead_filter": "all", "min_rating": 0.0, "personalize": True,
        "offer": "", "proof": "", "cta": "", "instructions": "",
        "steps": [dict(s) for s in DEFAULT_STEPS],
        "created_at": _iso(_now()), "activated_at": "", "tested_at": "", "finished_at": "",
        "enrollments": {},
    }
    _apply_fields(c, data or {})
    with _lock:
        _save_campaign(c)
    return campaign_view(c)


def update_campaign(campaign_id: str, data: dict) -> dict:
    with _lock:
        c = _load_campaign(campaign_id)
        if not c:
            raise KeyError(campaign_id)
        _apply_fields(c, data or {})
        _save_campaign(c)
    return campaign_view(c)


def delete_campaign(campaign_id: str) -> bool:
    """The campaign goes; its log entries stay, since they're the record of
    who was actually contacted."""
    with _lock:
        c = _load_campaign(campaign_id)
        if not c:
            return False
        _cancel_pending(lambda e: e.get("campaign_id") == campaign_id)
        _campaign_path(campaign_id).unlink(missing_ok=True)
    return True


def find_campaign(ref: str = ""):
    items = _all_campaigns()
    if not items:
        return None
    ref = (ref or "").strip().lower()
    if not ref or ref in ("latest", "last", "current"):
        active = [c for c in items if c["status"] == "active"]
        return (active or items)[0]
    for c in items:
        if c["id"] == ref or c["id"].startswith(ref) or c["name"].lower() == ref:
            return c
    words = set(re.findall(r"[a-z0-9]+", ref)) - {"the", "campaign", "my"}
    scored = [(len(words & set(re.findall(r"[a-z0-9]+", c["name"].lower()))), c) for c in items]
    scored = [s for s in scored if s[0] > 0]
    return max(scored, key=lambda s: s[0])[1] if scored else None


def _stats(c: dict, entries: list) -> dict:
    ens = c.get("enrollments", {}).values()
    mine = [e for e in entries if e.get("campaign_id") == c["id"] and not e.get("test")]
    replies = [e for e in mine if e.get("direction") == "in" and e.get("label") != "bounce"]
    return {
        "enrolled": len(c.get("enrollments", {})),
        "in_progress": sum(1 for e in ens if e["status"] == "active"),
        "finished_sequence": sum(1 for e in ens if e["status"] == "done"),
        "replied": sum(1 for e in ens if e["status"] == "replied"),
        "stopped": sum(1 for e in ens if e["status"] in ("stopped", "do_not_contact", "unreachable")),
        "emails_sent": sum(1 for e in mine if e.get("direction") == "out"
                           and e.get("channel") == "email" and e.get("status") == "sent"),
        "whatsapp_sent": sum(1 for e in mine if e.get("direction") == "out"
                             and e.get("channel") == "whatsapp" and e.get("status") == "sent"),
        "replies": len(replies),
        "interested": sum(1 for e in replies if e.get("label") == "interested"),
        "unsubscribed": sum(1 for e in replies if e.get("label") == "unsubscribe"),
        "bounced": sum(1 for e in mine if e.get("label") == "bounce"),
        "pending_approval": sum(1 for e in mine if e.get("status") == "pending_approval"),
    }


def campaign_view(c: dict, entries: list = None) -> dict:
    out = {k: v for k, v in c.items() if k != "enrollments"}
    lst = leads.get_list(c.get("list_id", "")) if c.get("list_id") else None
    out["list_name"] = lst.get("name", "") if lst else ""
    out["stats"] = _stats(c, entries if entries is not None else _load_log())
    return out


def list_campaigns() -> list:
    entries = _load_log()
    return [campaign_view(c, entries) for c in _all_campaigns()]


def get_campaign(campaign_id: str):
    c = _load_campaign(campaign_id)
    return campaign_view(c) if c else None


def campaign_leads(campaign_id: str) -> list:
    """Per-lead progress for the campaign screen."""
    c = _load_campaign(campaign_id)
    if not c:
        return []
    index = _lead_index(c["list_id"])
    entries = _load_log()
    last_by_lead = {}
    for e in entries:
        if e.get("campaign_id") == campaign_id and not e.get("test"):
            last_by_lead[e["lead_id"]] = e
    rows = []
    for lead_id, en in c.get("enrollments", {}).items():
        lead = index.get(lead_id, {})
        last = last_by_lead.get(lead_id, {})
        rows.append({
            "lead_id": lead_id, "business": lead.get("name", "?"), "stage": lead.get("stage", ""),
            "status": en["status"], "step": min(en["step"] + 1, len(c["steps"])),
            "steps": len(c["steps"]), "next_due": en.get("next_due", ""),
            "last": {"channel": last.get("channel"), "direction": last.get("direction"),
                     "status": last.get("status"), "label": last.get("label"),
                     "at": last.get("sent_at") or last.get("created_at")} if last else None,
        })
    order = {"replied": 0, "active": 1, "done": 2}
    rows.sort(key=lambda r: (order.get(r["status"], 3), r["business"].lower()))
    return rows


# ---------- leads ----------

def _lead_index(list_id: str) -> dict:
    record = leads.get_list(list_id) if list_id else None
    return {l["id"]: l for l in (record or {}).get("leads", [])}


def _list_record(list_id: str) -> dict:
    return leads.get_list(list_id) or {}


def _lead_key(lead: dict) -> str:
    return phone_key(lead.get("phone")) or ",".join(sorted(e.lower() for e in lead.get("emails") or []))


def _matches_filter(c: dict, lead: dict) -> bool:
    f = c.get("lead_filter", "all")
    if f == "with_email" and not lead.get("emails"):
        return False
    if f == "with_phone" and not lead.get("phone"):
        return False
    if f == "no_website" and lead.get("website"):
        return False
    if c.get("min_rating") and (lead.get("rating") or 0) < c["min_rating"]:
        return False
    return True


def _target(step: dict, lead: dict, cfg: dict) -> str:
    """Where this step's message would go for this lead, or "" if nowhere."""
    if step["channel"] == "email":
        bad = {e.lower() for e in lead.get("bad_emails") or []}
        return next((e for e in lead.get("emails") or [] if e.lower() not in bad), "")
    if not cfg.get("whatsapp_enabled") or lead.get("no_whatsapp"):
        return ""
    return normalize_phone(lead.get("phone"), cfg["country_code"])


# ---------- writing messages ----------

_PLACEHOLDER_RE = re.compile(r"\{(\w+)\}")
_LINK_RE = re.compile(r"(?:https?://|www\.)\S+|\b[\w-]+\.(?:com|in|io|co|me|app|link|so|org|net)/\S*", re.I)


def _facts(c: dict, lead: dict, record: dict, cfg: dict) -> dict:
    my_name = cfg.get("from_name", "")
    if not my_name:
        try:
            from main import load_settings  # local import: main imports this module
            my_name = load_settings().get("owner_name", "")
        except Exception:
            my_name = ""
    rating = lead.get("rating")
    facts = {
        "business": lead.get("name", ""),
        "business_type": record.get("business_type", "") or lead.get("category", ""),
        "category": lead.get("category", ""),
        "area": record.get("area", ""),
        "rating": f"{rating:g}" if isinstance(rating, (int, float)) and rating else "",
        "reviews": str(lead.get("reviews") or ""),
        "website": lead.get("website", ""),
        "my_name": my_name,
    }
    # The offer, proof and call to action are written once per campaign but
    # naturally mention the business ("3 reel ideas for {business}"), so they
    # get filled in too -- from the plain facts only, so nothing can recurse.
    for key in ("offer", "proof", "cta"):
        facts[key] = _fill(c.get(key, ""), facts)
    return facts


def _fill(template: str, facts: dict) -> str:
    text = _PLACEHOLDER_RE.sub(lambda m: str(facts.get(m.group(1), m.group(0))), template or "")
    # an empty {proof} leaves a blank paragraph behind; don't send it that way
    text = re.sub(r"[ \t]+\n", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return re.sub(r"[ \t]{2,}", " ", text).strip()


def _llm(prompt: str) -> str:
    from main import route_prompt  # local import: main imports this module
    reply, _model = route_prompt(prompt, "auto")
    return (reply or "").strip()


def _personalize(draft: str, subject: str, channel: str, facts: dict, c: dict) -> tuple:
    """Have the model rewrite the filled-in template for this one business.
    Anything that looks off -- a dropped link, a missing name, a reply that's
    far longer than the draft -- falls back to the template itself: a plain
    message is fine, a wrong one is not."""
    known = {k: v for k, v in facts.items()
             if v and k in ("business", "category", "area", "rating", "reviews", "website")}
    wants_subject = channel == "email" and bool(subject)
    prompt = (
        f"You are writing a short cold outreach {'email' if channel == 'email' else 'WhatsApp message'} "
        f"on behalf of {facts.get('my_name') or 'the sender'}.\n"
        "Rewrite the DRAFT so it reads as written for this specific business.\n"
        "Rules:\n"
        "- Keep the offer, the call to action and every link exactly as they are.\n"
        "- Keep the sender's name. Keep it the same length or shorter. Plain text, no markdown.\n"
        "- Use at most one or two facts from BUSINESS FACTS. Never invent anything not listed.\n"
        "- Write in the same language as the draft.\n"
        + (f"- {c['instructions']}\n" if c.get("instructions") else "")
        + ("Reply with the subject on the first line as 'SUBJECT: ...', then the message.\n"
           if wants_subject else "Reply with only the message.\n")
        + f"\nBUSINESS FACTS: {json.dumps(known, ensure_ascii=False)}\n"
        + (f"\nDRAFT SUBJECT: {subject}\n" if wants_subject else "")
        + f"\nDRAFT:\n{draft}\n"
    )
    out = _llm(prompt)
    new_subject = subject
    if wants_subject:
        m = re.match(r"\s*subject:\s*(.+)\n", out, re.I)
        if m:
            new_subject = m.group(1).strip().strip('"')
            out = out[m.end():]
    out = out.strip().strip('"').strip()
    out = re.sub(r"^(here(?:'s| is)[^\n]*:\s*\n)", "", out, flags=re.I).strip()

    if not out or len(out) > max(400, 2 * len(draft)):
        raise ValueError("personalised text was empty or too long")
    for link in _LINK_RE.findall(draft):
        if link.rstrip(".,)") not in out:
            raise ValueError(f"personalised text dropped the link {link}")
    if facts.get("my_name") and facts["my_name"] in draft and facts["my_name"] not in out:
        raise ValueError("personalised text dropped the sender's name")
    return out, new_subject


def render(c: dict, step_index: int, lead: dict, record: dict, cfg: dict,
           personalize: bool = None) -> dict:
    step = c["steps"][step_index]
    facts = _facts(c, lead, record, cfg)
    body = _fill(step["body"], facts)
    subject = _fill(step.get("subject", ""), facts)
    personalized, note = False, ""
    if personalize if personalize is not None else c.get("personalize"):
        try:
            body, subject = _personalize(body, subject, step["channel"], facts, c)
            personalized = True
        except Exception as e:
            note = f"sent as written: {e}"
    if step["channel"] == "email":
        tail = [p for p in (cfg.get("signature", "").strip(), cfg.get("optout_line", "").strip()) if p]
        if tail:
            body = body + "\n\n" + "\n\n".join(tail)
    first_subject = next((_fill(s["subject"], facts) for s in c["steps"]
                          if s["channel"] == "email" and s.get("subject")), "")
    return {"subject": subject, "fallback_subject": first_subject, "text": body,
            "personalized": personalized, "note": note}


def preview(campaign_id: str, lead_id: str = "", personalize: bool = None) -> dict:
    c = _load_campaign(campaign_id)
    if not c:
        raise KeyError(campaign_id)
    record = _list_record(c["list_id"])
    pool = [l for l in record.get("leads", []) if _matches_filter(c, l)]
    lead = next((l for l in pool if l["id"] == lead_id), None)
    if not lead:
        sample = _sample_leads(pool, 1)
        lead = sample[0] if sample else None
    if not lead:
        raise ValueError("the lead list has no leads matching this campaign's filter")
    cfg = load_config()
    steps = []
    for i, step in enumerate(c["steps"]):
        r = render(c, i, lead, record, cfg, personalize)
        subject = r["subject"] or (f"Re: {r['fallback_subject']}" if step["channel"] == "email" else "")
        steps.append({"day": step["day"], "channel": step["channel"], "subject": subject,
                      "text": r["text"], "personalized": r["personalized"], "note": r["note"]})
    return {"business": lead["name"], "lead_id": lead["id"], "steps": steps}


def _sample_leads(pool: list, n: int) -> list:
    # the most informative samples first: ones that exercise both channels
    ranked = sorted(pool, key=lambda l: (not l.get("emails"), not l.get("phone")))
    return ranked[:n]


# ---------- test sends ----------

def send_test(campaign_id: str, samples: int = 2) -> dict:
    """Every step, rendered for real leads, sent to the user's own email and
    WhatsApp instead. What arrives is exactly what a business would get --
    only the subject says [TEST] and who it was written for."""
    c = _load_campaign(campaign_id)
    if not c:
        raise KeyError(campaign_id)
    cfg = load_config()
    record = _list_record(c["list_id"])
    pool = [l for l in record.get("leads", []) if _matches_filter(c, l)]
    if not pool:
        raise ValueError("the lead list has no leads matching this campaign's filter")
    has_email = any(s["channel"] == "email" for s in c["steps"])
    has_wa = any(s["channel"] == "whatsapp" for s in c["steps"])
    notes = []
    if has_email and not cfg.get("test_email"):
        raise ValueError("Add a test email address in Outreach > Setup first.")
    if has_wa and not (cfg.get("whatsapp_enabled") and cfg.get("test_phone")):
        notes.append("WhatsApp steps weren't sent: turn on WhatsApp and add a test number in Setup.")

    sent_email, queued_wa, previews = 0, 0, []
    for lead in _sample_leads(pool, samples):
        for i, step in enumerate(c["steps"]):
            r = render(c, i, lead, record, cfg)
            label = f"[TEST · {lead['name']} · step {i + 1}]"
            if step["channel"] == "email":
                subject = r["subject"] or f"Re: {r['fallback_subject']}"
                message_id = mailbox.send(cfg, cfg["test_email"], f"{label} {subject}", r["text"])
                _log_add({"campaign_id": c["id"], "list_id": c["list_id"], "lead_id": lead["id"],
                          "step": i, "channel": "email", "direction": "out", "test": True,
                          "to": cfg["test_email"], "subject": subject, "text": r["text"],
                          "status": "sent", "sent_at": _iso(_now()), "external_id": message_id})
                sent_email += 1
            elif cfg.get("whatsapp_enabled") and cfg.get("test_phone"):
                _log_add({"campaign_id": c["id"], "list_id": c["list_id"], "lead_id": lead["id"],
                          "step": i, "channel": "whatsapp", "direction": "out", "test": True,
                          "to": normalize_phone(cfg["test_phone"], cfg["country_code"]),
                          "text": f"{label}\n{r['text']}", "status": "queued", "attempts": 0})
                queued_wa += 1
            previews.append({"business": lead["name"], "step": i + 1, "channel": step["channel"],
                             "personalized": r["personalized"], "note": r["note"]})
    with _lock:
        c = _load_campaign(campaign_id)
        c["tested_at"] = _iso(_now())
        _save_campaign(c)
    if queued_wa:
        notes.append("Test WhatsApp messages are queued for the extension to send.")
    return {"emails_sent": sent_email, "whatsapp_queued": queued_wa,
            "previews": previews, "notes": notes}


# ---------- starting and stopping ----------

def activate(campaign_id: str, mode: str = None, skip_test: bool = False) -> dict:
    with _lock:
        c = _load_campaign(campaign_id)
        if not c:
            raise KeyError(campaign_id)
        if c["status"] == "active":
            return {"campaign": campaign_view(c), "enrolled": 0, "warnings": []}
        if mode:
            if mode not in MODES:
                raise ValueError("mode must be auto or review")
            c["mode"] = mode
        if not c.get("tested_at") and not skip_test:
            raise ValueError("Send a test first, so you see exactly what businesses will get.")
        c["steps"] = _validate_steps(c["steps"])
        record = _list_record(c["list_id"])
        if not record:
            raise ValueError("this campaign's lead list no longer exists")
        if record.get("status") in ("running", "enriching"):
            raise ValueError("the lead search for this list is still running")

        enrolled = 0
        if not c.get("enrollments"):
            cfg, dnc = load_config(), _load_dnc()
            busy = set()
            for other in _all_campaigns():
                if other["id"] == c["id"] or other["status"] not in ("active", "paused"):
                    continue
                oidx = _lead_index(other["list_id"])
                for lid, en in other.get("enrollments", {}).items():
                    if en["status"] == "active" and lid in oidx:
                        busy.add(_lead_key(oidx[lid]))
            now = _iso(_now())
            for lead in record.get("leads", []):
                if not _matches_filter(c, lead) or is_suppressed(lead, dnc):
                    continue
                if lead.get("stage", "new") not in ("new", "contacted"):
                    continue  # already replied, won, lost or opted out
                if _lead_key(lead) in busy:
                    continue  # another running campaign already has them
                if not any(_target(s, lead, cfg) for s in c["steps"]):
                    continue  # no email, and no phone or WhatsApp is off
                c["enrollments"][lead["id"]] = {
                    "status": "active", "step": 0, "started_at": now, "next_due": now,
                    "pending_message": None, "last_sent_at": "", "last_sent_day": 0,
                    "thread": None,
                }
                enrolled += 1
            if not enrolled:
                raise ValueError("no leads in this list can be contacted (no email/phone, "
                                 "already contacted, or on the do-not-contact list)")
        c["status"] = "active"
        c["activated_at"] = c.get("activated_at") or _iso(_now())
        _save_campaign(c)

    cfg = load_config()
    warnings = []
    if not cfg["enabled"]:
        warnings.append("Outreach sending is switched off in Setup, so nothing will go out until you turn it on.")
    if not (cfg["email_address"] and cfg["email_app_password"]) and any(
            s["channel"] == "email" for s in c["steps"]):
        warnings.append("The outreach mailbox isn't set up, so email steps can't send.")
    if not cfg["whatsapp_enabled"] and any(s["channel"] == "whatsapp" for s in c["steps"]):
        warnings.append("WhatsApp is off, so WhatsApp steps will be skipped.")
    return {"campaign": campaign_view(c), "enrolled": enrolled, "warnings": warnings}


def pause(campaign_id: str) -> dict:
    with _lock:
        c = _load_campaign(campaign_id)
        if not c:
            raise KeyError(campaign_id)
        if c["status"] == "active":
            c["status"] = "paused"
            _save_campaign(c)
    return campaign_view(c)


def set_enabled(enabled: bool) -> dict:
    return update_config({"enabled": bool(enabled)})


# ---------- review queue ----------

def pending(campaign_id: str = "") -> list:
    entries = [e for e in _load_log() if e.get("status") == "pending_approval"
               and (not campaign_id or e.get("campaign_id") == campaign_id)]
    names = {}
    for e in entries:
        if e["list_id"] not in names:
            names[e["list_id"]] = _lead_index(e["list_id"])
        e["business"] = names[e["list_id"]].get(e["lead_id"], {}).get("name", "?")
    return entries


def approve(msg_id: str, text: str = None, subject: str = None) -> dict:
    with _lock:
        msg = _log_get(msg_id)
        if not msg or msg.get("status") != "pending_approval":
            raise KeyError(msg_id)
        fields = {"status": "queued", "approved_at": _iso(_now())}
        if text is not None and text.strip():
            fields["text"] = text.strip()
        if subject is not None and msg.get("channel") == "email":
            fields["subject"] = subject.strip()
        return _log_update(msg_id, **fields)


def approve_all(campaign_id: str = "") -> int:
    with _lock:
        entries = _load_log()
        n = 0
        for e in entries:
            if e.get("status") == "pending_approval" and (not campaign_id or e.get("campaign_id") == campaign_id):
                e["status"], e["approved_at"] = "queued", _iso(_now())
                n += 1
        _save_log(entries)
    return n


def skip(msg_id: str):
    with _lock:
        msg = _log_get(msg_id)
        if not msg or msg.get("status") not in ("pending_approval", "queued"):
            raise KeyError(msg_id)
        _log_update(msg_id, status="skipped")
        _after_step(msg, sent=False)


def _cancel_pending(pred):
    with _lock:
        entries = _load_log()
        changed = False
        for e in entries:
            if e.get("status") in ("pending_approval", "queued", "handed_off") and pred(e):
                e["status"] = "cancelled"
                changed = True
        if changed:
            _save_log(entries)


# ---------- sequence bookkeeping ----------

def _next_due(c: dict, en: dict, now: datetime.datetime) -> str:
    """The first message goes out as soon as it can; every later step is
    spaced from when the previous one actually went out, so a backlog on a
    busy day doesn't bunch a lead's follow-ups together."""
    steps = c["steps"]
    if en["step"] >= len(steps):
        return ""
    if not en.get("last_sent_at"):
        return _iso(now)
    delta = steps[en["step"]]["day"] - en.get("last_sent_day", 0)
    return _iso(_parse(en["last_sent_at"]) + datetime.timedelta(days=max(delta, 0)))


def _finish_step(c: dict, en: dict, sent: bool, now: datetime.datetime):
    if sent:
        en["last_sent_at"] = _iso(now)
        en["last_sent_day"] = c["steps"][en["step"]]["day"]
    en["step"] += 1
    en["pending_message"] = None
    if en["step"] >= len(c["steps"]):
        en["status"] = "done" if en.get("last_sent_at") else "unreachable"
        en["next_due"] = ""
    else:
        en["next_due"] = _next_due(c, en, now)


def _after_step(msg: dict, sent: bool, external_id: str = ""):
    """A message went out (or was skipped / failed for good): move that lead's
    sequence on, remember the email thread, and mark the lead contacted."""
    if msg.get("test"):
        return
    now = _now()
    with _lock:
        c = _load_campaign(msg["campaign_id"])
        if not c:
            return
        en = c["enrollments"].get(msg["lead_id"])
        if not en or en.get("pending_message") != msg["id"]:
            return
        if sent and msg["channel"] == "email" and external_id:
            thread = en.get("thread") or {"subject": msg.get("subject") or msg.get("fallback_subject", ""),
                                          "ids": []}
            thread["ids"].append(external_id)
            en["thread"] = thread
        _finish_step(c, en, sent, now)
        _save_campaign(c)
    if sent:
        index = _lead_index(msg["list_id"])
        if index.get(msg["lead_id"], {}).get("stage", "new") == "new":
            leads.update_lead(msg["list_id"], msg["lead_id"], stage="contacted")
        state = _load_state()
        if msg["channel"] == "email" and not state.get("first_email_at"):
            state["first_email_at"] = _iso(now)
            _save_state(state)


def _mark_sent(msg: dict, external_id: str = ""):
    _log_update(msg["id"], status="sent", sent_at=_iso(_now()), external_id=external_id)
    _after_step(msg, sent=True, external_id=external_id)


def _mark_failed(msg: dict, error: str, permanent: bool):
    attempts = int(msg.get("attempts") or 0) + 1
    if permanent or attempts >= MAX_ATTEMPTS:
        _log_update(msg["id"], status="failed", attempts=attempts, error=error)
        _after_step(msg, sent=False)
    else:
        _log_update(msg["id"], status="queued", attempts=attempts, error=error)


# ---------- the scheduler ----------

def _in_window(cfg: dict, now: datetime.datetime = None) -> bool:
    now = now or _now()
    start, end = cfg["send_hours"]
    return now.weekday() in cfg["send_days"] and start <= now.hour < end


def _prepare_due(cfg: dict):
    """Write the messages that are due. Three phases so the model call that
    personalises a message never runs while the lock is held: reserve the
    enrollments, render them, then record the messages."""
    now = _now()
    jobs = []
    with _lock:
        entries = _load_log()
        dnc = _load_dnc()
        for c in _all_campaigns():
            if c["status"] != "active":
                continue
            ahead = {ch: sum(1 for e in entries if e.get("campaign_id") == c["id"]
                             and e.get("channel") == ch and not e.get("test")
                             and e.get("status") in ("queued", "pending_approval", "handed_off"))
                     for ch in CHANNELS}
            limit = QUEUE_AHEAD[c.get("mode", "auto")]
            index = _lead_index(c["list_id"])
            changed = False
            due = sorted(((lid, en) for lid, en in c["enrollments"].items()
                          if en["status"] == "active" and not en.get("pending_message")
                          and (_parse(en.get("next_due")) or now) <= now),
                         key=lambda x: x[1].get("next_due", ""))
            for lead_id, en in due:
                lead = index.get(lead_id)
                if not lead:
                    en["status"], changed = "stopped", True
                    continue
                if is_suppressed(lead, dnc) or lead.get("stage") == "do_not_contact":
                    en["status"], changed = "do_not_contact", True
                    continue
                # skip steps this lead can't receive (no email, WhatsApp off...)
                while en["status"] == "active" and en["step"] < len(c["steps"]) \
                        and not _target(c["steps"][en["step"]], lead, cfg) \
                        and (_parse(en.get("next_due")) or now) <= now:
                    _finish_step(c, en, sent=False, now=now)
                    changed = True
                if en["status"] != "active" or (_parse(en.get("next_due")) or now) > now:
                    continue
                channel = c["steps"][en["step"]]["channel"]
                if ahead[channel] >= limit:
                    continue
                ahead[channel] += 1
                en["pending_message"] = _RENDERING
                changed = True
                jobs.append((c["id"], lead_id, en["step"]))
            if changed:
                _save_campaign(c)

    for campaign_id, lead_id, step_index in jobs:
        try:
            c = _load_campaign(campaign_id)
            record = _list_record(c["list_id"])
            lead = _lead_index(c["list_id"]).get(lead_id)
            r = render(c, step_index, lead, record, cfg)
            target = _target(c["steps"][step_index], lead, cfg)
        except Exception as e:
            log(f"could not write message for {lead_id}: {e}")
            r, target = None, ""
        with _lock:
            c = _load_campaign(campaign_id)
            if not c:
                continue
            en = c["enrollments"].get(lead_id)
            if not en or en.get("pending_message") != _RENDERING:
                continue
            if not r or not target or c["status"] != "active":
                en["pending_message"] = None  # try again on a later tick
                _save_campaign(c)
                continue
            step = c["steps"][step_index]
            msg = _log_add({
                "campaign_id": c["id"], "list_id": c["list_id"], "lead_id": lead_id,
                "step": step_index, "channel": step["channel"], "direction": "out",
                "to": target, "subject": r["subject"], "fallback_subject": r["fallback_subject"],
                "text": r["text"], "personalized": r["personalized"], "note": r["note"],
                "status": "pending_approval" if c.get("mode") == "review" else "queued",
                "attempts": 0,
            })
            en["pending_message"] = msg["id"]
            _save_campaign(c)


def _send_next_email(cfg: dict):
    state = _load_state()
    now = _now()
    ready_at = _parse(state.get("next_email_at", ""))
    if ready_at and now < ready_at:
        return
    if _sent_today("email") >= _email_cap(cfg, state):
        return
    active = {c["id"]: c for c in _all_campaigns() if c["status"] == "active"}
    msg = next((e for e in _load_log() if e.get("channel") == "email" and e.get("status") == "queued"
                and not e.get("test") and e.get("campaign_id") in active), None)
    if not msg:
        return

    en = active[msg["campaign_id"]]["enrollments"].get(msg["lead_id"], {})
    thread = en.get("thread")
    subject, in_reply_to, refs = msg.get("subject"), "", None
    if not subject:
        if thread and thread.get("ids"):
            subject = f"Re: {thread['subject']}"
            in_reply_to, refs = thread["ids"][-1], thread["ids"]
        else:
            subject = msg.get("fallback_subject") or "Hello"

    state["next_email_at"] = _iso(now + datetime.timedelta(
        seconds=random.randint(cfg["min_gap_seconds"], cfg["max_gap_seconds"])))
    _save_state(state)
    try:
        external_id = mailbox.send(cfg, msg["to"], subject, msg["text"], in_reply_to, refs)
    except mailbox.BadRecipient as e:
        index = _lead_index(msg["list_id"])
        bad = list(index.get(msg["lead_id"], {}).get("bad_emails") or []) + [msg["to"]]
        leads.update_lead(msg["list_id"], msg["lead_id"], bad_emails=bad)
        _mark_failed(msg, str(e), permanent=True)
        return
    except mailbox.MailboxError as e:
        _mark_failed(msg, str(e), permanent=False)
        _alert_once(f"Outreach email couldn't send: {e}")
        return
    except Exception as e:  # a malformed address etc. -- count it, don't retry forever
        _mark_failed(msg, str(e), permanent=False)
        return
    _log_update(msg["id"], subject=subject)
    msg["subject"] = subject
    _mark_sent(msg, external_id)
    if state.get("mailbox_error"):
        state = _load_state()
        state.pop("mailbox_error", None)
        _save_state(state)


def _requeue_stale_handoffs():
    cutoff = _now() - datetime.timedelta(seconds=HANDOFF_TIMEOUT_SECONDS)
    with _lock:
        entries = _load_log()
        changed = False
        for e in entries:
            if e.get("status") == "handed_off" and (_parse(e.get("handed_at")) or cutoff) <= cutoff:
                e["status"] = "queued"
                changed = True
        if changed:
            _save_log(entries)


def _finish_campaigns():
    notices = []
    with _lock:
        for c in _all_campaigns():
            if c["status"] != "active" or not c.get("enrollments"):
                continue
            if any(en["status"] == "active" for en in c["enrollments"].values()):
                continue
            c["status"] = "finished"
            c["finished_at"] = _iso(_now())
            _save_campaign(c)
            s = _stats(c, _load_log())
            notices.append(f"📣 Campaign “{c['name']}” finished: {s['emails_sent']} emails and "
                           f"{s['whatsapp_sent']} WhatsApp messages sent, {s['replies']} replies "
                           f"({s['interested']} interested).")
    for text in notices:  # outside the lock: delivery may speak aloud and take seconds
        _notify(text)


def tick():
    cfg = load_config()
    _requeue_stale_handoffs()
    # Replies are read even with sending switched off -- pausing outreach
    # must not mean missing the answers to what already went out.
    if any(c.get("enrollments") for c in _all_campaigns()):
        try:
            _poll_replies(cfg)
        except Exception as e:
            log(f"reply check failed: {e}")
    if not cfg["enabled"]:
        return
    try:
        _prepare_due(cfg)
    except Exception as e:
        log(f"preparing messages failed: {e}")
    if _in_window(cfg) and cfg.get("email_address") and cfg.get("email_app_password"):
        try:
            _send_next_email(cfg)
        except Exception as e:
            log(f"sending failed: {e}")
    _finish_campaigns()


def _loop():
    while not _stop.is_set():
        try:
            tick()
        except Exception as e:
            log(f"tick failed: {e}")
        _stop.wait(TICK_SECONDS)


def start_background():
    global _thread
    if _thread and _thread.is_alive():
        return
    # A restart mid-render leaves reservations nobody will ever complete.
    with _lock:
        for c in _all_campaigns():
            stuck = [en for en in c.get("enrollments", {}).values()
                     if en.get("pending_message") == _RENDERING]
            for en in stuck:
                en["pending_message"] = None
            if stuck:
                _save_campaign(c)
    _stop.clear()
    _thread = threading.Thread(target=_loop, daemon=True, name="outreach")
    _thread.start()


def stop_background():
    _stop.set()


# ---------- replies ----------

_STOP_RE = re.compile(r"^\W*(stop|unsubscribe|remove me|remove|opt ?out|don'?t (message|email|contact) me)\W*$", re.I)


def classify(text: str, auto_reply: bool = False) -> str:
    if auto_reply:
        return "auto_reply"
    t = (text or "").strip()
    if not t:
        return "other"
    if _STOP_RE.match(t) or "unsubscribe" in t.lower():
        return "unsubscribe"
    try:
        out = _llm(
            "Classify this reply to a sales outreach message. Answer with exactly one label:\n"
            "interested - wants to talk, asks for pricing/details, says yes\n"
            "question - asks something without saying yes or no\n"
            "not_interested - declines\n"
            "unsubscribe - asks not to be contacted again\n"
            "auto_reply - out of office or automatic response\n"
            "other - anything else\n\n"
            f"REPLY:\n{t[:1500]}\n\nLabel:").lower().replace("-", "_").replace(" ", "_")
        # longest first: "interested" is a substring of "not_interested"
        for label in sorted(REPLY_LABELS, key=len, reverse=True):
            if label in out:
                return label
    except Exception as e:
        log(f"reply classification fell back: {e}")
    return "other"


def _find_enrollment(pred):
    """(campaign, lead_id, lead) for the most recently active campaign whose
    lead matches `pred(lead)`."""
    for c in _all_campaigns():  # newest first
        index = _lead_index(c["list_id"])
        for lead_id in c.get("enrollments", {}):
            lead = index.get(lead_id)
            if lead and pred(lead):
                return c, lead_id, lead
    return None, None, None


def _record_reply(c: dict, lead_id: str, lead: dict, channel: str, sender: str,
                  text: str, label: str, external_id: str = ""):
    cfg = load_config()
    list_id = c["list_id"]
    entry = _log_add({"campaign_id": c["id"], "list_id": list_id, "lead_id": lead_id,
                      "channel": channel, "direction": "in", "from": sender, "text": text,
                      "label": label, "status": "received", "external_id": external_id,
                      "received_at": _iso(_now())})
    stage = {"interested": "interested", "question": "replied", "other": "replied",
             "not_interested": "lost", "unsubscribe": "do_not_contact"}.get(label)
    with _lock:
        c = _load_campaign(c["id"])
        en = c["enrollments"].get(lead_id) if c else None
        if en and label == "auto_reply":
            if en["status"] == "active" and not en.get("pending_message"):
                due = max(_parse(en.get("next_due")) or _now(), _now())
                en["next_due"] = _iso(due + datetime.timedelta(days=AUTO_REPLY_DELAY_DAYS))
        elif en and label != "bounce":
            en["status"] = "do_not_contact" if label == "unsubscribe" else "replied"
            en["pending_message"] = None
        if c:
            _save_campaign(c)
        if stage:
            # a human answered: nothing else queued for them should go out
            _cancel_pending(lambda e: e.get("lead_id") == lead_id and e.get("list_id") == list_id)
    if stage:
        leads.update_lead(list_id, lead_id, stage=stage)
    if label == "unsubscribe":
        suppress(lead)
    if label in ("interested", "question", "other") and cfg.get("notify_replies"):
        icon = "🔥" if label == "interested" else "💬"
        snippet = re.sub(r"\s+", " ", text)[:220]
        _notify(f"{icon} {lead.get('name', 'A lead')} replied on {channel} "
                f"({label.replace('_', ' ')}): “{snippet}”")
    return entry


def _poll_replies(cfg: dict):
    if not (cfg.get("email_address") and cfg.get("email_app_password")):
        return
    state = _load_state()
    last = _parse(state.get("last_reply_poll", ""))
    if last and (_now() - last).total_seconds() < REPLY_POLL_SECONDS:
        return
    state["last_reply_poll"] = _iso(_now())
    _save_state(state)
    messages, cursor = mailbox.fetch_new(cfg, state.get("imap_cursor"))
    state = _load_state()
    state["imap_cursor"] = cursor
    _save_state(state)
    for m in messages:
        try:
            handle_inbound_email(m)
        except Exception as e:
            log(f"could not process reply from {m.get('from_addr')}: {e}")


def handle_inbound_email(m: dict):
    entries = _load_log()
    by_external = {e.get("external_id"): e for e in entries
                   if e.get("direction") == "out" and e.get("external_id") and not e.get("test")}

    if m.get("bounce"):
        failed = set(m.get("bounced_recipients") or [])
        for mid in m.get("bounced_message_ids") or []:
            if mid in by_external:
                failed.add(by_external[mid].get("to", "").lower())
        for addr in failed:
            c, lead_id, lead = _find_enrollment(
                lambda l, a=addr: a in [e.lower() for e in l.get("emails") or []])
            if not c:
                continue
            bad = list(dict.fromkeys((lead.get("bad_emails") or []) + [addr]))
            leads.update_lead(c["list_id"], lead_id, bad_emails=bad)
            _log_add({"campaign_id": c["id"], "list_id": c["list_id"], "lead_id": lead_id,
                      "channel": "email", "direction": "in", "from": m.get("from_addr"),
                      "text": f"Bounced: {addr}", "label": "bounce", "status": "received",
                      "received_at": _iso(_now())})
        return

    if m.get("message_id") and any(e.get("external_id") == m["message_id"] for e in entries):
        return  # already processed
    original = next((by_external[i] for i in (m.get("in_reply_to") or []) + (m.get("references") or [])
                     if i in by_external), None)
    if original:
        c = _load_campaign(original["campaign_id"])
        lead_id = original["lead_id"]
        lead = _lead_index(original["list_id"]).get(lead_id)
    else:
        sender = m.get("from_addr", "")
        c, lead_id, lead = _find_enrollment(
            lambda l: sender and sender in [e.lower() for e in l.get("emails") or []])
    if not c or not lead:
        return  # not a reply to outreach
    label = classify(m.get("text", ""), m.get("auto_reply", False))
    _record_reply(c, lead_id, lead, "email", m.get("from_addr", ""), m.get("text", ""),
                  label, m.get("message_id", ""))


# ---------- WhatsApp bridge (the extension's side of the contract) ----------

def bridge_next() -> dict:
    """The next WhatsApp message the extension should send, or why there
    isn't one yet. Pacing, hours and caps are all decided here."""
    cfg = load_config()
    now = _now()
    with _lock:
        state = _load_state()
        state["bridge_last_seen"] = _iso(now)
        _save_state(state)
        if not cfg.get("whatsapp_enabled"):
            return {"message": None, "reason": "WhatsApp outreach is off in Mira"}
        entries = _load_log()
        queued = [e for e in entries if e.get("channel") == "whatsapp" and e.get("status") == "queued"]
        msg = next((e for e in queued if e.get("test")), None)
        if not msg:
            if not cfg["enabled"]:
                return {"message": None, "reason": "outreach sending is switched off"}
            if not _in_window(cfg, now):
                return {"message": None, "reason": "outside sending hours"}
            if _sent_today("whatsapp") >= cfg["whatsapp_daily_cap"]:
                return {"message": None, "reason": "daily limit reached"}
            ready_at = _parse(state.get("next_whatsapp_at", ""))
            if ready_at and now < ready_at:
                return {"message": None, "reason": "waiting between messages",
                        "retry_after": int((ready_at - now).total_seconds())}
            active = {c["id"] for c in _all_campaigns() if c["status"] == "active"}
            msg = next((e for e in queued if not e.get("test") and e.get("campaign_id") in active), None)
            if not msg:
                return {"message": None, "reason": "nothing due"}
            state["next_whatsapp_at"] = _iso(now + datetime.timedelta(
                seconds=random.randint(cfg["min_gap_seconds"], cfg["max_gap_seconds"])))
            _save_state(state)
        _log_update(msg["id"], status="handed_off", handed_at=_iso(now))
    business = _lead_index(msg["list_id"]).get(msg["lead_id"], {}).get("name", "")
    return {"message": {"id": msg["id"], "phone": msg["to"], "text": msg["text"],
                        "business": business, "test": bool(msg.get("test"))}}


def bridge_result(msg_id: str, ok: bool, error: str = "", not_on_whatsapp: bool = False) -> dict:
    msg = _log_get(msg_id)
    if not msg or msg.get("status") not in ("handed_off", "queued"):
        raise KeyError(msg_id)
    if ok:
        _mark_sent(msg)
    elif not_on_whatsapp:
        if not msg.get("test"):
            leads.update_lead(msg["list_id"], msg["lead_id"], no_whatsapp=True)
        _mark_failed(msg, error or "not on WhatsApp", permanent=True)
    else:
        _mark_failed(msg, error or "send failed", permanent=False)
    return {"ok": True}


def bridge_incoming(phone: str, text: str, wa_id: str = "", sender_name: str = "") -> dict:
    """A WhatsApp message the extension saw arrive. Matched to a lead by the
    last ten digits of the number; anything from someone who isn't a lead in
    a campaign is ignored, since the extension may see every chat."""
    if wa_id and any(e.get("external_id") == wa_id for e in _load_log()):
        return {"matched": True, "duplicate": True}
    key = phone_key(phone)
    if not key:
        return {"matched": False}
    c, lead_id, lead = _find_enrollment(lambda l: phone_key(l.get("phone")) == key)
    if not c:
        return {"matched": False}
    label = classify(text)
    entry = _record_reply(c, lead_id, lead, "whatsapp", sender_name or phone, text, label, wa_id)
    return {"matched": True, "business": lead.get("name"), "label": label, "log_id": entry["id"]}


# ---------- reading ----------

def replies(limit: int = 50, label: str = "") -> list:
    items = [e for e in _load_log() if e.get("direction") == "in" and not e.get("test")
             and e.get("label") != "bounce" and (not label or e.get("label") == label)]
    items.sort(key=lambda e: e.get("received_at", ""), reverse=True)
    items = items[:limit]
    cache = {}
    for e in items:
        if e["list_id"] not in cache:
            cache[e["list_id"]] = _lead_index(e["list_id"])
        lead = cache[e["list_id"]].get(e["lead_id"], {})
        e["business"] = lead.get("name", "?")
        e["phone"] = lead.get("phone", "")
        e["stage"] = lead.get("stage", "")
    return items


def timeline(list_id: str, lead_id: str) -> dict:
    lead = _lead_index(list_id).get(lead_id)
    if not lead:
        raise KeyError(lead_id)
    items = [e for e in _load_log() if e.get("lead_id") == lead_id and e.get("list_id") == list_id]
    items.sort(key=lambda e: e.get("sent_at") or e.get("received_at") or e.get("created_at", ""))
    return {"lead": lead, "messages": items}


def set_stage(list_id: str, lead_id: str, stage: str) -> dict:
    if stage not in leads.STAGES:
        raise ValueError(f"stage must be one of {', '.join(leads.STAGES)}")
    lead = leads.update_lead(list_id, lead_id, stage=stage)
    if not lead:
        raise KeyError(lead_id)
    if stage in ("replied", "interested", "proposal_sent", "won", "lost", "do_not_contact"):
        with _lock:
            for c in _all_campaigns():
                en = c.get("enrollments", {}).get(lead_id)
                if c["list_id"] == list_id and en and en["status"] == "active":
                    en["status"] = "do_not_contact" if stage == "do_not_contact" else "stopped"
                    en["pending_message"] = None
                    _save_campaign(c)
            _cancel_pending(lambda e: e.get("lead_id") == lead_id and e.get("list_id") == list_id)
    if stage == "do_not_contact":
        suppress(lead)
    return lead


def overview() -> dict:
    cfg, state = load_config(), _load_state()
    entries = _load_log()
    week_ago = _iso(_now() - datetime.timedelta(days=7))
    seen = _parse(state.get("bridge_last_seen", ""))
    return {
        "enabled": cfg["enabled"],
        "mailbox_configured": bool(cfg["email_address"] and cfg["email_app_password"]),
        "mailbox_address": cfg["email_address"],
        "whatsapp_enabled": cfg["whatsapp_enabled"],
        "extension_connected": bool(seen and (_now() - seen).total_seconds() < 300),
        "extension_last_seen": state.get("bridge_last_seen", ""),
        "in_sending_hours": _in_window(cfg),
        "today": {"email": _sent_today("email"), "email_cap": _email_cap(cfg, state),
                  "whatsapp": _sent_today("whatsapp"), "whatsapp_cap": cfg["whatsapp_daily_cap"]},
        "pending_approval": sum(1 for e in entries if e.get("status") == "pending_approval"),
        "replies_7d": sum(1 for e in entries if e.get("direction") == "in" and not e.get("test")
                          and e.get("label") not in ("bounce", "auto_reply")
                          and e.get("received_at", "") >= week_ago),
        "campaigns": [{"id": c["id"], "name": c["name"], "status": c["status"], "mode": c.get("mode")}
                      for c in _all_campaigns()],
    }


# ---------- notifications ----------

def _notify(text: str):
    try:
        import proactive
        proactive.deliver([text])
    except Exception as e:
        log(f"notify failed: {e}")


def _alert_once(text: str):
    """Tell the user about a sending problem once, not once per tick."""
    with _lock:
        state = _load_state()
        if state.get("mailbox_error") == text:
            return
        state["mailbox_error"] = text
        _save_state(state)
    _notify(f"⚠️ {text}")
