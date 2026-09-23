"""Cal.com integration -- bookings and event types.

Unlike google_integration.py this needs no OAuth flow: Cal.com issues a
personal API key straight from Settings > Developer > API Keys, so this is
just a bearer token the user pastes in, stored the same gitignored way as
every other credential in this file's siblings (see settings.json).

Cal.com's v2 API pins behavior to a specific date per endpoint via the
cal-api-version header rather than a single global API version -- passing
the wrong (or no) value silently falls back to an older response shape
instead of erroring, so every call here sends the exact version documented
for that specific endpoint.
"""

import datetime
import os

import requests

API_BASE = "https://api.cal.com/v2"
REQUEST_TIMEOUT = 20


def _local_offset() -> datetime.timezone:
    """This machine's real UTC offset, e.g. +05:30 -- not tzname()'s "IST",
    which is an ambiguous abbreviation (also Irish Standard Time) that isn't
    a valid Cal.com timeZone value."""
    return datetime.datetime.now().astimezone().tzinfo


def _local_iana_zone() -> str:
    """The actual IANA zone id (e.g. "Asia/Kolkata"), read off /etc/localtime
    the same way the `tzdata`-free stdlib itself has no cross-platform way to
    get one -- Cal.com's `attendee.timeZone` wants this, not an offset or an
    ambiguous abbreviation like "IST"."""
    try:
        real = os.path.realpath("/etc/localtime")
        return real.split("zoneinfo/", 1)[1]
    except (OSError, IndexError):
        return "UTC"


def _resolve_start(start: str) -> str:
    """A user asking to book "1pm tomorrow" means 1pm in THEIR timezone, not
    UTC -- but the tool used to ask the model for a UTC ISO string directly,
    which meant the model itself had to do the local-to-UTC conversion in
    its head on every single booking. That is exactly the kind of arithmetic
    LLMs get wrong silently: a request for 1pm IST was going out as
    literally "13:00Z" (1pm UTC = 6:30pm IST), landing outside business
    hours and coming back as a false "not available" -- confirmed directly
    against this account's real /slots response, which listed 1pm IST as
    open while 6:30pm IST was not.

    So: a bare local datetime with no timezone marker (no trailing Z, no
    +HH:MM/-HH:MM offset) is now assumed to already be in this machine's own
    local time -- which is the user's time, since Mira runs on their own
    Mac -- and gets the real local offset attached here, in code, instead of
    asking a model to compute it. A caller that already included a proper
    offset or "Z" is trusted as-is and passed through untouched."""
    s = start.strip()
    try:
        parsed = datetime.datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        raise ValueError(f"start must be an ISO 8601 datetime, got {start!r}")
    if parsed.tzinfo is not None:
        return s  # already carries an explicit offset/Z -- trust it as-is
    return parsed.replace(tzinfo=_local_offset()).isoformat()

# Each endpoint's response shape is versioned independently -- these are the
# versions this module's parsing was written against.
_VERSION_BOOKINGS_LIST = "2026-05-01"
_VERSION_BOOKINGS_WRITE = "2026-02-25"   # create / cancel / reschedule
_VERSION_EVENT_TYPES = "2024-06-14"


def is_connected() -> bool:
    from main import load_settings
    return bool(load_settings().get("cal_com_api_key"))


def _headers(version: str) -> dict:
    from main import load_settings
    api_key = load_settings().get("cal_com_api_key", "")
    if not api_key:
        raise RuntimeError("Cal.com isn't connected. Add an API key in Settings.")
    return {
        "Authorization": f"Bearer {api_key}",
        "cal-api-version": version,
        "Content-Type": "application/json",
    }


def _request(method: str, path: str, version: str, params: dict = None, body: dict = None) -> dict:
    resp = requests.request(
        method, f"{API_BASE}{path}",
        headers=_headers(version), params=params or {}, json=body,
        timeout=REQUEST_TIMEOUT,
    )
    try:
        data = resp.json()
    except ValueError:
        raise RuntimeError(f"Cal.com API error {resp.status_code}: {resp.text[:300]}")
    if resp.status_code >= 400 or data.get("status") == "error":
        detail = data.get("error", {}).get("message") if isinstance(data.get("error"), dict) else data.get("error")
        raise RuntimeError(f"Cal.com API error {resp.status_code}: {detail or data}")
    return data


# ---------- bookings ----------

def list_bookings(status: str = "upcoming", limit: int = 20) -> list:
    """status: upcoming | recurring | past | cancelled | unconfirmed."""
    data = _request("GET", "/bookings", _VERSION_BOOKINGS_LIST,
                    params={"status": status, "limit": max(1, min(limit, 100))})
    out = []
    for b in data.get("data", []) or []:
        out.append({
            "uid": b.get("uid"),
            "title": b.get("title"),
            "status": b.get("status"),
            "start": b.get("start"),
            "end": b.get("end"),
            "location": b.get("location"),
            "attendees": [{"name": a.get("name", ""), "email": a.get("email", "")}
                         for a in (b.get("attendees") or [])],
            "created_at": b.get("createdAt"),
        })
    return out


def find_event_type(name_or_slug: str) -> dict:
    """Resolves a human-typed event type ("30 Min Meeting") to its id/slug --
    a tool call has no reason to know Cal.com's internal numeric ids."""
    data = _request("GET", "/event-types", _VERSION_EVENT_TYPES)
    types = data.get("data", []) or []
    # Cal.com nests event types under a "eventTypeGroups[].eventTypes[]" shape
    # in some responses and a flat list in others depending on account type
    # (personal vs team) -- flatten defensively rather than assume one.
    flat = []
    for item in types:
        if "eventTypes" in item:
            flat.extend(item.get("eventTypes") or [])
        else:
            flat.append(item)

    needle = name_or_slug.strip().lower()
    for et in flat:
        if et.get("slug", "").lower() == needle or et.get("title", "").lower() == needle:
            return et
    for et in flat:
        if needle in et.get("title", "").lower() or needle in et.get("slug", "").lower():
            return et
    # Naming the real options here, not just rejecting the guess, is what
    # lets a model that skipped list_calcom_event_types self-correct on the
    # next tool call instead of reporting back "the tool doesn't work" --
    # this was the actual create_calcom_booking failure mode being fixed.
    available = ", ".join(f"\"{et.get('title')}\"" for et in flat) or "none configured"
    raise ValueError(
        f"No Cal.com event type matches \"{name_or_slug}\". "
        f"Real event types on this account: {available}."
    )


def list_event_types() -> list:
    data = _request("GET", "/event-types", _VERSION_EVENT_TYPES)
    types = data.get("data", []) or []
    flat = []
    for item in types:
        if "eventTypes" in item:
            flat.extend(item.get("eventTypes") or [])
        else:
            flat.append(item)
    return [{"id": et.get("id"), "title": et.get("title"), "slug": et.get("slug"),
             "length_minutes": et.get("lengthInMinutes")} for et in flat]


def create_booking(event_type: str, start_iso: str, attendee_name: str,
                   attendee_email: str, attendee_timezone: str = "") -> dict:
    if not event_type or not start_iso or not attendee_name or not attendee_email:
        raise ValueError("event_type, start, attendee_name and attendee_email are required")

    et = find_event_type(event_type)
    body = {
        "start": _resolve_start(start_iso),
        "eventTypeId": et["id"],
        "attendee": {
            "name": attendee_name,
            "email": attendee_email,
            "timeZone": attendee_timezone or _local_iana_zone(),
        },
        # Undocumented as of this writing (cal.com's own docs' example body
        # omits it entirely): an event type with a custom "title" booking
        # question 400s without this, with an error -- "responses -
        # {title}error_required_field" -- that reads like it wants a
        # ROOT-level `title`. It doesn't: a root-level title 400s just as
        # hard the other way ("property title should not exist"). It has to
        # go here, under bookingFieldsResponses, matching the shape a
        # booking's own bookingFieldsResponses comes back in once created.
        "bookingFieldsResponses": {
            "title": f"{et.get('title', event_type)} with {attendee_name}",
        },
    }
    data = _request("POST", "/bookings", _VERSION_BOOKINGS_WRITE, body=body)
    b = data.get("data", {})
    return {"uid": b.get("uid"), "title": b.get("title"),
            "start": b.get("start"), "end": b.get("end")}


def cancel_booking(booking_uid: str, reason: str = "") -> dict:
    if not booking_uid:
        raise ValueError("booking_uid is required")
    body = {"cancellationReason": reason} if reason else {}
    _request("POST", f"/bookings/{booking_uid}/cancel", _VERSION_BOOKINGS_WRITE, body=body)
    return {"cancelled": True, "uid": booking_uid}


def reschedule_booking(booking_uid: str, new_start_iso: str, reason: str = "") -> dict:
    if not booking_uid or not new_start_iso:
        raise ValueError("booking_uid and new_start are required")
    body = {"start": new_start_iso}
    if reason:
        body["reschedulingReason"] = reason
    data = _request("POST", f"/bookings/{booking_uid}/reschedule", _VERSION_BOOKINGS_WRITE, body=body)
    b = data.get("data", {})
    return {"uid": b.get("uid") or booking_uid, "start": b.get("start"), "end": b.get("end")}
