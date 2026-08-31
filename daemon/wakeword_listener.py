"""
wakeword_listener.py

Runs inside the main daemon process as a background thread. Continuously listens
to the microphone for the "Hey Mira" wake word using openWakeWord. On detection,
records a few seconds of follow-up speech, transcribes it, sends it through the
same routed /chat logic as the chat window, and optionally speaks the reply aloud
(controlled by a settings file toggle).

Uses the SAME microphone access path as MicHelper (this whole thing runs inside
the daemon's own Python process, which is already code-signed with the
com.apple.security.device.audio-input entitlement) -- no new permission setup needed.
"""

import threading
import time
import json
import wave
import subprocess
import sys
from pathlib import Path

import numpy as np
import pyaudio
from openwakeword.model import Model


def log(message: str):
    """print() alone can be buffered and not show up promptly in LaunchAgent logs --
    force an explicit flush every time so we can actually see what's happening."""
    print(message, flush=True)

WAKEWORD_DIR = Path.home() / "Mira" / "daemon" / "wakeword"
WAKEWORD_MODEL_PATH = WAKEWORD_DIR / "hey_mira.onnx"
TEMP_RECORDING_PATH = WAKEWORD_DIR / "command_recording.wav"

SAMPLE_RATE = 16000
CHUNK_SIZE = 1280  # openWakeWord's expected frame size at 16kHz
COMMAND_RECORD_SECONDS = 4
DETECTION_THRESHOLD = 0.5
DETECTION_COOLDOWN_SECONDS = 3  # avoid re-triggering immediately on the same utterance

WAKEWORD_SESSION_ID = "wakeword_voice"

# module-level handle to the running listener controller, so /settings can pause/resume it
_controller = None


def record_command_audio(pa: pyaudio.PyAudio, seconds: int) -> Path:
    """Records `seconds` of audio from the mic and saves it as a WAV file for transcription.
    Returns (path, has_meaningful_audio) -- the second value is a cheap volume-based guard
    against feeding near-silent/noise-only recordings to Whisper, which can hallucinate
    fluent-sounding but meaningless text on such input."""
    stream = pa.open(
        format=pyaudio.paInt16,
        channels=1,
        rate=SAMPLE_RATE,
        input=True,
        frames_per_buffer=CHUNK_SIZE
    )

    frames = []
    num_chunks = int(SAMPLE_RATE / CHUNK_SIZE * seconds)
    for _ in range(num_chunks):
        data = stream.read(CHUNK_SIZE, exception_on_overflow=False)
        frames.append(data)

    stream.stop_stream()
    stream.close()

    with wave.open(str(TEMP_RECORDING_PATH), "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(pa.get_sample_size(pyaudio.paInt16))
        wf.setframerate(SAMPLE_RATE)
        wf.writeframes(b"".join(frames))

    # cheap volume check: if the recording is almost entirely near-zero amplitude,
    # there's likely no real speech in it at all
    all_audio = np.frombuffer(b"".join(frames), dtype=np.int16)
    rms = np.sqrt(np.mean(all_audio.astype(np.float64) ** 2)) if len(all_audio) else 0
    has_meaningful_audio = rms > 150  # empirical floor for "some real signal present"

    return TEMP_RECORDING_PATH, has_meaningful_audio


def handle_wake_detected(pa: pyaudio.PyAudio):
    log("[wakeword] 'Hey Mira' detected, recording command...")

    audio_path, has_meaningful_audio = record_command_audio(pa, COMMAND_RECORD_SECONDS)

    if not has_meaningful_audio:
        log("[wakeword] Recording was near-silent, skipping transcription.")
        return

    # these are imported here (not at module top) because wakeword_listener is
    # imported BY main.py at startup -- importing main.py's own names at module
    # load time would create a circular import. Safe to import here since this
    # function only runs after main.py has fully finished loading.
    from main import (
        transcribe_with_groq, transcribe_with_whisper_cpp, has_internet,
        load_history, save_history, call_local_model, call_cloud_model,
        needs_cloud, load_settings
    )

    text = None
    if has_internet():
        text, _ = transcribe_with_groq(audio_path)
    if text is None:
        text, _ = transcribe_with_whisper_cpp(audio_path)

    if not text or not text.strip():
        log("[wakeword] No speech detected in command window, ignoring.")
        return

    log(f"[wakeword] Heard: {text}")

    history = load_history(WAKEWORD_SESSION_ID)
    history.append({"role": "user", "content": text})

    if needs_cloud(text) and has_internet():
        reply, error = call_cloud_model(history)
        if reply is None:
            reply = call_local_model(history)
    else:
        reply = call_local_model(history)

    history.append({"role": "assistant", "content": reply})
    save_history(WAKEWORD_SESSION_ID, history)

    log(f"[wakeword] Reply: {reply}")

    if load_settings().get("voice_response_enabled", True):
        speak_reply(reply)


def speak_reply(text: str):
    """Uses the same macOS `say` mechanism as the /speak endpoint, but plays directly
    since this runs inside the daemon process (no HTTP round-trip needed)."""
    import uuid
    TTS_DIR = Path.home() / "Mira" / "daemon" / "tts_output"
    TTS_DIR.mkdir(parents=True, exist_ok=True)
    filepath = TTS_DIR / f"{uuid.uuid4().hex}.aiff"

    subprocess.run(["say", "-o", str(filepath), text], capture_output=True)
    subprocess.run(["afplay", str(filepath)])


class WakewordController:
    """Wraps the listen loop with real start/stop control. When paused, the PyAudio
    stream and model are fully released -- not just internally ignored -- so the
    menu bar mic indicator actually disappears while wake-word listening is off."""

    def __init__(self):
        self._thread = None
        self._running = threading.Event()  # set = should be actively listening
        self._stop_requested = threading.Event()

    def start(self):
        if self._thread is not None and self._thread.is_alive():
            log("[wakeword] Already running, ignoring start request.")
            return
        self._stop_requested.clear()
        self._running.set()
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()
        log("[wakeword] Controller: start requested.")

    def stop(self):
        log("[wakeword] Controller: stop requested.")
        self._running.clear()
        self._stop_requested.set()

    def is_running(self):
        return self._running.is_set()

    def _loop(self):
        if not WAKEWORD_MODEL_PATH.exists():
            log(f"[wakeword] Model file not found at {WAKEWORD_MODEL_PATH}, wake-word listening disabled.")
            return

        log("[wakeword] Loading model...")
        try:
            oww_model = Model(wakeword_models=[str(WAKEWORD_MODEL_PATH)])
        except Exception:
            import traceback
            log("[wakeword] Failed to load model:")
            log(traceback.format_exc())
            return

        pa = pyaudio.PyAudio()
        stream = pa.open(
            format=pyaudio.paInt16,
            channels=1,
            rate=SAMPLE_RATE,
            input=True,
            frames_per_buffer=CHUNK_SIZE
        )

        log("[wakeword] Listening for 'Hey Mira'...")
        last_trigger_time = 0
        loop_count = 0

        try:
            while not self._stop_requested.is_set():
                audio_chunk = np.frombuffer(
                    stream.read(CHUNK_SIZE, exception_on_overflow=False),
                    dtype=np.int16
                )
                predictions = oww_model.predict(audio_chunk)

                loop_count += 1
                if loop_count % 100 == 0:
                    top_score = max(predictions.values()) if predictions else 0
                    log(f"[wakeword] alive, top score: {top_score:.3f}")

                for model_name, score in predictions.items():
                    if score > DETECTION_THRESHOLD:
                        now = time.time()
                        if now - last_trigger_time < DETECTION_COOLDOWN_SECONDS:
                            continue
                        last_trigger_time = now

                        log(f"[wakeword] Triggered by model '{model_name}' at score {score:.3f}")

                        stream.stop_stream()
                        try:
                            handle_wake_detected(pa)
                        except Exception:
                            import traceback
                            log("[wakeword] Error handling wake detection (listener continues):")
                            log(traceback.format_exc())
                        stream.start_stream()
        except Exception:
            import traceback
            log("[wakeword] Listener crashed:")
            log(traceback.format_exc())
        finally:
            # fully release the mic -- this is what makes the menu bar indicator disappear
            stream.stop_stream()
            stream.close()
            pa.terminate()
            log("[wakeword] Stopped, microphone released.")


def start_wakeword_listener_background():
    """Call this once from main.py at startup. Reads settings.json to decide whether
    to actually start listening immediately, or stay paused until enabled via /settings."""
    global _controller
    log("[wakeword] Initializing controller...")
    _controller = WakewordController()

    from main import load_settings
    if load_settings().get("wakeword_enabled", True):
        _controller.start()
    else:
        log("[wakeword] wakeword_enabled is false in settings, staying paused.")

    return _controller


def set_wakeword_enabled(enabled: bool):
    """Called by main.py's /settings endpoint to toggle listening on/off at runtime."""
    global _controller
    if _controller is None:
        log("[wakeword] Controller not initialized yet, cannot toggle.")
        return
    if enabled:
        _controller.start()
    else:
        _controller.stop()
