"""Mira's capabilities, as tools a model can actually call.

Until now Mira had plenty of features -- Dump Box, Reminders, OCR, meetings,
automations, Google -- but the model answering in chat/Telegram/voice knew
about none of them. It would say "I can't create reminders" while the app it
was running inside had been creating reminders for weeks.

This module is the single place that fixes that. Every capability is declared
once, with a JSON schema the model can call and a Python function that runs it.
The same registry drives:

  * tool calling for models that support it (the cloud model, and Ollama models
    that expose a tools API), and
  * the plain-English capability summary injected into the system prompt, so
    even a small local model with no tool support at least knows what Mira can
    do and can tell the user to ask for it.

Anything needing Apple Events (Reminders) is routed through actions.py to
Electron -- see that module for why.
"""

import datetime
import json

import actions
import app_control
import memory
import tasks
import web

# Which surface the current turn came from. Set by agent.py before a turn runs,
# so a task raised over Telegram is recorded as such rather than as "chat".
_CURRENT_SURFACE = {"name": "chat"}


def set_surface(name: str):
    _CURRENT_SURFACE["name"] = name or "chat"


# ---------- individual tool implementations ----------


def _tool_get_datetime(**_):
    now = datetime.datetime.now()
    return {"datetime": now.strftime("%A, %B %d, %Y at %I:%M %p"),
            "iso": now.isoformat(timespec="seconds")}


def _tool_web_search(query: str = "", max_results: int = 5, **_):
    from main import load_settings  # local import: main imports this module
    key = load_settings().get("tavily_api_key", "")
    res = web.search(query, max_results=min(int(max_results or 5), 8), tavily_key=key)
    if res["error"] and not res["results"]:
        return {"error": res["error"], "results": []}
    return {"provider": res["provider"], "results": res["results"]}


def _tool_read_webpage(url: str = "", **_):
    return web.fetch_page(url)


def _tool_remember(text: str = "", category: str = "fact", **_):
    try:
        fact = memory.add_fact(text, category, source="assistant")
        return {"stored": True, "fact": fact["text"], "category": fact["category"]}
    except ValueError as e:
        return {"stored": False, "error": str(e)}


def _tool_recall(query: str = "", **_):
    hits = memory.search_facts(query, limit=10)
    return {"facts": [{"text": f["text"], "category": f["category"]} for f in hits]}


def _tool_create_reminder(title: str = "", note: str = "", due_date: str = "", **_):
    if not title.strip():
        return {"success": False, "error": "a reminder needs a title"}
    action_id = actions.enqueue("create_reminder", {
        "title": title.strip(), "note": note or "", "due": due_date or None,
    })
    result = actions.wait_for(action_id, timeout=10)
    if result is None:
        return {"success": False,
                "error": "Queued, but the Mira app didn't confirm in time. "
                         "It may still appear in Reminders."}
    if not result.get("success"):
        return {"success": False, "error": result.get("error", "could not create reminder")}
    return {"success": True, "title": title, "due": due_date or None}


def _tool_add_to_dump_box(text: str = "", **_):
    import dumpbox
    try:
        entry = dumpbox.add_entry(text)
        return {"success": True, "id": entry["id"]}
    except ValueError as e:
        return {"success": False, "error": str(e)}


def _tool_list_calendar(days: int = 7, **_):
    import google_integration as g
    if not g.is_connected():
        return {"error": "Google account isn't connected. Connect it in Mira > Google."}
    try:
        return {"events": g.calendar_list_events(int(days or 7))}
    except Exception as e:
        return {"error": str(e)}


def _tool_create_calendar_event(summary: str = "", start: str = "", end: str = "",
                                description: str = "", location: str = "",
                                attendees: list = None, add_meet: bool = False, **_):
    import google_integration as g
    if not g.is_connected():
        return {"error": "Google account isn't connected. Connect it in Mira > Google."}
    try:
        return g.calendar_create_event(summary, start, end, description,
                                       location, attendees, add_meet)
    except Exception as e:
        return {"error": str(e)}


def _tool_search_email(query: str = "", max_results: int = 8, **_):
    import google_integration as g
    if not g.is_connected():
        return {"error": "Google account isn't connected. Connect it in Mira > Google."}
    try:
        return {"messages": g.gmail_list(query, min(int(max_results or 8), 15))}
    except Exception as e:
        return {"error": str(e)}


def _tool_draft_email(to: str = "", subject: str = "", body: str = "", **_):
    import google_integration as g
    if not g.is_connected():
        return {"error": "Google account isn't connected. Connect it in Mira > Google."}
    if not to.strip():
        return {"error": "an email needs a recipient"}
    try:
        return g.gmail_create_draft(to, subject, body)
    except Exception as e:
        return {"error": str(e)}


def _tool_list_calcom_bookings(status: str = "upcoming", **_):
    import calcom_integration as c
    if not c.is_connected():
        return {"error": "Cal.com isn't connected. Add an API key in Settings."}
    try:
        return {"bookings": c.list_bookings(status)}
    except Exception as e:
        return {"error": str(e)}


def _tool_create_calcom_booking(event_type: str = "", start: str = "",
                                attendee_name: str = "", attendee_email: str = "", **_):
    import calcom_integration as c
    if not c.is_connected():
        return {"error": "Cal.com isn't connected. Add an API key in Settings."}
    try:
        return c.create_booking(event_type, start, attendee_name, attendee_email)
    except Exception as e:
        return {"error": str(e)}


def _tool_cancel_calcom_booking(booking_uid: str = "", reason: str = "", **_):
    import calcom_integration as c
    if not c.is_connected():
        return {"error": "Cal.com isn't connected. Add an API key in Settings."}
    try:
        return c.cancel_booking(booking_uid, reason)
    except Exception as e:
        return {"error": str(e)}


def _tool_reschedule_calcom_booking(booking_uid: str = "", new_start: str = "",
                                    reason: str = "", **_):
    import calcom_integration as c
    if not c.is_connected():
        return {"error": "Cal.com isn't connected. Add an API key in Settings."}
    try:
        return c.reschedule_booking(booking_uid, new_start, reason)
    except Exception as e:
        return {"error": str(e)}


def _tool_open_app(name: str = "", **_):
    return app_control.open_app(name)


def _tool_open_url(url: str = "", **_):
    return app_control.open_url(url)


def _tool_control_music(action: str = "", **_):
    return app_control.media_control(action)


def _tool_set_volume(level: int = 50, **_):
    return app_control.set_volume(level)


def _tool_now_playing(**_):
    return app_control.now_playing()



def _tool_track_task(title: str = "", detail: str = "", due_date: str = "", **_):
    try:
        task = tasks.add_task(title, detail=detail, due=due_date,
                              surface=_CURRENT_SURFACE.get("name", "chat"))
    except ValueError as e:
        return {"tracked": False, "error": str(e)}
    return {"tracked": True, "id": task["id"], "title": task["title"],
            "due": task["due"], "already_tracked": task["mentions"] > 1}


def _tool_list_open_tasks(**_):
    items = tasks.list_tasks("open", limit=25)
    return {"count": len(items),
            "tasks": [{"id": t["id"], "title": t["title"], "due": t["due"],
                       "raised_on": t["surface"]} for t in items]}


def _tool_complete_task(task: str = "", **_):
    match = tasks.find_task(task)
    if not match:
        return {"completed": False,
                "error": f"no open task matching '{task}' -- list them first if unsure"}
    tasks.set_status(match["id"], "done")
    return {"completed": True, "title": match["title"]}


def _tool_list_automations(**_):
    import automations
    items = automations.list_automations()
    return {"automations": [{"name": a["name"], "description": a.get("description", "")}
                            for a in items]}


def _tool_run_automation(name: str = "", **_):
    import automations
    match = automations.match_automation(name)
    if not match:
        return {"success": False,
                "error": f"No automation clearly matches '{name}'. Ask the user which one."}
    result = automations.run_automation(match["id"])
    return {"success": bool(result.get("success")), "automation": match["name"],
            "error": result.get("error", "")}


# ---------- registry ----------
# `summary` is what a non-tool-calling model sees in its system prompt, so it
# must read as a plain capability statement, not as API documentation.

TOOLS = [
    {
        "name": "get_current_datetime",
        "summary": "know the current date and time",
        "description": "Get the current local date and time. Use before any reasoning about 'today', 'tomorrow', deadlines, or scheduling.",
        "parameters": {"type": "object", "properties": {}},
        "fn": _tool_get_datetime,
    },
    {
        "name": "web_search",
        "summary": "search the internet for current information",
        "description": "Search the web for current information, news, facts, prices, or anything after your training cutoff.",
        "parameters": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "The search query"},
                "max_results": {"type": "integer", "description": "How many results (default 5)"},
            },
            "required": ["query"],
        },
        "fn": _tool_web_search,
    },
    {
        "name": "read_webpage",
        "summary": "open and read a web page in full",
        "description": "Fetch a URL and read its text. Use after web_search when a snippet isn't enough to answer properly.",
        "parameters": {
            "type": "object",
            "properties": {"url": {"type": "string", "description": "Full http(s) URL"}},
            "required": ["url"],
        },
        "fn": _tool_read_webpage,
    },
    {
        "name": "remember",
        "summary": "save something to long-term memory",
        "description": "Store a durable fact about the user, their business, products, people, or preferences. Use when the user tells you something worth recalling later.",
        "parameters": {
            "type": "object",
            "properties": {
                "text": {"type": "string", "description": "The fact, written standalone in third person"},
                "category": {"type": "string", "enum": memory.CATEGORIES},
            },
            "required": ["text"],
        },
        "fn": _tool_remember,
    },
    {
        "name": "recall",
        "summary": "look things up in long-term memory",
        "description": "Search long-term memory for what you already know about a topic, person, or project.",
        "parameters": {
            "type": "object",
            "properties": {"query": {"type": "string"}},
            "required": ["query"],
        },
        "fn": _tool_recall,
    },
    {
        "name": "create_reminder",
        "summary": "create reminders in the macOS Reminders app",
        "description": "Create a reminder in the user's macOS Reminders app.",
        "parameters": {
            "type": "object",
            "properties": {
                "title": {"type": "string", "description": "Short imperative task"},
                "note": {"type": "string"},
                "due_date": {"type": "string", "description": "YYYY-MM-DD, only if a date is implied"},
            },
            "required": ["title"],
        },
        "fn": _tool_create_reminder,
    },
    {
        "name": "add_to_dump_box",
        "summary": "capture notes into the Dump Box for later processing",
        "description": "Save freeform text into Mira's Dump Box, where it can later be summarized into action items.",
        "parameters": {
            "type": "object",
            "properties": {"text": {"type": "string"}},
            "required": ["text"],
        },
        "fn": _tool_add_to_dump_box,
    },
    {
        "name": "list_calendar_events",
        "summary": "read the user's Google Calendar",
        "description": "List upcoming Google Calendar events.",
        "parameters": {
            "type": "object",
            "properties": {"days": {"type": "integer", "description": "How many days ahead (default 7)"}},
        },
        "fn": _tool_list_calendar,
    },
    {
        "name": "create_calendar_event",
        "summary": "schedule meetings and add events to Google Calendar, optionally with a Google Meet link",
        "description": "Create an event on the user's primary Google Calendar. "
                       "Set add_meet to true to attach a Google Meet video call to it -- "
                       "this is how to 'schedule a meeting' or 'set up a Google Meet' rather than "
                       "just a plain calendar entry.",
        "parameters": {
            "type": "object",
            "properties": {
                "summary": {"type": "string"},
                "start": {"type": "string", "description": "ISO 8601 start datetime"},
                "end": {"type": "string", "description": "ISO 8601 end datetime"},
                "description": {"type": "string"},
                "location": {"type": "string"},
                "attendees": {"type": "array", "items": {"type": "string"},
                             "description": "Email addresses to invite"},
                "add_meet": {"type": "boolean",
                            "description": "Attach a Google Meet video call to the event"},
            },
            "required": ["summary", "start"],
        },
        "fn": _tool_create_calendar_event,
    },
    {
        "name": "search_email",
        "summary": "search and read Gmail",
        "description": "Search the user's Gmail. Supports Gmail query syntax like 'from:x is:unread'.",
        "parameters": {
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "max_results": {"type": "integer"},
            },
        },
        "fn": _tool_search_email,
    },
    {
        "name": "draft_email",
        "summary": "draft emails in Gmail (e.g. to send someone meeting details)",
        "description": "Create a Gmail draft addressed to someone -- this is how to prepare a meeting "
                       "invite, follow-up, or any other email for the user. It only ever creates a "
                       "DRAFT for the user to review and send themselves; Mira never sends email on "
                       "the user's behalf.",
        "parameters": {
            "type": "object",
            "properties": {
                "to": {"type": "string", "description": "Recipient email address"},
                "subject": {"type": "string"},
                "body": {"type": "string"},
            },
            "required": ["to", "subject", "body"],
        },
        "fn": _tool_draft_email,
    },
    {
        "name": "list_calcom_bookings",
        "summary": "check bookings on the user's Cal.com page",
        "description": "List bookings from the user's Cal.com scheduling page.",
        "parameters": {
            "type": "object",
            "properties": {
                "status": {"type": "string",
                          "description": "upcoming (default), recurring, past, cancelled, or unconfirmed"},
            },
        },
        "fn": _tool_list_calcom_bookings,
    },
    {
        "name": "create_calcom_booking",
        "summary": "book a slot on the user's Cal.com page for someone",
        "description": "Create a booking on the user's Cal.com page. event_type is the event's name or "
                       "slug as it appears on their Cal.com page (e.g. '30 Min Meeting') -- list it with "
                       "list_calcom_bookings or ask the user if unsure.",
        "parameters": {
            "type": "object",
            "properties": {
                "event_type": {"type": "string"},
                "start": {"type": "string", "description": "ISO 8601 start datetime, UTC"},
                "attendee_name": {"type": "string"},
                "attendee_email": {"type": "string"},
            },
            "required": ["event_type", "start", "attendee_name", "attendee_email"],
        },
        "fn": _tool_create_calcom_booking,
    },
    {
        "name": "cancel_calcom_booking",
        "summary": "cancel a Cal.com booking",
        "description": "Cancel an existing booking on the user's Cal.com page, by its booking uid "
                       "(from list_calcom_bookings).",
        "parameters": {
            "type": "object",
            "properties": {
                "booking_uid": {"type": "string"},
                "reason": {"type": "string"},
            },
            "required": ["booking_uid"],
        },
        "fn": _tool_cancel_calcom_booking,
    },
    {
        "name": "reschedule_calcom_booking",
        "summary": "reschedule a Cal.com booking",
        "description": "Move an existing Cal.com booking to a new time, by its booking uid "
                       "(from list_calcom_bookings).",
        "parameters": {
            "type": "object",
            "properties": {
                "booking_uid": {"type": "string"},
                "new_start": {"type": "string", "description": "ISO 8601 new start datetime, UTC"},
                "reason": {"type": "string"},
            },
            "required": ["booking_uid", "new_start"],
        },
        "fn": _tool_reschedule_calcom_booking,
    },
    {
        "name": "open_app",
        "summary": "open apps on the Mac",
        "description": "Open or focus a macOS application by name.",
        "parameters": {
            "type": "object",
            "properties": {"name": {"type": "string", "description": "App name, e.g. Spotify, Safari, Notes"}},
            "required": ["name"],
        },
        "fn": _tool_open_app,
    },
    {
        "name": "open_url",
        "summary": "open websites and deep links",
        "description": "Open a URL in the default browser, or an app deep link.",
        "parameters": {
            "type": "object",
            "properties": {"url": {"type": "string"}},
            "required": ["url"],
        },
        "fn": _tool_open_url,
    },
    {
        "name": "control_music",
        "summary": "control music playback (play, pause, skip, volume)",
        "description": "Control whatever is playing audio -- Spotify, Music, a browser -- using system media keys.",
        "parameters": {
            "type": "object",
            "properties": {
                "action": {"type": "string",
                           "enum": ["playpause", "next", "previous", "volumeup", "volumedown", "mute"]},
            },
            "required": ["action"],
        },
        "fn": _tool_control_music,
    },
    {
        "name": "set_volume",
        "summary": "set the system volume",
        "description": "Set the Mac's output volume to a level from 0 to 100.",
        "parameters": {
            "type": "object",
            "properties": {"level": {"type": "integer"}},
            "required": ["level"],
        },
        "fn": _tool_set_volume,
    },
    {
        "name": "now_playing",
        "summary": "see what music is currently playing",
        "description": "Report the currently playing track from Spotify or Music.",
        "parameters": {"type": "object", "properties": {}},
        "fn": _tool_now_playing,
    },
    {
        "name": "track_task",
        "summary": "remember an open commitment and bring it back up later",
        "description": ("Track something the user has committed to or still owes, so it "
                        "stays visible across chat, Telegram and voice until it is done. "
                        "Use for open loops without a fixed alarm time; use create_reminder "
                        "instead when they want to be alerted at a specific time."),
        "parameters": {
            "type": "object",
            "properties": {
                "title": {"type": "string", "description": "The commitment, e.g. 'Send the pricing deck to Ravi'"},
                "detail": {"type": "string", "description": "Any useful context"},
                "due_date": {"type": "string", "description": "YYYY-MM-DD if there is a deadline"},
            },
            "required": ["title"],
        },
        "fn": _tool_track_task,
    },
    {
        "name": "list_open_tasks",
        "summary": "list what the user still has open",
        "description": "List the open commitments being tracked, whichever surface they were raised on.",
        "parameters": {"type": "object", "properties": {}},
        "fn": _tool_list_open_tasks,
    },
    {
        "name": "complete_task",
        "summary": "close out a task the user has finished",
        "description": "Mark a tracked task done. Matches on a description, not an id.",
        "parameters": {
            "type": "object",
            "properties": {"task": {"type": "string", "description": "Description of the finished task"}},
            "required": ["task"],
        },
        "fn": _tool_complete_task,
    },
    {
        "name": "list_automations",
        "summary": "list the user's n8n automations",
        "description": "List the automations (webhooks) the user has configured.",
        "parameters": {"type": "object", "properties": {}},
        "fn": _tool_list_automations,
    },
    {
        "name": "run_automation",
        "summary": "trigger an n8n automation",
        "description": "Run one of the user's configured automations by name.",
        "parameters": {
            "type": "object",
            "properties": {"name": {"type": "string"}},
            "required": ["name"],
        },
        "fn": _tool_run_automation,
    },
]

_BY_NAME = {t["name"]: t for t in TOOLS}


def openai_tool_schemas() -> list:
    """Tool definitions in the OpenAI/Ollama function-calling format."""
    return [{
        "type": "function",
        "function": {
            "name": t["name"],
            "description": t["description"],
            "parameters": t["parameters"],
        },
    } for t in TOOLS]


def capability_summary() -> str:
    """Plain-English list of what Mira can do, for the system prompt."""
    return "\n".join(f"- {t['summary']}" for t in TOOLS)


def execute(name: str, arguments) -> dict:
    """Run one tool call. Never raises -- a tool failure has to come back to the
    model as a result it can talk about, not as an exception that kills the turn."""
    tool = _BY_NAME.get(name)
    if not tool:
        return {"error": f"unknown tool '{name}'"}

    if isinstance(arguments, str):
        try:
            arguments = json.loads(arguments) if arguments.strip() else {}
        except json.JSONDecodeError:
            return {"error": f"could not parse arguments for '{name}'"}
    if not isinstance(arguments, dict):
        arguments = {}

    try:
        return tool["fn"](**arguments)
    except TypeError as e:
        return {"error": f"bad arguments for '{name}': {e}"}
    except Exception as e:
        return {"error": f"'{name}' failed: {e}"}
