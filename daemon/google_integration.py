"""Google service integrations (Gmail, Calendar, Drive).

Mira is meant to be open-sourced, so it deliberately does NOT ship an OAuth
client secret. Each user creates their own Google Cloud OAuth client ("Desktop
app" type) and pastes the ID/secret into Settings. That keeps the project
distributable without leaking shared credentials, and means a user's data only
ever moves between their own machine and their own Google project.

Auth uses the loopback redirect flow with PKCE, which is Google's recommended
approach for installed/desktop apps. Tokens live in a gitignored local file.

The Google REST endpoints are called directly with `requests` rather than
through `google-api-python-client` -- the daemon already depends on requests
everywhere else, and the handful of calls used here don't justify pulling in
the much larger client library and its transitive deps.
"""

import base64
import hashlib
import json
import os
import secrets
import threading
import time
import urllib.parse
import datetime
from email.mime.text import MIMEText
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import requests

TOKENS_PATH = Path.home() / "Mira" / "daemon" / "google_tokens.json"

AUTH_ENDPOINT = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_ENDPOINT = "https://oauth2.googleapis.com/token"
REVOKE_ENDPOINT = "https://oauth2.googleapis.com/revoke"
USERINFO_ENDPOINT = "https://www.googleapis.com/oauth2/v2/userinfo"

GMAIL_API = "https://gmail.googleapis.com/gmail/v1"
CALENDAR_API = "https://www.googleapis.com/calendar/v3"
DRIVE_API = "https://www.googleapis.com/drive/v3"

# gmail.compose lets Mira create DRAFTS. Sending is deliberately left to the
# user inside Gmail -- an assistant that can silently send mail on your behalf
# is a much bigger trust ask than one that prepares a draft for you to review.
SCOPES = [
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/gmail.compose",
    "https://www.googleapis.com/auth/calendar",
    "https://www.googleapis.com/auth/drive.readonly",
    "https://www.googleapis.com/auth/userinfo.email",
]

REQUEST_TIMEOUT = 20

# module-level state for an in-progress browser consent flow
_flow_lock = threading.Lock()
_flow_state = {
    "active": False,
    "error": None,
    "started_at": None,
}


# ---------- token storage ----------

def _load_tokens() -> dict:
    if not TOKENS_PATH.exists():
        return {}
    try:
        return json.loads(TOKENS_PATH.read_text())
    except (json.JSONDecodeError, OSError):
        return {}


def _save_tokens(tokens: dict):
    TOKENS_PATH.write_text(json.dumps(tokens, indent=2))
    # Refresh tokens are long-lived credentials to the user's mail and calendar;
    # don't leave them world-readable in a home directory.
    try:
        os.chmod(TOKENS_PATH, 0o600)
    except OSError:
        pass


def clear_tokens():
    tokens = _load_tokens()
    refresh = tokens.get("refresh_token")
    if refresh:
        try:
            requests.post(REVOKE_ENDPOINT, data={"token": refresh}, timeout=REQUEST_TIMEOUT)
        except requests.RequestException:
            # Best-effort: even if Google can't be reached, the local copy must go.
            pass
    try:
        TOKENS_PATH.unlink()
    except FileNotFoundError:
        pass


def is_connected() -> bool:
    return bool(_load_tokens().get("refresh_token"))


def auth_status() -> dict:
    tokens = _load_tokens()
    with _flow_lock:
        flow = dict(_flow_state)
    return {
        "connected": bool(tokens.get("refresh_token")),
        "email": tokens.get("email", ""),
        "connected_at": tokens.get("connected_at", ""),
        "flow_active": flow["active"],
        "flow_error": flow["error"],
    }


# ---------- OAuth (loopback + PKCE) ----------

def _pkce_pair():
    verifier = base64.urlsafe_b64encode(secrets.token_bytes(64)).decode().rstrip("=")
    challenge = base64.urlsafe_b64encode(
        hashlib.sha256(verifier.encode()).digest()
    ).decode().rstrip("=")
    return verifier, challenge


_SUCCESS_HTML = b"""<!doctype html><meta charset="utf-8">
<title>Mira connected</title>
<style>body{font-family:-apple-system,system-ui,sans-serif;background:#0e0e12;color:#e8e8ef;
display:flex;align-items:center;justify-content:center;height:100vh;margin:0}
.box{text-align:center}.t{font-size:20px;font-weight:600;margin-bottom:8px}
.s{opacity:.6;font-size:14px}</style>
<div class="box"><div class="t">Google account connected</div>
<div class="s">You can close this tab and go back to Mira.</div></div>"""

_FAILURE_HTML = b"""<!doctype html><meta charset="utf-8">
<title>Mira</title>
<style>body{font-family:-apple-system,system-ui,sans-serif;background:#0e0e12;color:#e8e8ef;
display:flex;align-items:center;justify-content:center;height:100vh;margin:0}
.box{text-align:center}.t{font-size:20px;font-weight:600;margin-bottom:8px}
.s{opacity:.6;font-size:14px}</style>
<div class="box"><div class="t">Connection failed</div>
<div class="s">Go back to Mira for details.</div></div>"""


def start_auth_flow(client_id: str, client_secret: str) -> dict:
    """Start the browser consent flow. Returns the URL the frontend should open.

    A short-lived loopback HTTP server receives Google's redirect. It runs in a
    background thread so this call returns immediately and the UI can poll
    `auth_status()` instead of blocking on the user's browser interaction.
    """
    if not client_id or not client_secret:
        raise ValueError("Google client ID and secret are required")

    with _flow_lock:
        if _flow_state["active"]:
            raise RuntimeError("A Google sign-in is already in progress")
        _flow_state.update(active=True, error=None, started_at=time.time())

    verifier, challenge = _pkce_pair()
    expected_state = secrets.token_urlsafe(24)

    # port 0 = let the OS pick a free port; Google allows any loopback port
    server = HTTPServer(("127.0.0.1", 0), _make_handler(
        client_id, client_secret, verifier, expected_state
    ))
    port = server.server_address[1]
    redirect_uri = f"http://127.0.0.1:{port}/callback"
    server.redirect_uri = redirect_uri

    params = {
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": " ".join(SCOPES),
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        "state": expected_state,
        "access_type": "offline",
        # force the consent screen so a refresh_token is always returned --
        # Google omits it on repeat authorizations otherwise, which silently
        # breaks reconnecting after a disconnect
        "prompt": "consent",
    }
    auth_url = f"{AUTH_ENDPOINT}?{urllib.parse.urlencode(params)}"

    def serve():
        # one request is all this server exists for; the timeout stops an
        # abandoned sign-in from leaving a listening socket open forever
        server.timeout = 300
        try:
            server.handle_request()
        except Exception as e:
            with _flow_lock:
                _flow_state["error"] = str(e)
        finally:
            server.server_close()
            with _flow_lock:
                if _flow_state["active"]:
                    if not _load_tokens().get("refresh_token") and not _flow_state["error"]:
                        _flow_state["error"] = "Sign-in timed out or was cancelled."
                    _flow_state["active"] = False

    threading.Thread(target=serve, daemon=True).start()
    return {"auth_url": auth_url, "port": port}


def _make_handler(client_id, client_secret, verifier, expected_state):
    class CallbackHandler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass  # keep the daemon log clean

        def do_GET(self):
            parsed = urllib.parse.urlparse(self.path)
            if not parsed.path.startswith("/callback"):
                self.send_response(404)
                self.end_headers()
                return

            qs = urllib.parse.parse_qs(parsed.query)
            error = qs.get("error", [None])[0]
            code = qs.get("code", [None])[0]
            state = qs.get("state", [None])[0]

            def fail(msg):
                with _flow_lock:
                    _flow_state["error"] = msg
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.end_headers()
                self.wfile.write(_FAILURE_HTML)

            if error:
                return fail(f"Google returned an error: {error}")
            # A mismatched state means the redirect didn't originate from the
            # request this server started -- treat it as hostile, not as a bug.
            if not state or not secrets.compare_digest(state, expected_state):
                return fail("State mismatch -- ignoring this callback.")
            if not code:
                return fail("No authorization code in Google's response.")

            try:
                resp = requests.post(TOKEN_ENDPOINT, data={
                    "code": code,
                    "client_id": client_id,
                    "client_secret": client_secret,
                    "redirect_uri": self.server.redirect_uri,
                    "grant_type": "authorization_code",
                    "code_verifier": verifier,
                }, timeout=REQUEST_TIMEOUT)
            except requests.RequestException as e:
                return fail(f"Could not reach Google: {e}")

            if resp.status_code != 200:
                return fail(f"Token exchange failed: {resp.text[:300]}")

            payload = resp.json()
            refresh_token = payload.get("refresh_token")
            if not refresh_token:
                return fail("Google did not return a refresh token.")

            tokens = {
                "refresh_token": refresh_token,
                "access_token": payload.get("access_token", ""),
                "expires_at": time.time() + payload.get("expires_in", 3600) - 60,
                "client_id": client_id,
                "client_secret": client_secret,
                "connected_at": datetime.datetime.now().isoformat(timespec="seconds"),
            }

            try:
                info = requests.get(
                    USERINFO_ENDPOINT,
                    headers={"Authorization": f"Bearer {tokens['access_token']}"},
                    timeout=REQUEST_TIMEOUT,
                )
                if info.status_code == 200:
                    tokens["email"] = info.json().get("email", "")
            except requests.RequestException:
                tokens["email"] = ""

            _save_tokens(tokens)
            with _flow_lock:
                _flow_state["error"] = None

            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(_SUCCESS_HTML)

    return CallbackHandler


def get_access_token() -> str:
    tokens = _load_tokens()
    refresh = tokens.get("refresh_token")
    if not refresh:
        raise RuntimeError("Google account not connected")

    if tokens.get("access_token") and time.time() < tokens.get("expires_at", 0):
        return tokens["access_token"]

    resp = requests.post(TOKEN_ENDPOINT, data={
        "refresh_token": refresh,
        "client_id": tokens.get("client_id", ""),
        "client_secret": tokens.get("client_secret", ""),
        "grant_type": "refresh_token",
    }, timeout=REQUEST_TIMEOUT)

    if resp.status_code != 200:
        raise RuntimeError(f"Could not refresh Google token: {resp.text[:200]}")

    payload = resp.json()
    tokens["access_token"] = payload.get("access_token", "")
    tokens["expires_at"] = time.time() + payload.get("expires_in", 3600) - 60
    _save_tokens(tokens)
    return tokens["access_token"]


def _api_get(url: str, params: dict = None):
    token = get_access_token()
    resp = requests.get(url, params=params or {},
                        headers={"Authorization": f"Bearer {token}"},
                        timeout=REQUEST_TIMEOUT)
    if resp.status_code != 200:
        raise RuntimeError(f"Google API error {resp.status_code}: {resp.text[:300]}")
    return resp.json()


def _api_post(url: str, body: dict, params: dict = None):
    token = get_access_token()
    resp = requests.post(url, json=body, params=params or {},
                         headers={"Authorization": f"Bearer {token}"},
                         timeout=REQUEST_TIMEOUT)
    if resp.status_code not in (200, 201):
        raise RuntimeError(f"Google API error {resp.status_code}: {resp.text[:300]}")
    return resp.json()


# ---------- Gmail ----------

def _header(headers: list, name: str) -> str:
    for h in headers:
        if h.get("name", "").lower() == name.lower():
            return h.get("value", "")
    return ""


def _decode_b64url(data: str) -> str:
    if not data:
        return ""
    padding = "=" * (-len(data) % 4)
    try:
        return base64.urlsafe_b64decode(data + padding).decode("utf-8", errors="replace")
    except (ValueError, TypeError):
        return ""


def _extract_body(payload: dict) -> str:
    """Walk a Gmail MIME tree for the best plain-text body."""
    if not payload:
        return ""

    mime = payload.get("mimeType", "")
    body_data = payload.get("body", {}).get("data")

    if mime == "text/plain" and body_data:
        return _decode_b64url(body_data)

    parts = payload.get("parts") or []
    # prefer a real text/plain part anywhere in the tree before falling back to HTML
    for part in parts:
        found = _extract_body(part)
        if found:
            return found

    if mime == "text/html" and body_data:
        import re
        html = _decode_b64url(body_data)
        text = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", html, flags=re.DOTALL | re.I)
        text = re.sub(r"<[^>]+>", " ", text)
        text = re.sub(r"\s+", " ", text)
        return text.strip()

    return ""


def gmail_list(query: str = "", max_results: int = 15, label: str = "INBOX") -> list:
    params = {"maxResults": max(1, min(max_results, 50))}
    if query:
        params["q"] = query
    if label:
        params["labelIds"] = label

    listing = _api_get(f"{GMAIL_API}/users/me/messages", params)
    messages = listing.get("messages", []) or []

    out = []
    for m in messages:
        detail = _api_get(
            f"{GMAIL_API}/users/me/messages/{m['id']}",
            {"format": "metadata",
             "metadataHeaders": ["From", "Subject", "Date"]},
        )
        headers = detail.get("payload", {}).get("headers", [])
        out.append({
            "id": detail.get("id"),
            "threadId": detail.get("threadId"),
            "from": _header(headers, "From"),
            "subject": _header(headers, "Subject") or "(no subject)",
            "date": _header(headers, "Date"),
            "snippet": detail.get("snippet", ""),
            "unread": "UNREAD" in (detail.get("labelIds") or []),
        })
    return out


def gmail_get(message_id: str) -> dict:
    detail = _api_get(f"{GMAIL_API}/users/me/messages/{message_id}", {"format": "full"})
    headers = detail.get("payload", {}).get("headers", [])
    return {
        "id": detail.get("id"),
        "from": _header(headers, "From"),
        "to": _header(headers, "To"),
        "subject": _header(headers, "Subject") or "(no subject)",
        "date": _header(headers, "Date"),
        "body": _extract_body(detail.get("payload", {}))[:20000],
        "snippet": detail.get("snippet", ""),
    }


def gmail_create_draft(to: str, subject: str, body: str, thread_id: str = None) -> dict:
    msg = MIMEText(body or "", _charset="utf-8")
    msg["To"] = to or ""
    msg["Subject"] = subject or ""
    raw = base64.urlsafe_b64encode(msg.as_bytes()).decode()

    payload = {"message": {"raw": raw}}
    if thread_id:
        payload["message"]["threadId"] = thread_id

    created = _api_post(f"{GMAIL_API}/users/me/drafts", payload)
    return {"draft_id": created.get("id"), "message_id": created.get("message", {}).get("id")}


# ---------- Calendar ----------

def calendar_list_events(days: int = 7, max_results: int = 25) -> list:
    now = datetime.datetime.now(datetime.timezone.utc)
    end = now + datetime.timedelta(days=max(1, min(days, 90)))

    data = _api_get(f"{CALENDAR_API}/calendars/primary/events", {
        "timeMin": now.isoformat().replace("+00:00", "Z"),
        "timeMax": end.isoformat().replace("+00:00", "Z"),
        "singleEvents": "true",
        "orderBy": "startTime",
        "maxResults": max(1, min(max_results, 100)),
    })

    out = []
    for ev in data.get("items", []) or []:
        start = ev.get("start", {})
        end_at = ev.get("end", {})
        out.append({
            "id": ev.get("id"),
            "summary": ev.get("summary") or "(no title)",
            "start": start.get("dateTime") or start.get("date") or "",
            "end": end_at.get("dateTime") or end_at.get("date") or "",
            "all_day": "date" in start,
            "location": ev.get("location", ""),
            "attendees": [a.get("email", "") for a in (ev.get("attendees") or [])],
            "link": ev.get("htmlLink", ""),
        })
    return out


def calendar_create_event(summary: str, start_iso: str, end_iso: str = None,
                          description: str = "", location: str = "",
                          attendees: list = None, add_meet: bool = False) -> dict:
    if not summary or not start_iso:
        raise ValueError("summary and start are required")

    if not end_iso:
        # default to a 1-hour block when the caller only knows the start
        try:
            start_dt = datetime.datetime.fromisoformat(start_iso.replace("Z", "+00:00"))
            end_iso = (start_dt + datetime.timedelta(hours=1)).isoformat()
        except ValueError:
            raise ValueError("start must be an ISO 8601 datetime")

    tz = datetime.datetime.now().astimezone().tzname() or "UTC"
    body = {
        "summary": summary,
        "description": description or "",
        "location": location or "",
        "start": {"dateTime": start_iso, "timeZone": tz},
        "end": {"dateTime": end_iso, "timeZone": tz},
    }
    if attendees:
        # A tool-calling model -- especially a smaller local one -- doesn't
        # reliably send array-typed arguments as a real JSON array; a single
        # attendee often arrives as a bare comma-separated string instead.
        # Iterating that directly walked its individual CHARACTERS into
        # {"email": "p"}, {"email": "r"}, ... which the Calendar API rejected
        # as a wall of "Invalid attendee email" errors.
        if isinstance(attendees, str):
            attendees = [a.strip() for a in attendees.split(",")]
        body["attendees"] = [{"email": a} for a in attendees if a]

    # The Calendar UI's own "notify guests" checkbox on invites IS the
    # sendUpdates param -- unlike the UI, events.insert defaults it to "none"
    # when omitted, so an attendee added by the API gets silently added to
    # the event with no invite email at all. Sending "all" whenever there are
    # attendees is what actually invites them, exactly like ticking that box.
    params = {"sendUpdates": "all"} if attendees else {}
    if add_meet:
        # Asking the Calendar API to attach a Meet link is done by sending a
        # conferenceData "create request" alongside the event, not by calling
        # any separate Meet API -- Meet itself has no public REST API of its
        # own for this. conferenceDataVersion=1 is required on the request or
        # Google silently ignores conferenceData and creates a plain event.
        body["conferenceData"] = {
            "createRequest": {
                "requestId": secrets.token_hex(16),
                "conferenceSolutionKey": {"type": "hangoutsMeet"},
            }
        }
        params["conferenceDataVersion"] = 1

    created = _api_post(f"{CALENDAR_API}/calendars/primary/events", body, params)
    meet_link = ""
    for entry in (created.get("conferenceData", {}).get("entryPoints") or []):
        if entry.get("entryPointType") == "video":
            meet_link = entry.get("uri", "")
            break

    return {"id": created.get("id"), "link": created.get("htmlLink"),
            "summary": created.get("summary"), "meet_link": meet_link}


# ---------- Drive ----------

def drive_list(query: str = "", max_results: int = 20) -> list:
    params = {
        "pageSize": max(1, min(max_results, 100)),
        "fields": "files(id,name,mimeType,modifiedTime,webViewLink,size,owners(displayName))",
        "orderBy": "modifiedTime desc",
    }
    if query:
        # escape single quotes so a filename with an apostrophe can't break
        # (or alter) the Drive query expression
        safe = query.replace("\\", "\\\\").replace("'", "\\'")
        params["q"] = f"name contains '{safe}' and trashed = false"
    else:
        params["q"] = "trashed = false"

    data = _api_get(f"{DRIVE_API}/files", params)
    out = []
    for f in data.get("files", []) or []:
        out.append({
            "id": f.get("id"),
            "name": f.get("name"),
            "mime_type": f.get("mimeType", ""),
            "modified": f.get("modifiedTime", ""),
            "link": f.get("webViewLink", ""),
            "size": f.get("size"),
            "owner": (f.get("owners") or [{}])[0].get("displayName", ""),
        })
    return out
