"""Commitments Mira is holding on to, across every place you talk to her.

This is the piece that makes the three surfaces feel like one assistant rather
than three. You mention on Telegram that you still owe someone a deck; two days
later you open the app and ask "what am I forgetting?" -- without this, that
sentence lives only in the Telegram transcript and nothing connects them.

A task here is deliberately NOT a macOS reminder. Reminders are for things with
a time, that you want to be alarmed about. These are open loops: things stated
in passing that should stay visible until they're closed, whether or not they
ever had a date. They get surfaced three ways:

  * injected into the system prompt, so any surface can answer "what's open?"
  * nudged about by proactive.py when one goes stale or comes due
  * listed and closed by hand in the Tasks view

Storage matches memory.py -- one JSON file, atomic writes, lexical matching --
for the same reason: it has to stay dependency-free and fast at the scale one
person's open loops actually reach.
"""

import datetime
import json
import re
import threading
import uuid
from pathlib import Path

TASKS_DIR = Path.home() / "Mira" / "daemon" / "memory_store"
TASKS_DIR.mkdir(parents=True, exist_ok=True)
TASKS_PATH = TASKS_DIR / "tasks.json"

STATUSES = ("open", "done", "dropped")
MAX_TITLE_CHARS = 200
MAX_DETAIL_CHARS = 600

# How many open tasks travel in the system prompt. Small on purpose: the point
# is for Mira to know they exist and be able to bring them up, not to spend a
# third of the context window on a to-do list.
PROMPT_TASK_LIMIT = 8

# A task nobody has touched in this long is worth one mention.
STALE_AFTER_DAYS = 3
# ...and then not again for this long, so a task you're ignoring on purpose
# doesn't turn into a daily lecture.
RENUDGE_AFTER_DAYS = 4

_lock = threading.Lock()

_STOPWORDS = {
    "the", "a", "an", "to", "for", "of", "and", "or", "on", "in", "at", "by",
    "with", "from", "is", "are", "be", "i", "me", "my", "we", "our", "you",
    "your", "it", "that", "this", "need", "needs", "should", "must", "have",
    "has", "get", "got", "do", "does", "did", "task", "todo",
}


def _now() -> str:
    return datetime.datetime.now().isoformat(timespec="seconds")


def _load() -> list:
    if not TASKS_PATH.exists():
        return []
    try:
        data = json.loads(TASKS_PATH.read_text())
        return data if isinstance(data, list) else []
    except (json.JSONDecodeError, OSError):
        return []


def _save(items: list):
    tmp = TASKS_PATH.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(items, indent=2, ensure_ascii=False))
    tmp.replace(TASKS_PATH)  # atomic -- a crash mid-write must not drop a commitment


def _tokens(text: str) -> set:
    words = re.findall(r"[a-z0-9]+", (text or "").lower())
    return {w for w in words if w not in _STOPWORDS and len(w) > 1}


def _age_days(iso: str) -> float:
    if not iso:
        return 0.0
    try:
        then = datetime.datetime.fromisoformat(iso)
    except ValueError:
        return 0.0
    return (datetime.datetime.now() - then).total_seconds() / 86400


def _parse_due(due: str):
    """Accept 'YYYY-MM-DD', a full ISO timestamp, or nothing.

    Anything vaguer than that ("next week") is left to the model to resolve
    into a date before it calls us -- it has a clock tool for exactly this.
    """
    due = (due or "").strip()
    if not due:
        return None
    try:
        return datetime.datetime.fromisoformat(due.replace("Z", "+00:00")).isoformat(
            timespec="seconds")
    except ValueError:
        pass
    try:
        return datetime.datetime.strptime(due[:10], "%Y-%m-%d").isoformat(timespec="seconds")
    except ValueError:
        return None


# ---------- core operations ----------


def add_task(title: str, detail: str = "", due: str = "", surface: str = "chat",
             session_id: str = "") -> dict:
    """Record an open loop. Restating an existing one updates it instead.

    The duplicate guard matters more here than in memory: the same commitment
    genuinely does get mentioned repeatedly ("still need to send that deck"),
    and a list that grows a new row every time you mention the same thing is a
    list you stop reading.
    """
    title = (title or "").strip()[:MAX_TITLE_CHARS]
    if not title:
        raise ValueError("a task needs a title")

    incoming = _tokens(title)
    with _lock:
        items = _load()

        for t in items:
            if t.get("status") != "open":
                continue
            other = _tokens(t["title"])
            same = t["title"].strip().lower() == title.lower()
            overlap = (len(incoming & other) / max(len(incoming | other), 1)
                       if incoming and other else 0)
            if same or overlap > 0.7:
                t["title"] = title
                if detail:
                    t["detail"] = detail.strip()[:MAX_DETAIL_CHARS]
                parsed = _parse_due(due)
                if parsed:
                    t["due"] = parsed
                t["mentions"] = t.get("mentions", 1) + 1
                t["updated_at"] = _now()
                _save(items)
                return t

        task = {
            "id": uuid.uuid4().hex[:12],
            "title": title,
            "detail": (detail or "").strip()[:MAX_DETAIL_CHARS],
            "status": "open",
            "due": _parse_due(due),
            "surface": surface or "chat",
            "session_id": session_id or "",
            "mentions": 1,
            "created_at": _now(),
            "updated_at": _now(),
            "completed_at": None,
            "last_nudged_at": None,
        }
        items.append(task)
        _save(items)
        return task


def list_tasks(status: str = "open", limit: int = 100) -> list:
    """Open tasks first by due date (undated last), then oldest first.

    Oldest-first is the right order for open loops: the thing you've been
    avoiding longest is the one worth showing at the top.
    """
    items = [t for t in _load() if status in (None, "all") or t.get("status") == status]
    if status == "open":
        items.sort(key=lambda t: (t.get("due") is None, t.get("due") or "",
                                  t.get("created_at") or ""))
    else:
        items.sort(key=lambda t: t.get("updated_at") or "", reverse=True)
    return items[:limit]


def find_task(query: str):
    """Best open task matching a phrase, for closing one by description."""
    q = _tokens(query)
    if not q:
        return None
    best, best_score = None, 0.0
    for t in _load():
        if t.get("status") != "open":
            continue
        if query.strip().lower() in t["title"].lower():
            return t
        other = _tokens(t["title"])
        if not other:
            continue
        score = len(q & other) / max(len(q | other), 1)
        if score > best_score:
            best, best_score = t, score
    return best if best_score >= 0.3 else None


def set_status(task_id: str, status: str) -> dict:
    if status not in STATUSES:
        raise ValueError(f"unknown status '{status}'")
    with _lock:
        items = _load()
        for t in items:
            if t["id"] != task_id:
                continue
            t["status"] = status
            t["updated_at"] = _now()
            t["completed_at"] = _now() if status == "done" else None
            _save(items)
            return t
    raise KeyError(task_id)


def update_task(task_id: str, title: str = None, detail: str = None,
                due: str = None) -> dict:
    with _lock:
        items = _load()
        for t in items:
            if t["id"] != task_id:
                continue
            if title is not None:
                t["title"] = title.strip()[:MAX_TITLE_CHARS]
            if detail is not None:
                t["detail"] = detail.strip()[:MAX_DETAIL_CHARS]
            if due is not None:
                t["due"] = _parse_due(due)
            t["updated_at"] = _now()
            _save(items)
            return t
    raise KeyError(task_id)


def delete_task(task_id: str) -> bool:
    with _lock:
        items = _load()
        remaining = [t for t in items if t["id"] != task_id]
        if len(remaining) == len(items):
            return False
        _save(remaining)
        return True


def mark_nudged(task_ids: list):
    with _lock:
        items = _load()
        stamp = _now()
        wanted = set(task_ids)
        for t in items:
            if t["id"] in wanted:
                t["last_nudged_at"] = stamp
        _save(items)


# ---------- surfacing ----------


def build_task_context() -> str:
    """The open-tasks block for the system prompt.

    Returns "" when nothing is open, so the prompt doesn't carry an empty
    header saying the user has no commitments -- which reads oddly and costs
    tokens for nothing.
    """
    items = list_tasks("open", limit=PROMPT_TASK_LIMIT)
    if not items:
        return ""

    today = datetime.date.today()
    lines = []
    for t in items:
        line = f"  - {t['title']}"
        if t.get("due"):
            try:
                due_date = datetime.datetime.fromisoformat(t["due"]).date()
                days = (due_date - today).days
                if days < 0:
                    line += f" (OVERDUE by {abs(days)}d)"
                elif days == 0:
                    line += " (due today)"
                elif days == 1:
                    line += " (due tomorrow)"
                else:
                    line += f" (due {due_date.strftime('%b %d')})"
            except ValueError:
                pass
        where = t.get("surface")
        if where and where != "chat":
            line += f" [from {where}]"
        lines.append(line)

    return ("Open commitments you are tracking for the user (raised across chat, "
            "Telegram and voice -- bring one up only when it is relevant, and mark "
            "it done when they say it is):\n" + "\n".join(lines))


def due_or_stale() -> list:
    """Tasks worth interrupting about: due today, overdue, or long untouched."""
    today = datetime.date.today()
    out = []

    for t in list_tasks("open", limit=200):
        # Never nudge twice inside the cooloff, whatever the reason.
        if t.get("last_nudged_at") and _age_days(t["last_nudged_at"]) < RENUDGE_AFTER_DAYS:
            continue

        if t.get("due"):
            try:
                due_date = datetime.datetime.fromisoformat(t["due"]).date()
            except ValueError:
                due_date = None
            if due_date and due_date <= today:
                overdue = (today - due_date).days
                label = "due today" if overdue == 0 else f"{overdue}d overdue"
                out.append((t, f"⏳ {t['title']} — {label}"))
                continue

        # Undated tasks only ever get the stale nudge, and only once per
        # cooloff, so an open loop you're deliberately sitting on stays quiet.
        if not t.get("due") and _age_days(t.get("updated_at") or t.get("created_at")) >= STALE_AFTER_DAYS:
            days = int(_age_days(t.get("updated_at") or t.get("created_at")))
            out.append((t, f"📌 Still open since {days}d ago: {t['title']}"))

    return out


def stats() -> dict:
    items = _load()
    return {
        "open": sum(1 for t in items if t.get("status") == "open"),
        "done": sum(1 for t in items if t.get("status") == "done"),
        "dropped": sum(1 for t in items if t.get("status") == "dropped"),
    }
