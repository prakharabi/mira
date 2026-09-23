"""The reasoning loop behind chat, Telegram and voice.

Before this, each of those three surfaces built its own prompt and called a
model directly, so Mira's personality and self-knowledge differed depending on
where you talked to her -- and none of them knew she could create a reminder or
search the web. They all route through here now, which means a capability added
to tools.py is immediately available everywhere.

The loop is the standard tool-calling shape: send the conversation plus tool
schemas, run whatever the model asks for, feed results back, repeat until it
answers in prose or hits the round cap.
"""

import datetime
import json

import requests

import memory
import tasks
import tools

MAX_TOOL_ROUNDS = 4          # generous enough to search -> read -> answer
# Sized against the free Groq tier's 8000 tokens/minute: at 4000 chars a single
# search-then-read turn spent the whole minute's budget and got rate-limited,
# which pushed every web answer onto the slower local model. Five search
# snippets still fit comfortably here.
TOOL_RESULT_CHAR_LIMIT = 2500

IDENTITY = """You are Mira, {owner}'s personal AI assistant. You run locally on their Mac and are deeply integrated with it.

You are not a generic chatbot. You have real capabilities on this machine and you use them rather than describing them:
{capabilities}

How to behave:
- Act, don't narrate. If asked to do something you have a tool for, call it. Never say "I can't" about anything in the list above.
- Be direct and concise. No preamble, no filler, no restating the question.
- You know today's date via a tool -- check it before reasoning about time.
- If you need current information, search instead of guessing or saying your knowledge is outdated.
- When you learn something durable about {owner}, their business, or their preferences, save it to memory.
- If a request is ambiguous in a way that matters, ask one short question instead of guessing.
- Reply in the language {owner} used -- Hindi in, Hindi out; Hinglish in, reply naturally in the same mixed register rather than switching to pure Hindi or pure English on your own.
- Greetings and anything you say unprompted (a morning digest, a proactive alert, the first line of a fresh conversation) should be personal, not generic -- use {owner}'s name or a natural honorific ("Good morning, {owner}", "Morning, sir" -- match whatever register the rest of the conversation is in) rather than a flat "Hey there" or "Hello". Mid-conversation replies don't need the name repeated every turn -- that reads as stiff, not personal.
- Never claim a tool call failed, or describe why, unless its result actually contains an error -- check for one before saying anything went wrong. If it does, quote or closely paraphrase that specific error text; don't substitute a different-sounding explanation that isn't in the result, even one that sounds plausible or matches something that failed earlier in this same conversation. A scheduling conflict, an invalid email, a missing permission, and "the API is down" are different problems with different fixes, and {owner} can only act on the real one. If a result has no error field, it succeeded -- report what it actually returned, not a guess. If the failure is something you can resolve yourself (e.g. a time slot was taken -- try a different time; a value doesn't match what a listing tool returned -- re-check that list), do that before giving up and offering a workaround."""


def build_system_prompt(user_message: str = "", owner: str = "the user",
                        surface: str = "chat") -> str:
    now = datetime.datetime.now()
    parts = [IDENTITY.format(owner=owner, capabilities=tools.capability_summary())]

    parts.append(f"Current date and time: {now.strftime('%A, %B %d, %Y at %I:%M %p')} (local).")

    mem = memory.build_memory_context(user_message)
    if mem:
        parts.append(
            "What you already know (long-term memory -- treat as established fact, "
            "don't re-ask):\n" + mem
        )

    open_tasks = tasks.build_task_context()
    if open_tasks:
        parts.append(open_tasks)

    if surface == "telegram":
        parts.append(
            "You are replying over Telegram, so the Mac's screen may not be visible to "
            "the user. Keep replies short and mobile-friendly, and confirm actions in words."
        )
    elif surface == "voice":
        parts.append(
            "You are being spoken aloud. Reply in one or two short sentences of plain "
            "prose -- no lists, no markdown, no code."
        )

    return "\n\n".join(parts)


def _truncate(obj) -> str:
    text = json.dumps(obj, ensure_ascii=False, default=str)
    if len(text) > TOOL_RESULT_CHAR_LIMIT:
        return text[:TOOL_RESULT_CHAR_LIMIT] + "...[truncated]"
    return text


def _call_cloud(messages, settings, groq_key, with_tools=True):
    api_key = settings.get("cloud_api_key") or groq_key
    base_url = settings.get("cloud_base_url") or "https://api.groq.com/openai/v1"
    model = settings.get("cloud_model") or "openai/gpt-oss-120b"
    if not api_key:
        return None, {"error": "no cloud API key configured"}

    payload = {"model": model, "messages": _shape_messages(messages, as_string=True)}
    if with_tools:
        payload["tools"] = tools.openai_tool_schemas()
        payload["tool_choice"] = "auto"

    try:
        resp = requests.post(
            f"{base_url.rstrip('/')}/chat/completions",
            headers={"Authorization": f"Bearer {api_key}"},
            json=payload, timeout=90,
        )
        data = resp.json()
    except (requests.RequestException, ValueError) as e:
        return None, {"error": str(e)}

    if "choices" not in data:
        return None, data
    return data["choices"][0]["message"], None


def _call_local(messages, settings, with_tools=True):
    model = settings.get("local_chat_model") or "batiai/gemma4-e2b:q4"
    payload = {
        "model": model,
        "messages": _shape_messages(messages, as_string=False),
        "stream": False,
        "think": False,
        "keep_alive": settings.get("local_model_keep_alive") or "60s",
    }
    if with_tools:
        payload["tools"] = tools.openai_tool_schemas()

    try:
        resp = requests.post("http://localhost:11434/api/chat", json=payload, timeout=180)
        data = resp.json()
    except (requests.RequestException, ValueError) as e:
        return None, {"error": str(e)}

    if "message" not in data:
        return None, data
    return data["message"], None


def _normalize_tool_calls(msg: dict) -> list:
    """Cloud and Ollama describe tool calls slightly differently.

    The difference that matters is `arguments`: the OpenAI wire format sends a
    JSON *string*, Ollama sends an object. Everything downstream works in
    objects, and _shape_tool_calls converts back on the way out.
    """
    calls = msg.get("tool_calls") or []
    out = []
    for i, c in enumerate(calls):
        fn = c.get("function", {}) if isinstance(c, dict) else {}
        name = fn.get("name")
        if not name:
            continue
        args = fn.get("arguments", {})
        if isinstance(args, str):
            try:
                args = json.loads(args) if args.strip() else {}
            except json.JSONDecodeError:
                args = {}
        if not isinstance(args, dict):
            args = {}
        out.append({"id": c.get("id") or f"call_{i}", "name": name, "arguments": args})
    return out


def _shape_tool_calls(calls: list, as_string: bool) -> list:
    """Render normalized calls in whichever dialect the provider expects.

    Replaying a cloud tool call verbatim into Ollama used to send `arguments`
    as a JSON string, which Ollama rejects while parsing the request -- so any
    fallback from cloud to local *after* a tool had run died with a parse error
    instead of answering. That is the exact path a rate-limited free tier takes,
    which made it the common case rather than an edge one.
    """
    shaped = []
    for c in calls:
        args = c["arguments"]
        shaped.append({
            "id": c["id"],
            "type": "function",
            "function": {
                "name": c["name"],
                "arguments": json.dumps(args, ensure_ascii=False) if as_string else args,
            },
        })
    return shaped


def _shape_messages(messages: list, as_string: bool) -> list:
    out = []
    for m in messages:
        if m.get("role") == "assistant" and m.get("_calls"):
            out.append({"role": "assistant", "content": m.get("content") or "",
                        "tool_calls": _shape_tool_calls(m["_calls"], as_string)})
        else:
            out.append({k: v for k, v in m.items() if k != "_calls"})
    return out



def _final_text(msg, messages, settings, groq_key, use_cloud, tools_used) -> str:
    """Get prose out of a message that has finished calling tools.

    Reasoning models (gpt-oss among them) sometimes put everything in a
    `reasoning` field and leave `content` null, especially on the turn straight
    after a tool result. That used to come back as an empty reply -- shown as a
    blank bubble, saved to history, and thereafter rejected by the API, so a
    single blank answer bricked the conversation. So: ask once more with tools
    off, and if even that is empty, say what was actually done rather than
    nothing.
    """
    text = (msg.get("content") or "").strip()
    if text:
        return text

    retry = _call_cloud(messages, settings, groq_key, with_tools=False)[0] if use_cloud \
        else _call_local(messages, settings, with_tools=False)[0]
    text = ((retry or {}).get("content") or "").strip()
    if text:
        return text

    if tools_used:
        # "A tool ran" is not "the tool succeeded" -- this used to return
        # "Done." unconditionally here, which was a confident lie whenever
        # the model went silent (the reasoning-field quirk above) right
        # after a tool call that actually FAILED (e.g. create_calcom_booking
        # hitting a real Cal.com error). Walk back over this round's tool
        # results -- the unbroken run of "tool" messages at the end of the
        # transcript -- and if any of them came back with an error, say so
        # instead of declaring victory over a failure nobody saw.
        recent_errors = []
        for m in reversed(messages):
            if m.get("role") != "tool":
                break
            try:
                payload = json.loads(m.get("content") or "{}")
            except (json.JSONDecodeError, TypeError):
                continue
            if isinstance(payload, dict) and payload.get("error"):
                recent_errors.append(str(payload["error"]))
        if recent_errors:
            return "That didn't actually go through -- " + recent_errors[0]
        # Confirming plainly beats both a blank bubble and a recital of
        # internal tool names, but only once we know nothing here failed.
        return "Done."
    return "I didn't get a usable answer back that time. Try rephrasing?"


def run_agent(history: list, user_message: str, model_pref: str = "auto",
              settings: dict = None, groq_key: str = "", surface: str = "chat",
              owner: str = None):
    """Run one turn, tools and all.

    `history` is the persisted conversation WITHOUT any system prompt -- the
    system prompt is rebuilt per turn so memory and the current time are always
    fresh, and never gets saved into the stored history.

    Returns (reply_text, meta) where meta records which model answered and
    which tools ran, so the UI can show what actually happened.
    """
    settings = settings or {}
    # Owner name is configuration, not something to hardcode in a project meant
    # to be cloned by other people.
    owner = owner or settings.get("owner_name") or "the user"
    tools.set_surface(surface)
    system_prompt = build_system_prompt(user_message, owner=owner, surface=surface)
    # Drop empty assistant turns. A stored "" (see _final_text below for how one
    # used to get written) makes the cloud API reject the whole request, which
    # turned one bad reply into a session that could never be used again.
    clean_history = [m for m in history
                     if m.get("role") != "assistant" or (m.get("content") or "").strip()]
    messages = [{"role": "system", "content": system_prompt}] + clean_history

    # Local models below ~4B are unreliable at tool calling and will often emit
    # malformed calls or loop. Tools are offered to the cloud model, and to the
    # local one only as a best effort -- if it never emits a call, the loop just
    # returns its prose on the first pass.
    use_cloud = model_pref == "cloud" or (model_pref == "auto")
    tools_used = []
    used_model = "cloud" if use_cloud else "local"

    for _round in range(MAX_TOOL_ROUNDS):
        if use_cloud:
            msg, err = _call_cloud(messages, settings, groq_key)
            if msg is None:
                # fall back to local for the rest of this turn
                use_cloud = False
                used_model = "local"
                msg, err = _call_local(messages, settings)
                if msg is None:
                    return None, {"error": err, "tools_used": tools_used}
        else:
            msg, err = _call_local(messages, settings)
            if msg is None:
                return None, {"error": err, "tools_used": tools_used}

        calls = _normalize_tool_calls(msg)
        if not calls:
            text = _final_text(msg, messages, settings, groq_key, use_cloud, tools_used)
            return text, {"model_used": used_model, "tools_used": tools_used}

        # record the assistant's tool-call turn verbatim so the model sees its
        # own request alongside the result it gets back
        messages.append({
            "role": "assistant",
            "content": msg.get("content") or "",
            "_calls": calls,
        })

        for call in calls:
            result = tools.execute(call["name"], call["arguments"])
            tools_used.append(call["name"])
            messages.append({
                "role": "tool",
                "tool_call_id": call["id"],
                "name": call["name"],
                "content": _truncate(result),
            })

    # Round cap hit: ask once more, tools disabled, so the user gets prose
    # rather than a silent failure or an endless tool loop.
    if use_cloud:
        msg, _ = _call_cloud(messages, settings, groq_key, with_tools=False)
    else:
        msg, _ = _call_local(messages, settings, with_tools=False)

    text = ((msg or {}).get("content") or "").strip()
    if not text and tools_used:
        text = "Done."
    return (text or "I ran out of steps on that one -- could you narrow it down?"), {
        "model_used": used_model, "tools_used": tools_used,
    }
