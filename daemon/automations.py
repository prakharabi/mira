"""Automation layer -- run user-defined webhooks (n8n and anything else).

Mira doesn't try to reimplement a workflow engine. The user already runs n8n,
so an automation here is just a named, described webhook that Mira can fire
with a payload. The description is what makes it addressable by voice or chat
("run my daily standup"), so it's treated as part of the automation's identity
rather than as a cosmetic note.
"""

import json
import re
import uuid
import datetime
from pathlib import Path
from urllib.parse import urlparse

import requests

AUTOMATIONS_PATH = Path.home() / "Mira" / "daemon" / "automations.json"
RUN_TIMEOUT = 30

# Words that carry no signal when matching a spoken request to an automation.
_STOPWORDS = {
    "the", "a", "an", "my", "me", "run", "start", "trigger", "execute", "please",
    "can", "you", "mira", "hey", "for", "to", "of", "and", "with", "it", "do",
    "automation", "workflow", "flow", "now", "just", "go", "let", "s",
}


def _load_all() -> list:
    if not AUTOMATIONS_PATH.exists():
        return []
    try:
        data = json.loads(AUTOMATIONS_PATH.read_text())
        return data if isinstance(data, list) else []
    except (json.JSONDecodeError, OSError):
        return []


def _save_all(items: list):
    AUTOMATIONS_PATH.write_text(json.dumps(items, indent=2))


def _public(item: dict) -> dict:
    """Strip the auth header value before anything leaves the daemon."""
    safe = dict(item)
    safe["auth_header_value_set"] = bool(item.get("auth_header_value"))
    safe.pop("auth_header_value", None)
    return safe


def list_automations() -> list:
    return [_public(a) for a in _load_all()]


def get_automation(automation_id: str):
    return next((a for a in _load_all() if a.get("id") == automation_id), None)


def _validate_url(url: str) -> str:
    url = (url or "").strip()
    parsed = urlparse(url)
    # Only http(s): a file:// or similar URL here would turn a "run automation"
    # click into a local file read.
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise ValueError("Webhook URL must be a valid http:// or https:// address")
    return url


def create_automation(name: str, webhook_url: str, description: str = "",
                      method: str = "POST", auth_header_name: str = "",
                      auth_header_value: str = "") -> dict:
    name = (name or "").strip()
    if not name:
        raise ValueError("Automation needs a name")

    method = (method or "POST").upper()
    if method not in ("POST", "GET"):
        raise ValueError("Method must be POST or GET")

    item = {
        "id": uuid.uuid4().hex[:12],
        "name": name[:120],
        "description": (description or "").strip()[:500],
        "webhook_url": _validate_url(webhook_url),
        "method": method,
        "auth_header_name": (auth_header_name or "").strip()[:100],
        "auth_header_value": (auth_header_value or "").strip(),
        "created_at": datetime.datetime.now().isoformat(timespec="seconds"),
        "last_run": "",
        "last_status": "",
    }
    items = _load_all()
    items.append(item)
    _save_all(items)
    return _public(item)


def update_automation(automation_id: str, **fields) -> dict:
    items = _load_all()
    for a in items:
        if a.get("id") != automation_id:
            continue

        if "webhook_url" in fields and fields["webhook_url"] is not None:
            a["webhook_url"] = _validate_url(fields["webhook_url"])
        if fields.get("name"):
            a["name"] = fields["name"].strip()[:120]
        if fields.get("description") is not None:
            a["description"] = fields["description"].strip()[:500]
        if fields.get("method"):
            m = fields["method"].upper()
            if m not in ("POST", "GET"):
                raise ValueError("Method must be POST or GET")
            a["method"] = m
        if fields.get("auth_header_name") is not None:
            a["auth_header_name"] = fields["auth_header_name"].strip()[:100]
        # a blank value means "leave the stored secret alone", so the UI can
        # save an edit without having to re-enter the token every time
        if fields.get("auth_header_value"):
            a["auth_header_value"] = fields["auth_header_value"].strip()

        _save_all(items)
        return _public(a)
    raise KeyError(automation_id)


def delete_automation(automation_id: str) -> bool:
    items = _load_all()
    remaining = [a for a in items if a.get("id") != automation_id]
    if len(remaining) == len(items):
        return False
    _save_all(remaining)
    return True


def run_automation(automation_id: str, payload: dict = None) -> dict:
    items = _load_all()
    target = next((a for a in items if a.get("id") == automation_id), None)
    if target is None:
        raise KeyError(automation_id)

    headers = {}
    if target.get("auth_header_name") and target.get("auth_header_value"):
        headers[target["auth_header_name"]] = target["auth_header_value"]

    body = payload or {}
    body.setdefault("source", "mira")
    body.setdefault("triggered_at", datetime.datetime.now().isoformat(timespec="seconds"))

    try:
        if target["method"] == "GET":
            resp = requests.get(target["webhook_url"], params=body,
                                headers=headers, timeout=RUN_TIMEOUT)
        else:
            resp = requests.post(target["webhook_url"], json=body,
                                 headers=headers, timeout=RUN_TIMEOUT)
        ok = 200 <= resp.status_code < 300
        result = {
            "success": ok,
            "status_code": resp.status_code,
            "response": resp.text[:2000],
        }
    except requests.RequestException as e:
        result = {"success": False, "status_code": None, "error": str(e)}

    target["last_run"] = datetime.datetime.now().isoformat(timespec="seconds")
    target["last_status"] = "success" if result.get("success") else "failed"
    target["last_output"] = (result.get("response") or result.get("error") or "")[:2000]
    _save_all(items)

    result["automation"] = _public(target)
    return result


def _keywords(text: str) -> set:
    words = re.findall(r"[a-z0-9]+", (text or "").lower())
    return {w for w in words if w not in _STOPWORDS and len(w) > 1}


def match_automation(text: str):
    """Find the automation a natural-language request is asking for.

    Deliberately conservative: this can fire real side effects, so an ambiguous
    or weak match returns nothing and lets the caller ask rather than guessing.
    """
    items = _load_all()
    if not items:
        return None

    request_words = _keywords(text)
    if not request_words:
        return None

    scored = []
    for a in items:
        name_words = _keywords(a.get("name", ""))
        desc_words = _keywords(a.get("description", ""))
        if not name_words and not desc_words:
            continue
        # name matches count double -- "run my standup" should pick the
        # automation actually called "standup" over one that merely mentions it
        score = 2 * len(request_words & name_words) + len(request_words & desc_words)
        if name_words and name_words <= request_words:
            score += 3  # whole name present in the request
        if score > 0:
            scored.append((score, a))

    if not scored:
        return None

    scored.sort(key=lambda s: s[0], reverse=True)
    best_score, best = scored[0]
    if best_score < 2:
        return None
    if len(scored) > 1 and scored[1][0] == best_score:
        return None  # tie -- too ambiguous to fire something with side effects
    return _public(best)
