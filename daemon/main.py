from fastapi import FastAPI, Body, UploadFile, File
from fastapi.responses import FileResponse
from contextlib import asynccontextmanager
from dotenv import load_dotenv
import requests
import os
import json
import subprocess
import uuid
import time
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
    yield
    # shutdown (nothing needed here currently)


app = FastAPI(lifespan=lifespan)

# ---------- Settings (voice_response_enabled toggle, etc) ----------
SETTINGS_PATH = Path.home() / "Mira" / "daemon" / "settings.json"

def load_settings():
    if SETTINGS_PATH.exists():
        try:
            return json.loads(SETTINGS_PATH.read_text())
        except (json.JSONDecodeError, OSError):
            pass
    return {"voice_response_enabled": True, "wakeword_enabled": True}

def save_settings(settings: dict):
    SETTINGS_PATH.write_text(json.dumps(settings, indent=2))

@app.get("/settings")
def get_settings():
    return load_settings()

@app.post("/settings")
def update_settings(voice_response_enabled: bool = Body(None), wakeword_enabled: bool = Body(None)):
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

    save_settings(settings)
    return settings


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
WHISPER_MODEL = Path.home() / "Mira" / "whisper.cpp" / "models" / "ggml-base.bin"

# ffmpeg's full path -- LaunchAgent daemons run with a minimal PATH that does NOT
# include Homebrew's /opt/homebrew/bin, so "ffmpeg" alone is not found. Confirm this
# matches `which ffmpeg` on your machine (Intel Macs often use /usr/local/bin/ffmpeg instead).
FFMPEG_PATH = "/opt/homebrew/bin/ffmpeg"

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


@app.get("/complete")
def complete_mira(prompt: str):
    response = requests.post(
        "http://localhost:11434/api/generate",
        json={
            "model": "batiai/gemma4-e2b:q4",
            "prompt": prompt,
            "stream": False,
            "think": False,
            "options": {
                "num_predict": 10,
                "temperature": 0.15,
                "repeat_penalty": 1.3
            }
        }
    )
    result = response.json()
    return {"response": result["response"]}


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

def call_local_model(history: list) -> str:
    response = requests.post(
        "http://localhost:11434/api/chat",
        json={
            "model": "batiai/gemma4-e2b:q4",
            "messages": history,
            "stream": False,
            "think": False
        }
    )
    result = response.json()
    return result["message"]["content"]

def call_cloud_model(history: list):
    response = requests.post(
        "https://api.groq.com/openai/v1/chat/completions",
        headers={"Authorization": f"Bearer {GROQ_API_KEY}"},
        json={
            "model": "llama-3.3-70b-versatile",
            "messages": history
        }
    )
    result = response.json()
    if "choices" not in result:
        return None, result
    return result["choices"][0]["message"]["content"], None


# ---------- Groq/local routed chatbot endpoint ----------
@app.post("/chat")
def chat(session_id: str = Body(...), message: str = Body(...), model: str = Body("auto")):
    history = load_history(session_id)
    history.append({"role": "user", "content": message})

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

    else:  # auto
        if needs_cloud(message) and has_internet():
            reply, error = call_cloud_model(history)
            if reply is not None:
                used_model = "cloud"
            else:
                reply = call_local_model(history)
                used_model = "local"
        else:
            reply = call_local_model(history)
            used_model = "local"

    history.append({"role": "assistant", "content": reply})
    save_history(session_id, history)

    return {"response": reply, "model_used": used_model}


# ---------- Short-clip transcription (used by predictive typing, unrelated to meetings) ----------
@app.post("/transcribe")
async def transcribe(audio: UploadFile = File(...)):
    audio_bytes = await audio.read()

    response = requests.post(
        "https://api.groq.com/openai/v1/audio/translations",
        headers={"Authorization": f"Bearer {GROQ_API_KEY}"},
        files={
            "file": (audio.filename, audio_bytes, audio.content_type)
        },
        data={
            "model": "whisper-large-v3"
        }
    )
    result = response.json()

    if "text" not in result:
        return {"error": result}

    return {"text": result["text"]}


# ---------- NEW: long-file transcription with local fallback ----------
def transcribe_with_groq(filepath: Path):
    try:
        with open(filepath, "rb") as f:
            response = requests.post(
                "https://api.groq.com/openai/v1/audio/translations",
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

def transcribe_with_whisper_cpp(filepath: Path):
    # whisper.cpp requires 16kHz mono WAV input; convert with ffmpeg first
    converted_path = filepath.with_suffix(".converted.wav")
    convert_result = subprocess.run(
        [FFMPEG_PATH, "-y", "-i", str(filepath), "-ar", "16000", "-ac", "1", str(converted_path)],
        capture_output=True, text=True
    )
    if convert_result.returncode != 0:
        return None, {"error": "ffmpeg conversion failed", "details": convert_result.stderr}

    result = subprocess.run(
        [str(WHISPER_CLI), "-m", str(WHISPER_MODEL), "-f", str(converted_path), "-l", "auto", "-tr", "-nt", "-otxt"],
        capture_output=True, text=True
    )
    if result.returncode != 0:
        return None, {"error": "whisper.cpp failed", "details": result.stderr}

    txt_output = converted_path.with_suffix(".wav.txt")
    if txt_output.exists():
        return txt_output.read_text().strip(), None

    # some whisper.cpp versions print directly to stdout instead of a .txt file
    if result.stdout.strip():
        return result.stdout.strip(), None

    return None, {"error": "no transcription output found"}

@app.post("/transcribe-file")
async def transcribe_file(audio: UploadFile = File(...)):
    """
    Accepts any audio/video file (meeting recordings, voice memos, etc).
    Tries Groq Whisper first (if internet available), falls back to local whisper.cpp.
    """
    temp_path = MEETINGS_DIR / f"upload_{uuid.uuid4().hex}_{audio.filename}"
    audio_bytes = await audio.read()
    temp_path.write_bytes(audio_bytes)

    text = None
    engine_used = None
    error_info = None

    if has_internet():
        text, error_info = transcribe_with_groq(temp_path)
        if text is not None:
            engine_used = "groq"

    if text is None:
        text, fallback_error = transcribe_with_whisper_cpp(temp_path)
        if text is not None:
            engine_used = "whisper.cpp"
        else:
            return {"error": {"groq_error": error_info, "local_error": fallback_error}}

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

    text = None
    engine_used = None
    error_info = None

    if has_internet():
        text, error_info = transcribe_with_groq(path)
        if text is not None:
            engine_used = "groq"

    if text is None:
        text, fallback_error = transcribe_with_whisper_cpp(path)
        if text is not None:
            engine_used = "whisper.cpp"
        else:
            return {"error": {"groq_error": error_info, "local_error": fallback_error}}

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
def speak(text: str = Body(..., embed=True)):
    filename = f"{uuid.uuid4().hex}.aiff"
    filepath = TTS_DIR / filename

    result = subprocess.run(
        ["say", "-o", str(filepath), text],
        capture_output=True,
        text=True
    )

    if result.returncode != 0:
        return {"error": result.stderr}

    return FileResponse(path=str(filepath), media_type="audio/aiff", filename=filename)
