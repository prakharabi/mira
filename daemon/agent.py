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
import tools

MAX_TOOL_ROUNDS = 4          # generous enough to search -> read -> answer
TOOL_RESULT_CHAR_LIMIT = 4000

IDENTITY = """You are Mira, {owner}'s personal AI assistant. You run locally on their Mac and are deeply integrated with it.

You are not a generic chatbot. You have real capabilities on this machine and you use them rather than describing them:
{capabilities}

How to behave:
- Act, don't narrate. If asked to do something you have a tool for, call it. Never say "I can't" about anything in the list above.
- Be direct and concise. No preamble, no filler, no restating the question.
- You know today's date via a tool -- check it before reasoning about time.
- If you need current information, search instead of guessing or saying your knowledge is outdated.
- When you learn something durable about {owner}, their business, or their preferences, save it to memory.
- If a request is ambiguous in a way that matters, ask one short question instead of guessing."""


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

    payload = {"model": model, "messages": messages}
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
        "messages": messages,
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
    """Cloud and Ollama describe tool calls slightly differently."""
    calls = msg.get("tool_calls") or []
    out = []
    for i, c in enumerate(calls):
        fn = c.get("function", {}) if isinstance(c, dict) else {}
        name = fn.get("name")
        if not name:
            continue
        out.append({
            "id": c.get("id") or f"call_{i}",
            "name": name,
            "arguments": fn.get("arguments", {}),
        })
    return out


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
    system_prompt = build_system_prompt(user_message, owner=owner, surface=surface)
    messages = [{"role": "system", "content": system_prompt}] + list(history)

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
            return (msg.get("content") or "").strip(), {
                "model_used": used_model, "tools_used": tools_used,
            }

        # record the assistant's tool-call turn verbatim so the model sees its
        # own request alongside the result it gets back
        messages.append({
            "role": "assistant",
            "content": msg.get("content") or "",
            "tool_calls": msg.get("tool_calls"),
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

    text = (msg or {}).get("content", "") if msg else ""
    return (text or "I ran out of steps on that one -- could you narrow it down?"), {
        "model_used": used_model, "tools_used": tools_used,
    }
