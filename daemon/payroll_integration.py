"""RazorpayX Payroll (Opfin) integration -- attendance, leave, salary holds,
bonuses/deductions, and "what is X getting paid this month".

Like calcom_integration.py this needs no OAuth: RazorpayX Payroll issues an
org id + API key from Settings > API, and both are stored in settings.json.

The API is unusual in a few ways worth knowing before touching this file:

  * Auth goes in the JSON body, not a header, and every call is a POST to one
    URL per module -- `request.type` + `request.sub-type` pick the action.
  * Errors come back as {"error": {"message", "code"}}, often with HTTP 200,
    so the status code alone can't be trusted.
  * There is NO list/search endpoint for people. A user saying "mark Rahul on
    leave" needs a name -> email lookup that the API can't answer, so this
    module keeps a local directory (payroll_directory.json) built by walking
    `people/view` over employee ids 1, 2, 3... until several in a row miss.
    Employee ids are the plain sequential integers Opfin assigns on create.
  * `people/view` returns PAN and full bank account numbers. The directory
    keeps only name/email/title/department/is_active; the rest is dropped the
    moment the response is parsed and never written to disk or shown to a model.
"""

import datetime
import difflib
import json
import threading
from pathlib import Path

import requests

API_BASE = "https://payroll.razorpay.com/api"
REQUEST_TIMEOUT = 20
DIRECTORY_PATH = Path.home() / "Mira" / "daemon" / "payroll_directory.json"

# Directory scan: stop after this many consecutive "Unable to locate employee"
# misses. A few gaps are normal (deleted drafts), a long run means we're past
# the end of the org.
_SCAN_MISS_LIMIT = 5
_SCAN_MAX_ID = 1000
_MAX_LEAVE_RANGE_DAYS = 31

_ATTENDANCE_STATUSES = ("present", "leave", "half-day", "unpaid-leave", "unpaid-half-day")

_ERR_INVALID_AUTH = 3
_ERR_PERMISSION = -1
_ERR_NOT_FOUND = 8
_ERR_NO_ATTENDANCE = 12

_MODULE_NAMES = {"people": "People", "payroll": "Payroll", "att": "Attendance",
                 "contractorPayment": "Contractor Payments", "advanceSalary": "Advance Salary"}

_sync_lock = threading.Lock()


class PayrollError(RuntimeError):
    def __init__(self, message: str, code=None):
        super().__init__(message)
        self.code = code


class EmployeeNotFound(ValueError):
    pass


# ---------- transport ----------

def _credentials():
    from main import load_settings
    s = load_settings()
    org_id = str(s.get("payroll_org_id", "")).strip()
    key = s.get("payroll_api_key", "")
    return org_id, key


def is_connected() -> bool:
    org_id, key = _credentials()
    return bool(org_id and key)


def _call(module: str, sub_type: str, data: dict = None, method: str = "POST") -> dict:
    org_id, key = _credentials()
    if not (org_id and key):
        raise PayrollError("RazorpayX Payroll isn't connected. Add the org ID and API key in Settings.")
    try:
        auth_id = int(org_id)
    except ValueError:
        raise PayrollError(f"The payroll org ID in Settings should be a number, got {org_id!r}.")

    body = {
        "auth": {"id": auth_id, "key": key},
        "request": {"type": module, "sub-type": sub_type},
        "data": data or {},
    }
    resp = requests.request(method, f"{API_BASE}/{module}", json=body, timeout=REQUEST_TIMEOUT)
    try:
        payload = resp.json()
    except ValueError:
        raise PayrollError(f"RazorpayX Payroll error {resp.status_code}: {resp.text[:300]}")

    err = payload.get("error") if isinstance(payload, dict) else None
    if err:
        code = err.get("code") if isinstance(err, dict) else None
        message = err.get("message") if isinstance(err, dict) else str(err)
        if code == _ERR_INVALID_AUTH:
            message = "Invalid auth -- the payroll org ID or API key in Settings is wrong."
        elif code == _ERR_PERMISSION:
            message = (f"Permission denied -- this API key doesn't have the "
                       f"{_MODULE_NAMES.get(module, module)} module enabled "
                       f"(RazorpayX Payroll > Settings > API).")
        raise PayrollError(message or f"RazorpayX Payroll error: {payload}", code)
    if resp.status_code >= 400:
        raise PayrollError(f"RazorpayX Payroll error {resp.status_code}: {payload}")
    return payload


def _unwrap(payload: dict) -> dict:
    """Some responses nest the record under "data", some return it flat."""
    inner = payload.get("data") if isinstance(payload, dict) else None
    return inner if isinstance(inner, dict) else payload


# ---------- employee directory ----------

def _load_directory() -> dict:
    if DIRECTORY_PATH.exists():
        try:
            return json.loads(DIRECTORY_PATH.read_text())
        except (json.JSONDecodeError, OSError):
            pass
    return {"employees": [], "synced_at": None}


def _save_directory(directory: dict):
    DIRECTORY_PATH.write_text(json.dumps(directory, indent=2))


def _view_person(employee_id: int, employee_type: str) -> dict:
    rec = _unwrap(_call("people", "view", {"employee-id": employee_id, "employee-type": employee_type}))
    # Deliberately an allowlist: PAN, bank details and DOB never leave this function.
    return {
        "employee_id": employee_id,
        "type": employee_type,
        "name": rec.get("name", ""),
        "email": rec.get("email", ""),
        "title": rec.get("title", ""),
        "department": rec.get("department", ""),
        "manager_email": rec.get("manager-email", ""),
        "date_of_hiring": rec.get("date-of-hiring", ""),
        "is_active": rec.get("is_active", True) not in (False, 0, "0", "false"),
    }


def sync_directory() -> dict:
    """Rebuild the local directory by walking employee ids for both employees
    and contractors (they have separate id spaces)."""
    with _sync_lock:
        people = []
        for employee_type in ("employee", "contractor"):
            misses = 0
            for employee_id in range(1, _SCAN_MAX_ID + 1):
                try:
                    people.append(_view_person(employee_id, employee_type))
                    misses = 0
                except PayrollError as e:
                    if e.code != _ERR_NOT_FOUND:
                        raise
                    misses += 1
                    if misses >= _SCAN_MISS_LIMIT:
                        break
        directory = {"employees": people,
                     "synced_at": datetime.datetime.now().isoformat(timespec="seconds")}
        _save_directory(directory)
        return directory


def directory_status() -> dict:
    d = _load_directory()
    emps = d.get("employees", [])
    return {
        "connected": is_connected(),
        "synced_at": d.get("synced_at"),
        "employees": sum(1 for e in emps if e["type"] == "employee" and e.get("is_active")),
        "contractors": sum(1 for e in emps if e["type"] == "contractor" and e.get("is_active")),
        "inactive": sum(1 for e in emps if not e.get("is_active")),
    }


def list_people(include_inactive: bool = False) -> list:
    d = _load_directory()
    if not d.get("synced_at"):
        d = sync_directory()
    return [{"name": e["name"], "email": e["email"], "employee_id": e["employee_id"],
             "type": e["type"], "title": e.get("title", ""), "department": e.get("department", ""),
             "active": e.get("is_active", True)}
            for e in d.get("employees", []) if include_inactive or e.get("is_active", True)]


def _match(query: str, people: list) -> list:
    q = " ".join(query.lower().split())
    if not q:
        return []
    if q.isdigit():
        return [p for p in people if p["employee_id"] == int(q)]
    if "@" in q:
        return [p for p in people if p.get("email", "").lower() == q]

    names = {id(p): " ".join(p.get("name", "").lower().split()) for p in people}
    exact = [p for p in people if names[id(p)] == q]
    if exact:
        return exact
    # "Rahul" or "Sharma" -- every word the user said appears as a word in the name
    words = q.split()
    by_words = [p for p in people if all(w in names[id(p)].split() for w in words)]
    if by_words:
        return by_words
    by_substring = [p for p in people if q in names[id(p)]]
    if by_substring:
        return by_substring
    # Speech-to-text spellings: "Rahool", "Priya Sharmaa"
    close = difflib.get_close_matches(q, [names[id(p)] for p in people], n=3, cutoff=0.75)
    return [p for p in people if names[id(p)] in close]


_SELF_WORDS = {"me", "myself", "i", "my", "mine", "self", "owner"}


def self_person() -> dict:
    """The Mira owner's own payroll record, for "check me in" / "mark me on
    leave". Set explicitly in Settings; falls back to matching owner_name."""
    from main import load_settings
    s = load_settings()
    people = _load_directory().get("employees", [])
    email = str(s.get("payroll_self_email", "")).strip().lower()
    if email:
        for p in people:
            if p.get("email", "").lower() == email:
                return p
        return {"name": email, "email": email, "employee_id": None, "type": "employee", "is_active": True}
    owner = s.get("owner_name", "")
    found = [p for p in _match(owner, people) if p.get("is_active", True)] if owner else []
    if len(found) == 1:
        return found[0]
    raise EmployeeNotFound("I don't know which payroll employee you are yet. "
                           "Pick yourself under Settings > RazorpayX Payroll > You in payroll.")


def resolve_person(query: str, employee_type: str = "") -> dict:
    """Name, email or employee id -> one directory entry. Raises
    EmployeeNotFound with the real candidates when it can't pick one, so the
    model can ask the user which person they meant instead of guessing."""
    query = str(query or "").strip()
    if query.lower() in _SELF_WORDS:
        return self_person()
    if not query:
        raise EmployeeNotFound("Which employee? Give a name, email or employee id.")

    def candidates(directory):
        people = directory.get("employees", [])
        if employee_type:
            people = [p for p in people if p["type"] == employee_type]
        found = _match(query, people)
        active = [p for p in found if p.get("is_active", True)]
        return active or found

    directory = _load_directory()
    found = candidates(directory)
    if not found:
        # Someone may have joined since the last sync -- rescan once.
        directory = sync_directory()
        found = candidates(directory)

    if not found:
        if "@" in query:
            # Not in our directory, but the API accepts email directly.
            return {"name": query, "email": query, "employee_id": None,
                    "type": employee_type or "employee", "is_active": True}
        raise EmployeeNotFound(f"No one called \"{query}\" in RazorpayX Payroll.")
    if len(found) > 1:
        options = "; ".join(f"{p['name']} ({p['email']}, {p['type']} #{p['employee_id']})" for p in found[:6])
        raise EmployeeNotFound(f"\"{query}\" matches more than one person: {options}. Ask which one.")
    return found[0]


def _identity(person: dict, with_type: bool = True) -> dict:
    """Email is what the docs recommend identifying people by."""
    ident = {"email": person["email"]} if person.get("email") else {"employee-id": person["employee_id"]}
    if with_type:
        ident["employee-type"] = person.get("type", "employee")
    return ident


# ---------- payroll ----------

def _month(month: str = "") -> str:
    m = str(month or "").strip()
    if not m:
        return datetime.date.today().strftime("%Y-%m")
    try:
        return datetime.datetime.strptime(m[:7], "%Y-%m").strftime("%Y-%m")
    except ValueError:
        raise ValueError(f"month must be YYYY-MM, got {month!r}")


def _sum_additions(additions) -> float:
    if isinstance(additions, dict):
        values = additions.values()
    elif isinstance(additions, list):
        values = [a.get("amount", 0) if isinstance(a, dict) else a for a in additions]
    else:
        return 0.0
    total = 0.0
    for v in values:
        if isinstance(v, dict):
            v = v.get("amount", 0)
        try:
            total += float(v)
        except (TypeError, ValueError):
            pass
    return total


def _payroll_snapshot(payload: dict, person: dict) -> dict:
    snap = _unwrap(payload)
    salary = float(snap.get("salary") or 0)
    additions = snap.get("additions") or {}
    deduction = float(snap.get("deduction-amount") or 0)
    do_not_pay = bool(snap.get("do-not-pay"))
    payable = 0.0 if do_not_pay else salary + _sum_additions(additions) - deduction
    return {
        "name": snap.get("employee-name") or person.get("name"),
        "email": person.get("email"),
        "month": snap.get("payroll-month"),
        "salary": salary,
        "additions": additions,
        "deductions": deduction,
        "salary_on_hold": do_not_pay,
        "payable_before_statutory_deductions": round(payable, 2),
        "note": "Payable = salary + additions - deductions, before TDS/PF/PT which the API doesn't return.",
    }


def _payroll_call(sub_type: str, employee: str, month: str, extra: dict = None) -> dict:
    person = resolve_person(employee, "employee")
    data = {**_identity(person, with_type=False), "payroll-month": _month(month), **(extra or {})}
    return _payroll_snapshot(_call("payroll", sub_type, data), person)


def view_payroll(employee: str, month: str = "") -> dict:
    return _payroll_call("view-payroll", employee, month)


def payroll_summary(month: str = "") -> dict:
    month = _month(month)
    rows, errors, total = [], [], 0.0
    for p in _load_directory().get("employees", []) or sync_directory()["employees"]:
        if p["type"] != "employee" or not p.get("is_active", True):
            continue
        try:
            snap = _payroll_snapshot(
                _call("payroll", "view-payroll", {**_identity(p, with_type=False), "payroll-month": month}), p)
        except PayrollError as e:
            errors.append(f"{p['name']}: {e}")
            continue
        total += snap["payable_before_statutory_deductions"]
        rows.append({"name": snap["name"], "salary": snap["salary"],
                     "additions": _sum_additions(snap["additions"]), "deductions": snap["deductions"],
                     "on_hold": snap["salary_on_hold"],
                     "payable": snap["payable_before_statutory_deductions"]})
    return {"month": month, "employees": rows, "total_payable_before_statutory_deductions": round(total, 2),
            "errors": errors}


def set_salary_hold(employee: str, hold: bool, month: str = "") -> dict:
    return _payroll_call("do-not-pay", employee, month, {"value": bool(hold)})


def add_addition(employee: str, amount: float, label: str = "Bonus", month: str = "",
                 remarks: str = "") -> dict:
    amount = float(amount or 0)
    if amount <= 0:
        raise ValueError("amount must be a positive number")
    extra = {"additions": [{"label": label or "Bonus", "amount": amount}]}
    if remarks:
        extra["remarks"] = remarks
    return _payroll_call("add-additions", employee, month, extra)


def add_deduction(employee: str, days: float = None, amount: float = None, month: str = "",
                  remarks: str = "") -> dict:
    if bool(days) == bool(amount):
        raise ValueError("give exactly one of days (loss of pay) or amount")
    # Opfin computes days x monthly salary / days in month itself when given days.
    extra = {"deduction-days": float(days)} if days else {"deduction-amount": float(amount)}
    if remarks:
        extra["remarks"] = remarks
    return _payroll_call("add-deduction", employee, month, extra)


def reset_modifications(employee: str, month: str = "") -> dict:
    return _payroll_call("reset-modifications", employee, month)


# ---------- attendance ----------

_WEEKDAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]


def _date(value: str, prefer: str = "past") -> datetime.date:
    """YYYY-MM-DD, or the user's own words -- "today", "tomorrow", "monday",
    "last friday", "next tuesday". Same lesson as calcom_integration's
    _resolve_start: date arithmetic done by the model came out a day off
    ("Monday" from a Thursday the 24th became the 22nd, a Tuesday), so it's
    done here instead. A bare weekday means the most recent one for reads
    (prefer="past") and the coming one for marking leave (prefer="future")."""
    raw = " ".join(str(value or "").strip().lower().split())
    today = datetime.date.today()
    simple = {"today": 0, "tomorrow": 1, "yesterday": -1,
              "day after tomorrow": 2, "day before yesterday": -2}
    if raw in simple:
        return today + datetime.timedelta(days=simple[raw])
    words = raw.split()
    if words and words[-1] in _WEEKDAYS and len(words) <= 2:
        target = _WEEKDAYS.index(words[-1])
        direction = {"last": "past", "next": "future", "this": prefer, "coming": "future"}.get(
            words[0], prefer) if len(words) == 2 else prefer
        if direction == "past":
            delta = (today.weekday() - target) % 7
            return today - datetime.timedelta(days=delta)
        delta = (target - today.weekday()) % 7 or 7
        return today + datetime.timedelta(days=delta)
    try:
        return datetime.date.fromisoformat(raw[:10])
    except ValueError:
        raise ValueError(f"dates must be YYYY-MM-DD or like 'tomorrow'/'monday', got {value!r}")


def _leave_types_from(payload) -> list:
    """Shape of the leave-type=-1 response isn't documented -- accept a list of
    {id,name}-ish dicts or an {id: name} mapping wherever it's nested."""
    found = []

    def walk(node):
        if isinstance(node, list):
            for item in node:
                if isinstance(item, dict):
                    ident = item.get("id", item.get("leave-type", item.get("type")))
                    name = item.get("name", item.get("label", item.get("title")))
                    if ident is not None and name:
                        found.append({"id": ident, "name": str(name)})
                    else:
                        walk(item)
        elif isinstance(node, dict):
            for k, v in node.items():
                if isinstance(v, str) and str(k).lstrip("-").isdigit():
                    found.append({"id": int(k), "name": v})
                else:
                    walk(v)

    walk(payload)
    return found


def _resolve_leave_type(person: dict, date: datetime.date, leave_type):
    """Numeric ids pass through; a name like "sick" is looked up via the
    API's leave-type=-1 listing so the model never has to know org ids."""
    if leave_type in (None, ""):
        return None
    if isinstance(leave_type, (int, float)) or str(leave_type).lstrip("-").isdigit():
        return int(leave_type)
    try:
        listing = _call("att", "modify", {**_identity(person), "date": date.isoformat(),
                                          "status": "leave", "leave-type": -1})
    except PayrollError as e:
        listing = {"error": str(e)}
    types = _leave_types_from(listing)
    needle = str(leave_type).lower()
    for t in types:
        if needle == t["name"].lower() or needle in t["name"].lower():
            return int(t["id"])
    available = ", ".join(f"{t['name']} ({t['id']})" for t in types) or json.dumps(listing)[:400]
    raise ValueError(f"No leave type matches \"{leave_type}\". This org's leave types: {available}.")


def mark_attendance(employee: str, status: str, start_date: str, end_date: str = "",
                    leave_type=None, checkin: str = "", checkout: str = "", remarks: str = "",
                    skip_weekends: bool = False) -> dict:
    status = str(status or "").strip().lower()
    if status not in _ATTENDANCE_STATUSES:
        raise ValueError(f"status must be one of: {', '.join(_ATTENDANCE_STATUSES)}")
    start = _date(start_date, "future")
    end = _date(end_date, "future") if end_date else start
    if end < start and end_date and not str(end_date)[:4].isdigit():
        end += datetime.timedelta(days=7)  # "friday to monday" wraps into next week
    if end < start:
        raise ValueError("end_date is before start_date")
    if (end - start).days + 1 > _MAX_LEAVE_RANGE_DAYS:
        raise ValueError(f"at most {_MAX_LEAVE_RANGE_DAYS} days at a time")

    person = resolve_person(employee)
    leave_id = _resolve_leave_type(person, start, leave_type) if status in ("leave", "half-day") else None

    marked, failed = [], []
    day = start
    while day <= end:
        if skip_weekends and day.weekday() >= 5:
            day += datetime.timedelta(days=1)
            continue
        data = {**_identity(person), "date": day.isoformat(), "status": status}
        if leave_id is not None:
            data["leave-type"] = leave_id
        if status in ("present", "half-day"):
            if checkin:
                data["checkin"] = checkin
            if checkout:
                data["checkout"] = checkout
        if remarks:
            data["remarks"] = remarks
        # POST replaces the whole day's record, which is what marking a
        # status means -- a leave day shouldn't keep a stale check-in.
        try:
            _call("att", "modify", data)
            marked.append(f"{day:%a %d %b %Y}")  # weekday included so a wrong range is obvious
        except PayrollError as e:
            failed.append(f"{day.isoformat()}: {e}")
        day += datetime.timedelta(days=1)

    result = {"name": person["name"], "status": status, "marked_dates": marked}
    if failed:
        result["failed"] = failed
    if status == "leave":
        result["note"] = "If the org doesn't allow negative leave balance, extra leave becomes unpaid leave."
    return result


def _hhmm(value: str = "") -> str:
    if not value:
        return datetime.datetime.now().strftime("%H:%M")
    v = str(value).strip().lower().replace(".", ":")
    for fmt in ("%H:%M", "%H:%M:%S", "%I:%M %p", "%I:%M%p", "%I %p", "%I%p"):
        try:
            return datetime.datetime.strptime(v.upper(), fmt).strftime("%H:%M")
        except ValueError:
            pass
    raise ValueError(f"time should look like 09:30 or 6pm, got {value!r}")


def _fetch_day(person: dict, day: datetime.date):
    try:
        return _unwrap(_call("att", "fetch", {**_identity(person), "date": day.isoformat()}))
    except PayrollError as e:
        if e.code == _ERR_NO_ATTENDANCE:
            return None
        raise


def punch(action: str = "auto", employee: str = "me", time: str = "", remarks: str = "") -> dict:
    """Check in or out for today, the way the NFC gate device does it: one
    check-in and one check-out per day, so fetch first and decide. "auto"
    checks in if there's no check-in yet, otherwise checks out.

    Check-in is write-once (the first one of the day is the real arrival
    time); check-out is overwritten freely, since the last one is the real
    departure. `remarks` replaces the day's remark only when given."""
    remarks = str(remarks or "").strip()
    action = str(action or "auto").strip().lower().replace("-", "").replace(" ", "")
    if action not in ("auto", "checkin", "checkout"):
        raise ValueError("action must be checkin, checkout or auto")
    person = resolve_person(employee or "me")
    day = datetime.date.today()
    at = _hhmm(time)
    rec = _fetch_day(person, day) or {}
    checked_in = rec.get("check-in")
    checked_out = rec.get("check-out")
    if action == "auto":
        action = "checkout" if checked_in else "checkin"

    result = {"name": person["name"], "date": day.isoformat()}
    if action == "checkin":
        if checked_in:
            out = {**result, "action": "none",
                   "message": f"Already checked in today at {checked_in[:5]}; check-in is only set once a day."}
            if remarks:
                _call("att", "modify", {**_identity(person), "date": day.isoformat(), "remarks": remarks},
                      method="PATCH")
                out["remarks_updated"] = remarks
            return out
        # POST creates the day's record.
        _call("att", "modify", {**_identity(person), "date": day.isoformat(), "status": "present",
                                "checkin": at, "remarks": remarks or "Mira"})
        return {**result, "action": "checked in", "time": at, "remarks": remarks or "Mira"}

    if not checked_in:
        return {**result, "action": "none",
                "message": "No check-in recorded today, so there's nothing to check out of. "
                           "Check in first (a time can be given, e.g. 'I came in at 9:30')."}
    # PATCH, not POST: POST replaces the record and would lose the check-in.
    patch = {**_identity(person), "date": day.isoformat(), "checkout": at}
    if remarks:
        patch["remarks"] = remarks
    _call("att", "modify", patch, method="PATCH")
    out = {**result, "action": "checked out", "time": at, "checked_in_at": checked_in[:5]}
    if remarks:
        out["remarks"] = remarks
    if checked_out:
        out["replaced_earlier_checkout"] = checked_out[:5]
    return out


def get_attendance(employee: str, date: str = "") -> dict:
    person = resolve_person(employee)
    day = _date(date) if date else datetime.date.today()
    rec = _fetch_day(person, day)
    if rec is None:
        return {"name": person["name"], "date": day.isoformat(), "status": "no record"}
    def desc(field):
        # status / leave-type come back as {"code": 10, "description": "present"}
        v = rec.get(field)
        return v.get("description") if isinstance(v, dict) else v

    out = {"name": person["name"], "date": day.isoformat(), "status": desc("status"),
           "leave_type": desc("leave-type"), "checkin": rec.get("check-in"),
           "checkout": rec.get("check-out"), "remarks": rec.get("remarks")}
    pending = {"status": desc("requested-status"), "leave_type": desc("requested-leave-type"),
               "checkin": rec.get("requested-check-in"), "checkout": rec.get("requested-check-out")}
    if any(pending.values()):
        out["pending_change_request"] = {k: v for k, v in pending.items() if v}
    return {k: v for k, v in out.items() if v is not None}


def employee_details(employee: str) -> dict:
    person = resolve_person(employee)
    if person.get("employee_id") is None:
        return person
    return _view_person(person["employee_id"], person["type"])
