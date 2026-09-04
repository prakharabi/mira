from fastapi import FastAPI, Body, UploadFile, File
from fastapi.responses import FileResponse, StreamingResponse
from contextlib import asynccontextmanager
from dotenv import load_dotenv
import requests
import os
import json
import subprocess
import uuid
import time
import datetime
import re
from pathlib import Path

# Load variables from the .env file (like GROQ_API_KEY) into the environment
load_dotenv()
GROQ_API_KEY = os.getenv("GROQ_API_KEY")


@asynccontextmanager
async def lifespan(app: FastAPI):
    # startup
    print("[main] STARTUP EVENT FIRED - attempting to start wakeword listener", flush=True)
    try:
        from wakeword_listener import start_wakeword_listener_background
        start_wakeword_listener_background()
        print("[main] wakeword listener start call completed", flush=True)
    except Exception as e:
        import traceback
        print(f"[main] Could not start wake-word listener: {e}", flush=True)
        print(traceback.format_exc(), flush=True)

    try:
        from personalization import bootstrap_from_chat_history
        bootstrap_from_chat_history()
        print("[main] predictive typing personalization bootstrapped", flush=True)
    except Exception as e:
        print(f"[main] Could not bootstrap personalization: {e}", flush=True)

    yield
    # shutdown (nothing needed here currently)


app = FastAPI(lifespan=lifespan)

# ---------- Settings (voice_response_enabled toggle, model config, etc) ----------
SETTINGS_PATH = Path.home() / "Mira" / "daemon" / "settings.json"

DEFAULT_SETTINGS = {
    "voice_response_enabled": True,
    "wakeword_enabled": True,
    # local model used for /chat and /generate-minutes (Ollama model name).
    # /ask and /complete (predictive typing) intentionally stay on their own
    # fixed fast models -- they're latency-sensitive and not exposed here.
    "local_chat_model": "batiai/gemma4-e2b:q4",
    # cloud LLM: "groq" uses the bundled Groq preset (falls back to GROQ_API_KEY
    # from .env if cloud_api_key is blank); "custom" hits any OpenAI-compatible
    # /chat/completions endpoint with the given base URL, key, and model name.
    "cloud_provider": "groq",
    "cloud_api_key": "",
    "cloud_base_url": "https://api.groq.com/openai/v1",
    "cloud_model": "openai/gpt-oss-120b",
    # STT: which engine to try first; the other is always used as a fallback.
    "stt_prefer": "local",           # "local" | "cloud"
    "transcription_mode": "translate",  # "translate" (-> English) | "native" (original language)
    # TTS: macOS `say` voice name (e.g. "Samantha", "Lekha"). Empty = system default.
    "tts_voice": "",
    # optional: Tavily API key for live web search on queries that need current info.
    # blank = web search disabled, models just answer from their own knowledge.
    "tavily_api_key": "",
    # predictive typing: rerank the LLM's first suggested word using a local
    # word-frequency model learned from the user's own writing, when confident.
    "predictive_personalization_enabled": True,
    # Google integration: the user supplies their own OAuth client (Desktop app
    # type) from their own Google Cloud project. Mira ships no shared secret --
    # see google_integration.py for why.
    "google_client_id": "",
    "google_client_secret": "",
    # Dump Box: which Reminders list action items get pushed into. Blank = the
    # system default list.
    "reminders_list": "",
    # optional convenience: base URL of the user's n8n instance, used only to
    # prefill webhook URLs in the Automations UI.
    "n8n_base_url": "",
    # window appearance: "system" follows macOS, "light"/"dark" pin it.
    "appearance": "system"
}

def load_settings():
    settings = dict(DEFAULT_SETTINGS)
    if SETTINGS_PATH.exists():
        try:
            settings.update(json.loads(SETTINGS_PATH.read_text()))
        except (json.JSONDecodeError, OSError):
            pass
    return settings

def save_settings(settings: dict):
    SETTINGS_PATH.write_text(json.dumps(settings, indent=2))

@app.get("/settings")
def get_settings():
    settings = load_settings()
    # never echo the raw API key back to the frontend; expose only whether one is set
    masked = dict(settings)
    masked["cloud_api_key_set"] = bool(settings.get("cloud_api_key"))
    masked["tavily_api_key_set"] = bool(settings.get("tavily_api_key"))
    masked["google_client_secret_set"] = bool(settings.get("google_client_secret"))
    masked.pop("cloud_api_key", None)
    masked.pop("tavily_api_key", None)
    masked.pop("google_client_secret", None)
    return masked

@app.post("/settings")
def update_settings(
    voice_response_enabled: bool = Body(None),
    wakeword_enabled: bool = Body(None),
    local_chat_model: str = Body(None),
    cloud_provider: str = Body(None),
    cloud_api_key: str = Body(None),
    cloud_base_url: str = Body(None),
    cloud_model: str = Body(None),
    stt_prefer: str = Body(None),
    transcription_mode: str = Body(None),
    tts_voice: str = Body(None),
    tavily_api_key: str = Body(None),
    predictive_personalization_enabled: bool = Body(None),
    google_client_id: str = Body(None),
    google_client_secret: str = Body(None),
    reminders_list: str = Body(None),
    n8n_base_url: str = Body(None),
    appearance: str = Body(None),
):
    settings = load_settings()

    if voice_response_enabled is not None:
        settings["voice_response_enabled"] = voice_response_enabled

    if wakeword_enabled is not None:
        settings["wakeword_enabled"] = wakeword_enabled
        try:
            from wakeword_listener import set_wakeword_enabled
            set_wakeword_enabled(wakeword_enabled)
        except Exception as e:
            print(f"[main] Could not toggle wakeword listener: {e}", flush=True)

    if local_chat_model is not None:
        settings["local_chat_model"] = local_chat_model
    if cloud_provider is not None:
        settings["cloud_provider"] = cloud_provider
    if cloud_api_key is not None:
        settings["cloud_api_key"] = cloud_api_key
    if cloud_base_url is not None:
        settings["cloud_base_url"] = cloud_base_url
    if cloud_model is not None:
        settings["cloud_model"] = cloud_model
    if stt_prefer is not None:
        settings["stt_prefer"] = stt_prefer
    if transcription_mode is not None:
        settings["transcription_mode"] = transcription_mode
    if tts_voice is not None:
        settings["tts_voice"] = tts_voice
    if tavily_api_key is not None:
        settings["tavily_api_key"] = tavily_api_key
    if predictive_personalization_enabled is not None:
        settings["predictive_personalization_enabled"] = predictive_personalization_enabled
    if google_client_id is not None:
        settings["google_client_id"] = google_client_id
    if google_client_secret is not None:
        settings["google_client_secret"] = google_client_secret
    if reminders_list is not None:
        settings["reminders_list"] = reminders_list
    if n8n_base_url is not None:
        settings["n8n_base_url"] = n8n_base_url
    if appearance is not None:
        settings["appearance"] = appearance

    save_settings(settings)
    result = dict(settings)
    result["cloud_api_key_set"] = bool(settings.get("cloud_api_key"))
    result["tavily_api_key_set"] = bool(settings.get("tavily_api_key"))
    result["google_client_secret_set"] = bool(settings.get("google_client_secret"))
    result.pop("cloud_api_key", None)
    result.pop("tavily_api_key", None)
    result.pop("google_client_secret", None)
    return result


# ---------- Wake-word listener ("Hey Mira") ----------
# Runs as a background thread inside this same daemon process -- reuses the same
# already-authorized microphone access (this process is code-signed with the
# com.apple.security.device.audio-input entitlement), avoiding a second permission
# fight for a separate LaunchAgent. Started via the `lifespan` handler above.

# ---------- Conversation history storage ----------
HISTORY_DIR = Path.home() / "Mira" / "daemon" / "chat_history"
HISTORY_DIR.mkdir(parents=True, exist_ok=True)

def load_history(session_id: str):
    f = HISTORY_DIR / f"{session_id}.json"
    if f.exists():
        return json.loads(f.read_text())
    return []

def save_history(session_id: str, messages: list):
    f = HISTORY_DIR / f"{session_id}.json"
    f.write_text(json.dumps(messages, ensure_ascii=False, indent=2))

# ---------- TTS audio output storage ----------
TTS_DIR = Path.home() / "Mira" / "daemon" / "tts_output"
TTS_DIR.mkdir(parents=True, exist_ok=True)

# ---------- Meeting audio storage ----------
MEETINGS_DIR = Path.home() / "Mira" / "daemon" / "meetings"
MEETINGS_DIR.mkdir(parents=True, exist_ok=True)

# ---------- whisper.cpp paths (EDIT THESE if your paths differ) ----------
WHISPER_CLI = Path.home() / "Mira" / "whisper.cpp" / "build" / "bin" / "whisper-cli"
WHISPER_MODEL = Path.home() / "Mira" / "whisper.cpp" / "models" / "ggml-small.bin"
WHISPER_VAD_MODEL = Path.home() / "Mira" / "whisper.cpp" / "models" / "ggml-silero-v6.2.0.bin"

# ffmpeg's full path -- LaunchAgent daemons run with a minimal PATH that does NOT
# include Homebrew's /opt/homebrew/bin, so "ffmpeg" alone is not found. Confirm this
# matches `which ffmpeg` on your machine (Intel Macs often use /usr/local/bin/ffmpeg instead).
FFMPEG_PATH = "/opt/homebrew/bin/ffmpeg"
FFPROBE_PATH = "/opt/homebrew/bin/ffprobe"

# ---------- mic_helper (Swift, run by the daemon) ----------
# mic_helper runs from inside a proper .app bundle (with Info.plist declaring
# NSMicrophoneUsageDescription) and the daemon's Python interpreter is code-signed
# with the com.apple.security.device.audio-input entitlement -- this combination is
# what makes daemon-spawned mic recording work without any runtime prompt.
MIC_HELPER_PATH = Path.home() / "Mira" / "electron" / "MicHelper.app" / "Contents" / "MacOS" / "mic_helper"
MIC_CONTROL_FILE = Path("/tmp/mira_mic_control")

# NOTE: system audio (ScreenCaptureKit) is NOT spawned by the daemon anymore.
# Screen Recording permission cannot be reliably granted to a headless LaunchAgent --
# macOS re-prompts every time and the grant never persists for a background process.
# Electron (a real foreground GUI app) spawns system_audio_helper directly instead
# (see meeting_recorder.js) and writes to the same _system.wav path the daemon
# expects, so /meeting/stop below just waits for that file to exist before merging.

# tracks the currently running mic recording process, if any
active_mic_process = None
active_meeting_base = None


@app.get("/health")
def health_check():
    return {"status": "Mira daemon is running"}


# ---------- manual "Hey Mira" trigger (keyboard shortcut / button, skips the wake word) ----------
@app.post("/wakeword/trigger")
def wakeword_trigger():
    try:
        from wakeword_listener import trigger_manual_command
        ok = trigger_manual_command()
        if not ok:
            return {"error": "wake-word listener is not initialized"}
        return {"status": "listening"}
    except Exception as e:
        return {"error": str(e)}


@app.get("/ask")
def ask_mira(prompt: str):
    response = requests.post(
        "http://localhost:11434/api/generate",
        json={
            "model": "qwen3:1.7b",
            "prompt": prompt,
            "stream": False
        }
    )
    result = response.json()
    return {"response": result["response"]}


def _normalize_word(w: str) -> str:
    return re.sub(r"[^a-z0-9]", "", w.lower())


def clean_completion(completion: str, context: str) -> str:
    """Mirrors predictive_typing.js's client-side cleanup (first line only, strip
    leading ellipsis/punctuation and markdown emphasis markers, strip an echoed
    prefix of the context) so the server-side personalization swap below operates
    on the actual first suggested word, not raw/noisy model output.

    The echo check compares words after stripping punctuation/apostrophes rather
    than a raw substring match -- the model frequently echoes context back with
    different apostrophe/casing (e.g. "lets" typed -> "let's" echoed), which broke
    a naive string-prefix comparison."""
    cleaned = completion.split("\n")[0].strip()
    cleaned = re.sub(r"^[.…\s]+", "", cleaned)
    cleaned = re.sub(r"\*\*|\*", "", cleaned)

    if context:
        context_words = context.strip().split()
        completion_words = cleaned.split()
        max_check = min(6, len(context_words), len(completion_words))
        strip_n = 0
        for n in range(max_check, 0, -1):
            ctx_tail = [_normalize_word(w) for w in context_words[-n:]]
            comp_head = [_normalize_word(w) for w in completion_words[:n]]
            if all(ctx_tail) and ctx_tail == comp_head:
                strip_n = n
                break
        if strip_n:
            cleaned = " ".join(completion_words[strip_n:])

    return cleaned


@app.get("/complete")
def complete_mira(prompt: str, context: str = ""):
    response = requests.post(
        "http://localhost:11434/api/generate",
        json={
            "model": "batiai/gemma4-e2b:q4",
            "prompt": prompt,
            "stream": False,
            "think": False,
            "options": {
                # client only ever shows the first 4 words -- generating more than
                # ~8 tokens is pure wasted latency. Lower temperature + higher
                # repeat_penalty were tuned together with the shorter prompt to
                # cut the "now"/"today" filler-word tic the model otherwise has.
                "num_predict": 8,
                "temperature": 0.1,
                "repeat_penalty": 1.5
            }
        }
    )
    result = response.json()
    completion = clean_completion(result["response"], context)

    # personalization: if the user's own writing history strongly prefers a
    # different first word in this exact context, swap it in. The LLM still
    # generates the rest of the phrase -- this only corrects the first word.
    if context and load_settings().get("predictive_personalization_enabled", True):
        try:
            from personalization import predict_next_word
            personal_word, confidence = predict_next_word(context)
            if personal_word:
                words = completion.strip().split()
                if words and words[0].lower() != personal_word.lower():
                    words[0] = personal_word
                    completion = " ".join(words)
        except Exception as e:
            print(f"[main] personalization lookup failed: {e}", flush=True)

    return {"response": completion}


@app.post("/complete/feedback")
def complete_feedback(context: str = Body(...), word: str = Body(...)):
    """Called when the user actually Tab-accepts a suggested word -- reinforces
    the personalization model with a real, directly-relevant training signal."""
    try:
        from personalization import record_accepted_word
        record_accepted_word(context, word)
        return {"status": "recorded"}
    except Exception as e:
        return {"error": str(e)}


# ---------- In-word (letter-level) completion -- purely local, no LLM ----------
@app.get("/complete/word")
def complete_word_endpoint(partial: str):
    try:
        from personalization import complete_word
        suffix = complete_word(partial)
        return {"suffix": suffix}
    except Exception as e:
        return {"error": str(e)}


@app.post("/complete/word/feedback")
def complete_word_feedback(word: str = Body(..., embed=True)):
    """Called when an in-word completion is Tab-accepted."""
    try:
        from personalization import record_accepted_completion
        record_accepted_completion(word)
        return {"status": "recorded"}
    except Exception as e:
        return {"error": str(e)}


# ---------- Spell-check -- proxies to ax_helper's native NSSpellChecker call ----------
AX_HELPER_PATH = Path.home() / "Mira" / "electron" / "AXHelper.app" / "Contents" / "MacOS" / "ax_helper"

@app.get("/spellcheck")
def spellcheck_endpoint(word: str):
    try:
        result = subprocess.run([str(AX_HELPER_PATH), "spellcheck", word], capture_output=True, text=True, timeout=5)
        return json.loads(result.stdout.strip())
    except (subprocess.SubprocessError, json.JSONDecodeError) as e:
        return {"error": str(e)}


# ---------- Model routing helpers ----------

CLOUD_KEYWORDS = [
    "search", "latest", "news", "today", "current", "currently",
    "right now", "this week", "recent", "update", "score", "weather",
    "stock", "price", "who is the", "what is the current", "look up"
]

def needs_cloud(message: str) -> bool:
    text = message.lower()
    return any(kw in text for kw in CLOUD_KEYWORDS)

def has_internet() -> bool:
    try:
        requests.get("https://www.google.com", timeout=2)
        return True
    except requests.RequestException:
        return False


def web_search(query: str, max_results: int = 4):
    """Live web search via Tavily (https://tavily.com), used to ground answers about
    current events/info the model's own training data can't know. Optional -- returns
    an error if no key is configured, callers should treat that as "skip search"."""
    settings = load_settings()
    api_key = settings.get("tavily_api_key")
    if not api_key:
        return None, {"error": "no Tavily API key configured"}

    try:
        response = requests.post(
            "https://api.tavily.com/search",
            json={"api_key": api_key, "query": query, "max_results": max_results, "search_depth": "basic"},
            timeout=15
        )
        data = response.json()
        results = data.get("results", [])
        if not results:
            return None, {"error": "no results", "details": data}

        formatted = "\n\n".join(
            f"- {r.get('title', '')}: {r.get('content', '')} (source: {r.get('url', '')})"
            for r in results
        )
        return formatted, None
    except requests.RequestException as e:
        return None, {"error": str(e)}


def build_context_message(message: str) -> str:
    """Built fresh per-request and prepended as a system message to whatever gets
    sent to the model -- never persisted into saved chat history, so it doesn't
    pollute the conversation shown to the user."""
    now = datetime.datetime.now()
    parts = [f"Current date and time: {now.strftime('%A, %B %d, %Y, %I:%M %p')} (local system time)."]

    if needs_cloud(message) and has_internet():
        results, _err = web_search(message)
        if results:
            parts.append(
                "Live web search results for reference -- use them if relevant to answer "
                "accurately, and cite naturally rather than dumping the raw list:\n" + results
            )

    return "\n\n".join(parts)


def call_local_model(history: list) -> str:
    settings = load_settings()
    response = requests.post(
        "http://localhost:11434/api/chat",
        json={
            "model": settings.get("local_chat_model") or DEFAULT_SETTINGS["local_chat_model"],
            "messages": history,
            "stream": False,
            "think": False
        }
    )
    result = response.json()
    return result["message"]["content"]

def call_cloud_model(history: list):
    settings = load_settings()
    # a custom key in settings always wins; otherwise fall back to the bundled
    # Groq preset using GROQ_API_KEY from .env, so this keeps working with zero config
    api_key = settings.get("cloud_api_key") or GROQ_API_KEY
    base_url = settings.get("cloud_base_url") or DEFAULT_SETTINGS["cloud_base_url"]
    model = settings.get("cloud_model") or DEFAULT_SETTINGS["cloud_model"]

    if not api_key:
        return None, {"error": "no cloud API key configured"}

    response = requests.post(
        f"{base_url.rstrip('/')}/chat/completions",
        headers={"Authorization": f"Bearer {api_key}"},
        json={
            "model": model,
            "messages": history
        }
    )
    result = response.json()
    if "choices" not in result:
        return None, result
    return result["choices"][0]["message"]["content"], None


# Firing a webhook has real side effects, so an automation only runs when the
# user said so explicitly. Both gates must pass: the message has to open with a
# run verb, AND automations.match_automation has to find one unambiguous match.
# Anything less falls through to a normal reply rather than guessing.
RUN_VERBS = ("run", "trigger", "start", "execute", "fire", "launch", "kick off")


def maybe_run_automation(message: str):
    text = (message or "").strip().lower()
    if not any(text.startswith(v) or f" {v} " in f" {text} " for v in RUN_VERBS):
        return None

    import automations as _a
    match = _a.match_automation(message)
    if not match:
        return None

    result = _a.run_automation(match["id"])
    if result.get("success"):
        return f"Ran \"{match['name']}\"."
    return f"Couldn't run \"{match['name']}\": {result.get('error') or 'HTTP ' + str(result.get('status_code'))}"


# ---------- Groq/local routed chatbot endpoint ----------
@app.post("/chat")
def chat(session_id: str = Body(...), message: str = Body(...), model: str = Body("auto")):
    automation_reply = maybe_run_automation(message)
    if automation_reply:
        history = load_history(session_id)
        history.append({"role": "user", "content": message})
        history.append({"role": "assistant", "content": automation_reply})
        save_history(session_id, history)
        return {"response": automation_reply, "model_used": "automation"}

    history = load_history(session_id)
    history.append({"role": "user", "content": message})

    # current date/time (+ live search results, if configured) go in as a system
    # message for the model only -- never saved into the persisted conversation
    context_message = build_context_message(message)
    messages_for_model = [{"role": "system", "content": context_message}] + history

    used_model = "local"
    reply = None

    if model == "local":
        reply = call_local_model(messages_for_model)
        used_model = "local"

    elif model == "cloud":
        reply, error = call_cloud_model(messages_for_model)
        if reply is None:
            return {"error": error}
        used_model = "cloud"

    else:  # auto
        if needs_cloud(message) and has_internet():
            reply, error = call_cloud_model(messages_for_model)
            if reply is not None:
                used_model = "cloud"
            else:
                reply = call_local_model(messages_for_model)
                used_model = "local"
        else:
            reply = call_local_model(messages_for_model)
            used_model = "local"

    history.append({"role": "assistant", "content": reply})
    save_history(session_id, history)

    return {"response": reply, "model_used": used_model}


# ---------- NEW: long-file transcription with local fallback ----------
def transcribe_with_groq(filepath: Path, mode: str = "translate"):
    # "translate" -> always English output; "native" -> transcribed in the spoken language
    endpoint = "translations" if mode == "translate" else "transcriptions"
    try:
        with open(filepath, "rb") as f:
            response = requests.post(
                f"https://api.groq.com/openai/v1/audio/{endpoint}",
                headers={"Authorization": f"Bearer {GROQ_API_KEY}"},
                files={"file": (filepath.name, f, "application/octet-stream")},
                data={"model": "whisper-large-v3"},
                timeout=120
            )
        result = response.json()
        if "text" in result:
            return result["text"], None
        return None, result
    except requests.RequestException as e:
        return None, {"error": str(e)}

def transcribe_with_whisper_cpp(filepath: Path, mode: str = "translate"):
    # whisper.cpp requires 16kHz mono WAV input; convert with ffmpeg first
    converted_path = filepath.with_suffix(".converted.wav")
    convert_result = subprocess.run(
        [FFMPEG_PATH, "-y", "-i", str(filepath), "-ar", "16000", "-ac", "1", str(converted_path)],
        capture_output=True, text=True
    )
    if convert_result.returncode != 0:
        return None, {"error": "ffmpeg conversion failed", "details": convert_result.stderr}

    cmd = [str(WHISPER_CLI), "-m", str(WHISPER_MODEL), "-f", str(converted_path), "-l", "auto"]
    if mode == "translate":
        cmd.append("-tr")
    # VAD splits audio at real speech/silence boundaries instead of decoding one
    # long continuous stretch -- meaningfully reduces (though doesn't eliminate on
    # its own, for this small model) the runaway repetition-loop hallucination
    # small Whisper models are prone to on long/noisy/non-English audio.
    if WHISPER_VAD_MODEL.exists():
        cmd += ["--vad", "-vm", str(WHISPER_VAD_MODEL)]
    cmd += ["-nt", "-otxt"]

    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        return None, {"error": "whisper.cpp failed", "details": result.stderr}

    txt_output = converted_path.with_suffix(".wav.txt")
    if txt_output.exists():
        return txt_output.read_text().strip(), None

    # some whisper.cpp versions print directly to stdout instead of a .txt file
    if result.stdout.strip():
        return result.stdout.strip(), None

    return None, {"error": "no transcription output found"}


def transcribe_smart(filepath: Path):
    """
    Tries the engine set in Settings ("stt_prefer") first, falling back to the
    other one on failure (cloud fallback also requires internet). Local whisper.cpp
    is the default preference: it's proven to translate non-English speech (e.g.
    Hindi) correctly, whereas Groq's cloud /translations endpoint has been observed
    to only transliterate (romanize) it instead of actually translating.
    Returns (text, engine_used, error_info).
    """
    settings = load_settings()
    prefer = settings.get("stt_prefer", "local")
    mode = settings.get("transcription_mode", "translate")

    def try_local():
        return transcribe_with_whisper_cpp(filepath, mode)

    def try_cloud():
        if not has_internet():
            return None, {"error": "no internet connection"}
        return transcribe_with_groq(filepath, mode)

    order = [("whisper.cpp", try_local), ("groq", try_cloud)]
    if prefer == "cloud":
        order = list(reversed(order))

    errors = {}
    for engine_name, fn in order:
        text, err = fn()
        if text is not None:
            return text, engine_name, None
        errors[f"{engine_name}_error"] = err

    return None, None, errors


def collapse_repetition_loops(text: str) -> str:
    """Small Whisper models can fall into a runaway loop of repeating the same
    short phrase over and over on long/noisy/non-English audio (a well-known
    failure mode, confirmed on real meeting recordings here). This is a
    defensive safety net applied to ANY engine's output: collapses a run of
    3+ consecutive duplicate lines down to a single copy plus a note, rather
    than shipping pages of "I don't know. I don't know. I don't know...."."""
    lines = text.split("\n")
    out = []
    i = 0
    while i < len(lines):
        line = lines[i]
        run = 1
        while i + run < len(lines) and lines[i + run].strip() == line.strip():
            run += 1
        if run >= 3 and line.strip():
            out.append(line)
            out.append(f"[repeated {run}x, collapsed]")
        else:
            out.extend(lines[i:i + run])
        i += run
    return "\n".join(out)


def get_audio_duration_seconds(filepath: Path):
    try:
        result = subprocess.run(
            [FFPROBE_PATH, "-v", "error", "-show_entries", "format=duration", "-of", "default=noprint_wrappers=1:nokey=1", str(filepath)],
            capture_output=True, text=True, timeout=30
        )
        return float(result.stdout.strip())
    except (subprocess.SubprocessError, ValueError):
        return None


MEETING_CHUNK_SECONDS = 600  # 10 min per chunk -- safely under Groq's per-file upload limit


def transcribe_meeting_chunked_groq(filepath: Path, duration: float):
    """Splits long audio into ~10-minute chunks and transcribes each via Groq's
    cloud whisper-large-v3 in NATIVE mode (not translate). This is meaningfully
    more accurate than the local model on long, noisy, real-world (often
    multilingual) meeting audio -- confirmed directly: the local base model
    produced runaway hallucination loops on a real Hindi meeting recording
    where Groq's cloud model transcribed it correctly. Always native mode at
    the audio level -- Groq's /translations endpoint was separately found to
    only transliterate Hindi rather than actually translate it; English output
    (if wanted) is handled afterward as a proper text-to-text LLM translation
    pass in transcribe_meeting_smart, not baked into the audio model."""
    chunk_paths = []
    try:
        num_chunks = max(1, int(duration // MEETING_CHUNK_SECONDS) + (1 if duration % MEETING_CHUNK_SECONDS > 5 else 0))
        texts = []
        for i in range(num_chunks):
            start = i * MEETING_CHUNK_SECONDS
            chunk_path = filepath.with_suffix(f".chunk{i}.wav")
            convert_result = subprocess.run(
                [FFMPEG_PATH, "-y", "-ss", str(start), "-t", str(MEETING_CHUNK_SECONDS),
                 "-i", str(filepath), "-ar", "16000", "-ac", "1", str(chunk_path)],
                capture_output=True, text=True
            )
            if convert_result.returncode != 0 or not chunk_path.exists():
                continue
            chunk_paths.append(chunk_path)

            text, err = transcribe_with_groq(chunk_path, mode="native")
            if text is None:
                return None, {"error": f"chunk {i} failed", "details": err}
            texts.append(text.strip())

        return "\n".join(t for t in texts if t), None
    finally:
        for p in chunk_paths:
            p.unlink(missing_ok=True)


def translate_text_via_llm(text: str):
    """Text-to-text translation via the chat LLM, used for meeting transcripts
    instead of relying on an audio model's built-in translate mode -- Groq's
    /translations endpoint and the local model's -tr flag were both found to
    handle Hindi poorly (transliteration / hallucination respectively), whereas
    a general-purpose LLM translating text it can already read in full context
    does this reliably. Chunks long transcripts to stay within reasonable
    prompt sizes for the local/small cloud models."""
    CHUNK_CHARS = 3000
    chunks = [text[i:i + CHUNK_CHARS] for i in range(0, len(text), CHUNK_CHARS)] or [text]

    translated_parts = []
    for chunk in chunks:
        prompt = f"Translate the following meeting transcript to English. Keep it as plain transcript text, no commentary, no preamble:\n\n{chunk}"
        history = [{"role": "user", "content": prompt}]
        if has_internet():
            reply, error = call_cloud_model(history)
            if reply is None:
                reply = call_local_model(history)
        else:
            reply = call_local_model(history)
        translated_parts.append(reply)

    return "\n".join(translated_parts)


def transcribe_meeting_smart(filepath: Path):
    """Meeting-specific transcription pipeline (used by /transcribe-file and
    /transcribe-local-meeting, NOT the short-clip /transcribe endpoint, which
    already works well via transcribe_smart). Always transcribes at the audio
    level in the spoken language (native mode) -- proven more reliable than
    either engine's translate mode -- chunks through Groq's cloud model for
    long recordings when online (meaningfully more accurate on real meeting
    audio than the local model), falls back to local whisper.cpp+VAD, and
    applies a repetition-loop safety net plus an optional LLM-based text
    translation pass afterward if the user wants English output.
    Returns (text, engine_used, error_info)."""
    settings = load_settings()
    want_english = settings.get("transcription_mode", "translate") == "translate"

    duration = get_audio_duration_seconds(filepath)
    text, engine_used, error_info = None, None, None

    if duration and duration > MEETING_CHUNK_SECONDS and has_internet():
        text, err = transcribe_meeting_chunked_groq(filepath, duration)
        if text is not None:
            engine_used = "groq (chunked)"
        else:
            error_info = {"groq_chunked_error": err}

    if text is None:
        # short recording, offline, or the chunked cloud pass failed -- fall
        # back to the existing single-shot pipeline, forced to native mode
        fallback_text, fallback_engine, fallback_err = None, None, None
        prefer = settings.get("stt_prefer", "local")

        def try_local():
            return transcribe_with_whisper_cpp(filepath, "native")

        def try_cloud():
            if not has_internet():
                return None, {"error": "no internet connection"}
            return transcribe_with_groq(filepath, "native")

        order = [("whisper.cpp", try_local), ("groq", try_cloud)]
        if prefer == "cloud":
            order = list(reversed(order))

        errors = dict(error_info) if error_info else {}
        for engine_name, fn in order:
            fallback_text, fallback_err = fn()
            if fallback_text is not None:
                fallback_engine = engine_name
                break
            errors[f"{engine_name}_error"] = fallback_err

        if fallback_text is None:
            return None, None, errors

        text, engine_used = fallback_text, fallback_engine

    text = collapse_repetition_loops(text)

    if want_english:
        text = translate_text_via_llm(text)
        engine_used += " + LLM translation"

    return text, engine_used, None


# ---------- Short-clip transcription (used by chat mic / push-to-talk) ----------
@app.post("/transcribe")
async def transcribe(audio: UploadFile = File(...)):
    temp_path = MEETINGS_DIR / f"clip_{uuid.uuid4().hex}.webm"
    audio_bytes = await audio.read()
    temp_path.write_bytes(audio_bytes)

    text, engine_used, error_info = transcribe_smart(temp_path)

    for f in [temp_path, temp_path.with_suffix(".converted.wav"), temp_path.with_suffix(".converted.wav.txt")]:
        f.unlink(missing_ok=True)

    if text is None:
        return {"error": error_info}

    return {"text": text, "engine_used": engine_used}


@app.post("/transcribe-file")
async def transcribe_file(audio: UploadFile = File(...)):
    """
    Accepts any audio/video file (meeting recordings, voice memos, etc).
    """
    temp_path = MEETINGS_DIR / f"upload_{uuid.uuid4().hex}_{audio.filename}"
    audio_bytes = await audio.read()
    temp_path.write_bytes(audio_bytes)

    text, engine_used, error_info = transcribe_meeting_smart(temp_path)

    if text is None:
        return {"error": error_info}

    return {"text": text, "engine_used": engine_used}


@app.post("/transcribe-local-meeting")
def transcribe_local_meeting(filepath: str = Body(..., embed=True)):
    """
    Transcribes a file that already exists on disk (used for live meeting recordings
    saved by audio_helper, avoiding a redundant re-upload of a potentially large file).
    """
    path = Path(filepath)
    if not path.exists():
        return {"error": f"file not found: {filepath}"}

    text, engine_used, error_info = transcribe_meeting_smart(path)
    if text is None:
        return {"error": error_info}

    return {"text": text, "engine_used": engine_used}


# ---------- NEW: Meeting minutes generation ----------
MINUTES_PROMPT_TEMPLATE = """You are an assistant that writes clear meeting minutes from a raw transcript.

Given the transcript below, produce:
1. A short summary (2-4 sentences)
2. Key decisions made (bullet points, or "None noted" if none)
3. Action items with owner if mentioned (bullet points, or "None noted" if none)
4. Any important dates or deadlines mentioned (bullet points, or "None noted" if none)

Transcript:
\"\"\"
{transcript}
\"\"\"

Respond only with the structured minutes, no preamble."""

@app.post("/generate-minutes")
def generate_minutes(transcript: str = Body(...), model: str = Body("auto")):
    prompt = MINUTES_PROMPT_TEMPLATE.format(transcript=transcript)
    history = [{"role": "user", "content": prompt}]

    used_model = "local"
    reply = None

    if model == "local":
        reply = call_local_model(history)
        used_model = "local"
    elif model == "cloud":
        reply, error = call_cloud_model(history)
        if reply is None:
            return {"error": error}
        used_model = "cloud"
    else:  # auto - minutes generation benefits from the stronger cloud model when available
        if has_internet():
            reply, error = call_cloud_model(history)
            if reply is not None:
                used_model = "cloud"
            else:
                reply = call_local_model(history)
                used_model = "local"
        else:
            reply = call_local_model(history)
            used_model = "local"

    return {"minutes": reply, "model_used": used_model}


# ---------- Live meeting recording control ----------
# Mic recording is managed here (daemon). System audio recording is managed by
# Electron directly (see meeting_recorder.js) -- Electron tells us the base_path
# it will use, we start mic recording at that same base_path, and on stop we wait
# briefly for Electron's system-audio file to finish appearing before merging.
@app.post("/meeting/start")
def meeting_start():
    global active_mic_process, active_meeting_base

    if active_mic_process is not None and active_mic_process.poll() is None:
        return {"error": "a meeting recording is already in progress"}

    base_name = f"meeting_{int(time.time())}"
    base_path = MEETINGS_DIR / base_name
    mic_path = Path(str(base_path) + "_mic.wav")

    if MIC_CONTROL_FILE.exists():
        MIC_CONTROL_FILE.unlink()

    mic_log = open(MEETINGS_DIR / f"{base_name}_mic_helper.log", "w")

    active_mic_process = subprocess.Popen(
        [str(MIC_HELPER_PATH), "start", str(mic_path)],
        stdout=mic_log, stderr=subprocess.STDOUT, text=True
    )
    active_meeting_base = base_path

    # base_path is returned so Electron can derive the exact same _system.wav path
    # and spawn system_audio_helper against it
    return {"status": "recording started", "base_path": str(base_path)}

@app.post("/meeting/stop")
def meeting_stop():
    global active_mic_process, active_meeting_base

    if active_mic_process is None:
        return {"error": "no meeting recording in progress"}

    subprocess.run([str(MIC_HELPER_PATH), "stop"])

    try:
        active_mic_process.wait(timeout=10)
    except subprocess.TimeoutExpired:
        active_mic_process.kill()

    base_path = active_meeting_base
    active_mic_process = None
    active_meeting_base = None

    if base_path is None:
        return {"error": "no recording file tracked"}

    mic_file = Path(str(base_path) + "_mic.wav")
    system_file = Path(str(base_path) + "_system.wav")
    merged_file = Path(str(base_path) + "_merged.wav")

    # Electron stops system_audio_helper itself and may still be finalizing that
    # file's WAV header when this endpoint is called -- poll briefly instead of
    # failing immediately if it's not there yet.
    for _ in range(20):  # up to ~5 seconds
        if system_file.exists():
            break
        time.sleep(0.25)

    if not mic_file.exists() or not system_file.exists():
        return {"error": "recording finished but one or both audio files were not found",
                "mic_exists": mic_file.exists(), "system_exists": system_file.exists()}

    merge_result = subprocess.run(
        [
            FFMPEG_PATH, "-y",
            "-i", str(mic_file),
            "-i", str(system_file),
            "-filter_complex", "[0:a][1:a]amix=inputs=2:duration=longest:dropout_transition=0",
            str(merged_file)
        ],
        capture_output=True, text=True
    )

    if merge_result.returncode != 0:
        return {"error": "ffmpeg merge failed", "details": merge_result.stderr,
                "mic_file": str(mic_file), "system_file": str(system_file)}

    return {"status": "recording stopped", "file": str(merged_file),
             "mic_file": str(mic_file), "system_file": str(system_file)}


# ---------- macOS `say`-based text-to-speech endpoint ----------
@app.post("/speak")
def speak(text: str = Body(..., embed=True), voice: str = Body(None)):
    settings = load_settings()
    chosen_voice = voice or settings.get("tts_voice")

    filename = f"{uuid.uuid4().hex}.aiff"
    filepath = TTS_DIR / filename

    cmd = ["say", "-o", str(filepath)]
    if chosen_voice:
        cmd += ["-v", chosen_voice]
    cmd.append(text)

    result = subprocess.run(cmd, capture_output=True, text=True)

    if result.returncode != 0:
        return {"error": result.stderr}

    return FileResponse(path=str(filepath), media_type="audio/aiff", filename=filename)


# ---------- available macOS TTS voices ----------
@app.get("/voices")
def list_voices():
    result = subprocess.run(["say", "-v", "?"], capture_output=True, text=True)
    if result.returncode != 0:
        return {"error": result.stderr}

    voices = []
    for line in result.stdout.splitlines():
        # format: "Name                locale    # sample text"
        if "#" not in line:
            continue
        left, _, _sample = line.partition("#")
        parts = left.split()
        if len(parts) < 2:
            continue
        locale = parts[-1]
        name = " ".join(parts[:-1]).strip()
        voices.append({"name": name, "locale": locale})

    return {"voices": voices}


# ---------- Ollama local model management ----------
OLLAMA_URL = "http://localhost:11434"

@app.get("/models/local")
def list_local_models():
    try:
        response = requests.get(f"{OLLAMA_URL}/api/tags", timeout=5)
        data = response.json()
        return {"models": data.get("models", [])}
    except requests.RequestException as e:
        return {"error": f"could not reach Ollama: {e}"}


@app.post("/models/local/pull")
def pull_local_model(name: str = Body(..., embed=True)):
    def stream():
        try:
            with requests.post(f"{OLLAMA_URL}/api/pull", json={"name": name}, stream=True, timeout=None) as r:
                for line in r.iter_lines():
                    if line:
                        yield line + b"\n"
        except requests.RequestException as e:
            yield (json.dumps({"error": str(e)}) + "\n").encode()

    return StreamingResponse(stream(), media_type="application/x-ndjson")


@app.delete("/models/local")
def delete_local_model(name: str = Body(..., embed=True)):
    try:
        response = requests.delete(f"{OLLAMA_URL}/api/delete", json={"name": name}, timeout=10)
        if response.status_code == 200:
            return {"status": "deleted", "name": name}
        return {"error": response.text}
    except requests.RequestException as e:
        return {"error": f"could not reach Ollama: {e}"}


# ---------- test a cloud LLM connection before saving it ----------
@app.post("/settings/test-cloud")
def test_cloud_connection(base_url: str = Body(...), api_key: str = Body(""), model: str = Body(...)):
    # an empty api_key means "use whatever's already saved" (or the .env fallback),
    # so testing works without re-typing a key that's already stored
    if not api_key:
        settings = load_settings()
        api_key = settings.get("cloud_api_key") or GROQ_API_KEY
        if not api_key:
            return {"ok": False, "error": "no API key saved and none provided"}

    try:
        response = requests.post(
            f"{base_url.rstrip('/')}/chat/completions",
            headers={"Authorization": f"Bearer {api_key}"},
            json={"model": model, "messages": [{"role": "user", "content": "Say OK"}], "max_tokens": 50},
            timeout=15
        )
        result = response.json()
        if "choices" in result:
            return {"ok": True, "reply": result["choices"][0]["message"]["content"]}
        return {"ok": False, "error": result}
    except requests.RequestException as e:
        return {"ok": False, "error": str(e)}


@app.post("/settings/test-search")
def test_search_connection(api_key: str = Body("")):
    # an empty api_key means "use whatever's already saved"
    if not api_key:
        api_key = load_settings().get("tavily_api_key")
        if not api_key:
            return {"ok": False, "error": "no API key saved and none provided"}

    try:
        response = requests.post(
            "https://api.tavily.com/search",
            json={"api_key": api_key, "query": "current weather in San Francisco", "max_results": 1},
            timeout=15
        )
        data = response.json()
        if data.get("results"):
            return {"ok": True, "sample": data["results"][0].get("title", "")}
        return {"ok": False, "error": data}
    except requests.RequestException as e:
        return {"ok": False, "error": str(e)}


# ---------- Shared: one-shot prompt through the normal model routing ----------
def route_prompt(prompt: str, model: str = "auto") -> tuple:
    """Send a single prompt through the same local/cloud routing as /chat.

    Returns (reply, used_model). Raises RuntimeError if no model could answer.
    """
    history = [{"role": "user", "content": prompt}]

    if model == "local":
        return call_local_model(history), "local"

    if model == "cloud":
        reply, error = call_cloud_model(history)
        if reply is None:
            raise RuntimeError(f"cloud model unavailable: {error}")
        return reply, "cloud"

    # auto: structured-output tasks are noticeably more reliable on the cloud
    # model, so prefer it when there's a connection, but never hard-fail offline
    if has_internet():
        reply, _error = call_cloud_model(history)
        if reply is not None:
            return reply, "cloud"
    return call_local_model(history), "local"


# ---------- Dump Box ----------
import dumpbox as _dumpbox


@app.get("/dumpbox")
def dumpbox_list():
    return {"entries": _dumpbox.list_entries()}


@app.post("/dumpbox")
def dumpbox_add(text: str = Body(..., embed=True)):
    try:
        return _dumpbox.add_entry(text)
    except ValueError as e:
        return {"error": str(e)}


@app.get("/dumpbox/{entry_id}")
def dumpbox_get(entry_id: str):
    entry = _dumpbox.get_entry(entry_id)
    if entry is None:
        return {"error": "not found"}
    return entry


@app.delete("/dumpbox/{entry_id}")
def dumpbox_delete(entry_id: str):
    return {"deleted": _dumpbox.delete_entry(entry_id)}


@app.post("/dumpbox/{entry_id}/process")
def dumpbox_process(entry_id: str, model: str = Body("auto", embed=True)):
    used = {"model": "local"}

    def llm_call(prompt: str) -> str:
        reply, used_model = route_prompt(prompt, model)
        used["model"] = used_model
        return reply

    try:
        entry = _dumpbox.process_entry(entry_id, llm_call)
    except KeyError:
        return {"error": "not found"}
    except (RuntimeError, requests.RequestException) as e:
        return {"error": str(e)}

    return {**entry, "model_used": used["model"]}


@app.post("/dumpbox/{entry_id}/reminders")
def dumpbox_push_reminders(entry_id: str, item_ids: list = Body(None, embed=True)):
    list_name = load_settings().get("reminders_list", "")
    try:
        return _dumpbox.push_action_items(entry_id, item_ids, list_name)
    except KeyError:
        return {"error": "not found"}


@app.post("/dumpbox/{entry_id}/reminders/mark")
def dumpbox_mark_pushed(entry_id: str, item_ids: list = Body(..., embed=True)):
    try:
        return _dumpbox.mark_items_pushed(entry_id, item_ids)
    except KeyError:
        return {"error": "not found"}


@app.get("/reminders/lists")
def reminders_lists():
    result = _dumpbox.reminders_lists()
    if isinstance(result, dict):
        return result
    return {"lists": result}


# ---------- Google integration (Gmail / Calendar / Drive) ----------
import google_integration as _google


@app.get("/google/status")
def google_status():
    settings = load_settings()
    status = _google.auth_status()
    status["client_configured"] = bool(
        settings.get("google_client_id") and settings.get("google_client_secret")
    )
    return status


@app.post("/google/auth/start")
def google_auth_start():
    settings = load_settings()
    try:
        return _google.start_auth_flow(
            settings.get("google_client_id", ""),
            settings.get("google_client_secret", ""),
        )
    except (ValueError, RuntimeError) as e:
        return {"error": str(e)}


@app.post("/google/auth/disconnect")
def google_auth_disconnect():
    _google.clear_tokens()
    return {"connected": False}


@app.get("/google/gmail")
def google_gmail_list(query: str = "", max_results: int = 15, label: str = "INBOX"):
    try:
        return {"messages": _google.gmail_list(query, max_results, label)}
    except (RuntimeError, requests.RequestException) as e:
        return {"error": str(e)}


@app.get("/google/gmail/{message_id}")
def google_gmail_get(message_id: str):
    try:
        return _google.gmail_get(message_id)
    except (RuntimeError, requests.RequestException) as e:
        return {"error": str(e)}


@app.post("/google/gmail/draft")
def google_gmail_draft(to: str = Body(""), subject: str = Body(""),
                       body: str = Body(""), thread_id: str = Body(None)):
    try:
        return _google.gmail_create_draft(to, subject, body, thread_id)
    except (RuntimeError, requests.RequestException) as e:
        return {"error": str(e)}


@app.post("/google/gmail/summarize")
def google_gmail_summarize(query: str = Body(""), max_results: int = Body(10),
                           model: str = Body("auto")):
    """Summarize what's sitting in the inbox right now."""
    try:
        messages = _google.gmail_list(query, max_results)
    except (RuntimeError, requests.RequestException) as e:
        return {"error": str(e)}

    if not messages:
        return {"summary": "No messages matched.", "count": 0, "model_used": "none"}

    lines = [
        f"{i}. From: {m['from']} | Subject: {m['subject']}\n   {m['snippet'][:300]}"
        for i, m in enumerate(messages, 1)
    ]
    prompt = (
        "Summarize this inbox for a busy founder. Group related mail, call out anything "
        "that clearly needs a reply or has a deadline, and keep it under 200 words. "
        "Do not invent senders or details that aren't listed.\n\n" + "\n".join(lines)
    )

    try:
        reply, used_model = route_prompt(prompt, model)
    except (RuntimeError, requests.RequestException) as e:
        return {"error": str(e)}

    return {"summary": reply, "count": len(messages), "model_used": used_model}


@app.get("/google/calendar")
def google_calendar_list(days: int = 7, max_results: int = 25):
    try:
        return {"events": _google.calendar_list_events(days, max_results)}
    except (RuntimeError, requests.RequestException) as e:
        return {"error": str(e)}


@app.post("/google/calendar")
def google_calendar_create(summary: str = Body(...), start: str = Body(...),
                           end: str = Body(None), description: str = Body(""),
                           location: str = Body(""), attendees: list = Body(None)):
    try:
        return _google.calendar_create_event(summary, start, end, description, location, attendees)
    except (ValueError, RuntimeError, requests.RequestException) as e:
        return {"error": str(e)}


@app.get("/google/drive")
def google_drive_list(query: str = "", max_results: int = 20):
    try:
        return {"files": _google.drive_list(query, max_results)}
    except (RuntimeError, requests.RequestException) as e:
        return {"error": str(e)}


# ---------- Automations (n8n and other webhooks) ----------
import automations as _automations


@app.get("/automations")
def automations_list():
    return {"automations": _automations.list_automations()}


@app.post("/automations")
def automations_create(name: str = Body(...), webhook_url: str = Body(...),
                       description: str = Body(""), method: str = Body("POST"),
                       auth_header_name: str = Body(""), auth_header_value: str = Body("")):
    try:
        return _automations.create_automation(
            name, webhook_url, description, method, auth_header_name, auth_header_value
        )
    except ValueError as e:
        return {"error": str(e)}


# Declared before the /automations/{automation_id} route below: FastAPI matches
# routes in definition order, so a parameterized path registered first would
# swallow "match" as an automation id.
@app.post("/automations/match")
def automations_match(text: str = Body(..., embed=True)):
    match = _automations.match_automation(text)
    return {"match": match}


@app.post("/automations/{automation_id}")
def automations_update(automation_id: str, name: str = Body(None),
                       webhook_url: str = Body(None), description: str = Body(None),
                       method: str = Body(None), auth_header_name: str = Body(None),
                       auth_header_value: str = Body(None)):
    try:
        return _automations.update_automation(
            automation_id, name=name, webhook_url=webhook_url, description=description,
            method=method, auth_header_name=auth_header_name,
            auth_header_value=auth_header_value,
        )
    except KeyError:
        return {"error": "not found"}
    except ValueError as e:
        return {"error": str(e)}


@app.delete("/automations/{automation_id}")
def automations_delete(automation_id: str):
    return {"deleted": _automations.delete_automation(automation_id)}


@app.post("/automations/{automation_id}/run")
def automations_run(automation_id: str, payload: dict = Body(None, embed=True)):
    try:
        return _automations.run_automation(automation_id, payload)
    except KeyError:
        return {"error": "not found"}
