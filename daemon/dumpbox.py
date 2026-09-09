"""Dump Box -- freeform capture that Mira turns into a summary + action plan.

The idea is a single low-friction inbox: paste or dictate anything (meeting
scraps, a wall of thoughts, a list of links), and Mira turns it into a short
summary, key points, and concrete action items that can be pushed into the
macOS Reminders app.

The LLM itself is injected as a callable by main.py rather than imported here,
so this module stays free of the daemon's model-routing/import graph.
"""

import json
import re
import subprocess
import uuid
import datetime
from pathlib import Path

DUMPBOX_DIR = Path.home() / "Mira" / "daemon" / "dumpbox"
DUMPBOX_DIR.mkdir(parents=True, exist_ok=True)
ENTRIES_PATH = DUMPBOX_DIR / "entries.json"

MAX_ENTRY_CHARS = 20000


# ---------- storage ----------

def _load_all() -> list:
    if not ENTRIES_PATH.exists():
        return []
    try:
        data = json.loads(ENTRIES_PATH.read_text())
        return data if isinstance(data, list) else []
    except (json.JSONDecodeError, OSError):
        return []


def _save_all(entries: list):
    ENTRIES_PATH.write_text(json.dumps(entries, indent=2))


def list_entries() -> list:
    # newest first -- the box is an inbox, most recent capture is most relevant
    return sorted(_load_all(), key=lambda e: e.get("created_at", ""), reverse=True)


def get_entry(entry_id: str):
    return next((e for e in _load_all() if e.get("id") == entry_id), None)


def add_entry(text: str) -> dict:
    text = (text or "").strip()
    if not text:
        raise ValueError("empty entry")
    if len(text) > MAX_ENTRY_CHARS:
        text = text[:MAX_ENTRY_CHARS]

    entry = {
        "id": uuid.uuid4().hex[:12],
        "text": text,
        "created_at": datetime.datetime.now().isoformat(timespec="seconds"),
        "processed": False,
        "summary": "",
        "key_points": [],
        "action_items": [],
    }
    entries = _load_all()
    entries.append(entry)
    _save_all(entries)
    return entry


def update_entry(entry_id: str, **fields) -> dict:
    entries = _load_all()
    for e in entries:
        if e.get("id") == entry_id:
            e.update(fields)
            _save_all(entries)
            return e
    raise KeyError(entry_id)


def delete_entry(entry_id: str) -> bool:
    entries = _load_all()
    remaining = [e for e in entries if e.get("id") != entry_id]
    if len(remaining) == len(entries):
        return False
    _save_all(remaining)
    return True


# ---------- LLM processing ----------

PROCESS_PROMPT = """You turn a freeform brain-dump into a structured action plan.

Today is {today} ({weekday}).

The next seven days, so you never have to work a weekday out yourself:
{date_table}

Read the dump below and reply with ONLY a JSON object, no prose before or after,
in exactly this shape:

{{
  "summary": "one or two sentences capturing what this dump is about",
  "key_points": ["short factual point", "..."],
  "action_items": [
    {{"title": "short imperative task", "note": "any useful detail, or empty string", "due": "YYYY-MM-DD or null"}}
  ]
}}

Rules:
- action_items must be genuine tasks the writer needs to DO. If the dump has no
  tasks, return an empty list. Do not invent tasks to fill space.
- "title" must be short and imperative ("Email Ravi the invoice"), not a sentence
  copied from the dump.
- Set "due" only when the dump actually implies a date or deadline. Resolve
  relative dates by COPYING the matching date from the table above rather than
  counting days yourself. Otherwise null.
- key_points should be at most 6 items. Omit filler.

DUMP:
{text}
"""


def _extract_json(raw: str):
    """Pull the first JSON object out of a model reply.

    Local models frequently wrap JSON in prose or markdown fences even when told
    not to, so locating the outermost braces is more reliable than json.loads on
    the whole reply.
    """
    if not raw:
        return None

    fenced = re.search(r"```(?:json)?\s*(.*?)```", raw, re.DOTALL)
    if fenced:
        raw = fenced.group(1)

    start = raw.find("{")
    if start == -1:
        return None

    depth = 0
    in_string = False
    escaped = False
    for i in range(start, len(raw)):
        ch = raw[i]
        if in_string:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                try:
                    return json.loads(raw[start:i + 1])
                except json.JSONDecodeError:
                    return None
    return None


def _coerce_due(value):
    """Normalize a model-supplied due date to YYYY-MM-DD, or None."""
    if not value or not isinstance(value, str):
        return None
    v = value.strip()
    if v.lower() in ("null", "none", ""):
        return None
    m = re.match(r"(\d{4})-(\d{2})-(\d{2})", v)
    if not m:
        return None
    try:
        datetime.date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    except ValueError:
        return None
    return m.group(0)


def process_entry(entry_id: str, llm_call) -> dict:
    """Run the dump through the LLM and store the structured result.

    `llm_call` takes a prompt string and returns the model's raw text reply.
    """
    entry = get_entry(entry_id)
    if entry is None:
        raise KeyError(entry_id)

    now = datetime.datetime.now()
    # Spelling the week out beats asking a small model to do calendar
    # arithmetic: given only "today is Wednesday", they routinely resolve
    # "by Friday" to a Sunday. That mattered little while a human reviewed
    # every item, and matters a lot now that these file themselves.
    date_table = "\n".join(
        "  {} = {}{}".format(
            (now + datetime.timedelta(days=d)).strftime("%A"),
            (now + datetime.timedelta(days=d)).strftime("%Y-%m-%d"),
            " (tomorrow)" if d == 1 else (" (today)" if d == 0 else ""),
        )
        for d in range(8)
    )
    prompt = PROCESS_PROMPT.format(
        today=now.strftime("%Y-%m-%d"),
        weekday=now.strftime("%A"),
        date_table=date_table,
        text=entry["text"],
    )

    raw = llm_call(prompt)
    parsed = _extract_json(raw)

    if not parsed:
        # Don't fail the whole capture just because the model returned prose --
        # the user's text is the valuable part and must never be lost.
        return update_entry(
            entry_id,
            processed=True,
            summary=(raw or "").strip()[:500] or "Could not summarize this entry.",
            key_points=[],
            action_items=[],
            parse_failed=True,
        )

    raw_items = parsed.get("action_items") or []
    action_items = []
    for item in raw_items:
        if not isinstance(item, dict):
            continue
        title = str(item.get("title") or "").strip()
        if not title:
            continue
        action_items.append({
            "id": uuid.uuid4().hex[:8],
            "title": title[:200],
            "note": str(item.get("note") or "").strip()[:500],
            "due": _coerce_due(item.get("due")),
            "pushed": False,
        })

    key_points = [
        str(p).strip()[:300]
        for p in (parsed.get("key_points") or [])
        if str(p).strip()
    ][:6]

    return update_entry(
        entry_id,
        processed=True,
        summary=str(parsed.get("summary") or "").strip()[:1000],
        key_points=key_points,
        action_items=action_items,
        parse_failed=False,
    )


# ---------- macOS Reminders ----------

# Arguments are passed through `osascript ... -- arg1 arg2` and read via `on run
# argv`, never interpolated into the script text. User-supplied task titles can
# contain quotes and backslashes, and string-building the script would make that
# an injection vector.
_ADD_REMINDER_SCRIPT = """
on run argv
    set theName to item 1 of argv
    set theBody to item 2 of argv
    set dueSpec to item 3 of argv
    set listName to item 4 of argv

    tell application "Reminders"
        if listName is "" then
            set targetList to default list
        else
            set targetList to list listName
        end if

        if dueSpec is "" then
            set newReminder to make new reminder at targetList with properties {name:theName, body:theBody}
        else
            set secsFromNow to dueSpec as integer
            set dueDate to (current date) + secsFromNow
            set newReminder to make new reminder at targetList with properties {name:theName, body:theBody, due date:dueDate}
        end if
    end tell
    return "ok"
end run
"""

_LIST_LISTS_SCRIPT = """
tell application "Reminders"
    set out to ""
    repeat with l in lists
        set out to out & (name of l) & linefeed
    end repeat
    return out
end tell
"""


def _due_to_offset_seconds(due: str):
    """Convert a YYYY-MM-DD due date into seconds from now.

    AppleScript's own date-string parsing is locale-dependent and a common source
    of silent wrong-date bugs, so the arithmetic is done here and only a plain
    integer offset crosses the boundary.
    """
    if not due:
        return ""
    try:
        d = datetime.datetime.strptime(due, "%Y-%m-%d")
    except ValueError:
        return ""
    # 9am on the due date reads as a sensible default reminder time
    target = d.replace(hour=9, minute=0, second=0)
    delta = int((target - datetime.datetime.now()).total_seconds())
    return str(delta)


def reminders_lists() -> list:
    try:
        result = subprocess.run(
            ["osascript", "-e", _LIST_LISTS_SCRIPT],
            capture_output=True, text=True, timeout=15,
        )
    except (subprocess.SubprocessError, OSError) as e:
        return {"error": str(e)}
    if result.returncode != 0:
        return {"error": (result.stderr or "").strip()}
    return [line.strip() for line in result.stdout.splitlines() if line.strip()]


def push_to_reminders(title: str, note: str = "", due: str = None, list_name: str = "") -> dict:
    """Create one reminder, by asking Electron to do it.

    This used to run osascript directly. It could not work: the daemon is a
    headless LaunchAgent, and macOS will not show one an Automation consent
    prompt -- the call either fails with -1743 or hangs on a dialog nobody can
    see. So the write is queued for Electron, which as a foreground app can get
    consent. It is the same path the create_reminder tool uses.
    """
    import actions

    action_id = actions.enqueue("create_reminder", {
        "title": title,
        "note": note or "",
        "due": due or None,
        "list_name": list_name or "",
    })
    # Generous, because the very first reminder of a session may sit behind the
    # user answering the Automation consent dialog.
    result = actions.wait_for(action_id, timeout=30)

    if result is None:
        return {"success": False,
                "error": "Queued, but the Mira app did not confirm. Is Mira running?"}
    if not result.get("success"):
        err = result.get("error", "could not create reminder")
        if "-1743" in str(err) or "Not authorized" in str(err):
            return {
                "success": False,
                "error": "Mira needs permission to control Reminders. Grant it in "
                         "System Settings > Privacy & Security > Automation, then try again.",
                "needs_permission": True,
            }
        return {"success": False, "error": err}
    return {"success": True}


def mark_items_pushed(entry_id: str, item_ids: list) -> dict:
    """Record that these action items made it into Reminders.

    The actual Reminders write happens in Electron (a headless LaunchAgent can't
    get an Automation consent grant), so the daemon is told after the fact rather
    than doing the push itself.
    """
    entry = get_entry(entry_id)
    if entry is None:
        raise KeyError(entry_id)

    wanted = set(item_ids or [])
    items = entry.get("action_items", [])
    for item in items:
        if item.get("id") in wanted:
            item["pushed"] = True

    update_entry(entry_id, action_items=items)
    return {"marked": sorted(wanted)}


def push_action_items(entry_id: str, item_ids: list = None, list_name: str = "") -> dict:
    """Push some or all of an entry's action items into Reminders."""
    entry = get_entry(entry_id)
    if entry is None:
        raise KeyError(entry_id)

    items = entry.get("action_items", [])
    targets = [i for i in items if item_ids is None or i.get("id") in item_ids]

    pushed, failures = [], []
    for item in targets:
        if item.get("pushed"):
            continue
        res = push_to_reminders(item["title"], item.get("note", ""), item.get("due"), list_name)
        if res.get("success"):
            item["pushed"] = True
            pushed.append(item["id"])
        else:
            failures.append({"id": item["id"], "error": res.get("error"), **({"needs_permission": True} if res.get("needs_permission") else {})})
            # A permission failure will hit every subsequent item identically --
            # stop rather than firing N more doomed osascript calls.
            if res.get("needs_permission"):
                break

    update_entry(entry_id, action_items=items)
    return {"pushed": pushed, "failures": failures}
