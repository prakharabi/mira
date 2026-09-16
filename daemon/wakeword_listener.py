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

import voice_state


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
# Below this RMS a recording is treated as having no speech in it. Whisper
# hallucinates fluent nonsense on near-silence, so the guard earns its place --
# but it is an absolute level, which means a quiet input device or a low system
# input volume trips it just as surely as an empty room. When it trips, say so.
SILENCE_RMS_FLOOR = 150

WAKEWORD_SESSION_ID = "wakeword_voice"

# module-level handle to the running listener controller, so /settings can pause/resume it
_controller = None


def record_command_audio(pa: pyaudio.PyAudio, seconds: int) -> tuple:
    """Records `seconds` of audio from the mic and saves it as a WAV file for transcription.
    Returns (path, has_meaningful_audio, rms, device_name) -- the guard is a volume check
    against feeding near-silent/noise-only recordings to Whisper, which can hallucinate
    fluent-sounding but meaningless text on such input."""
    stream = pa.open(
        format=pyaudio.paInt16,
        channels=1,
        rate=SAMPLE_RATE,
        input=True,
        frames_per_buffer=CHUNK_SIZE
    )
    try:
        device_name = pa.get_default_input_device_info().get("name", "the microphone")
    except Exception:
        device_name = "the microphone"

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
    has_meaningful_audio = rms > SILENCE_RMS_FLOOR

    return TEMP_RECORDING_PATH, has_meaningful_audio, float(rms), device_name


def notify(title: str, body: str):
    """A visible alert, for failures that cannot be reported by voice."""
    script = ('on run argv\n'
              '  display notification (item 2 of argv) with title (item 1 of argv)\n'
              'end run')
    try:
        subprocess.run(["osascript", "-e", script, "--", title, body],
                       capture_output=True, timeout=10)
    except (subprocess.SubprocessError, OSError):
        pass


def load_settings_safe() -> dict:
    """Settings, without tripping the circular import guarded against below."""
    try:
        from main import load_settings
        return load_settings()
    except Exception:
        return {}


def play_chime():
    """Short acknowledgment sound so the user knows Mira is now listening --
    matters most for the manual trigger, where there's no spoken wake word to confirm."""
    subprocess.run(["afplay", "/System/Library/Sounds/Tink.aiff"], capture_output=True)


def handle_wake_detected(pa: pyaudio.PyAudio, chime: bool = False):
    # chime=True means this came from the shortcut, not the spoken wake word.
    # Logging "'Hey Mira' detected" either way made shortcut sessions look like
    # misfires of a wake word nobody said.
    log("[wakeword] Shortcut triggered, recording command..." if chime
        else "[wakeword] 'Hey Mira' detected, recording command...")

    if chime:
        play_chime()

    # Reset to idle on every exit from here down, early return or not -- the
    # notch's listening/thinking animation would otherwise stay stuck on
    # whatever it last showed if a run ends in silence or an empty transcript.
    try:
        voice_state.set_state("listening")
        audio_path, has_meaningful_audio, rms, device_name = record_command_audio(
            pa, COMMAND_RECORD_SECONDS)

        if not has_meaningful_audio:
            # Saying nothing here was the whole problem: Mira took the trigger,
            # recorded, decided it heard nothing and returned in silence -- which
            # from the outside is indistinguishable from the shortcut being broken.
            log(f"[wakeword] Near-silent recording from '{device_name}' "
                f"(RMS {rms:.0f}, floor {SILENCE_RMS_FLOOR}).")
            # Deliberately a NOTIFICATION rather than a spoken reply. When the
            # cause is the audio route -- a Bluetooth headset taking the default
            # input, say -- announcing it through that same route is useless: the
            # user is not listening to the device Mira is talking into. Naming the
            # device is the whole point, since that is the thing to change.
            notify("Mira didn't hear you",
                   f"Nothing audible came from \u201c{device_name}\u201d. "
                   f"Check that it is the right input and that input volume is up.")
            return

        # these are imported here (not at module top) because wakeword_listener is
        # imported BY main.py at startup -- importing main.py's own names at module
        # load time would create a circular import. Safe to import here since this
        # function only runs after main.py has fully finished loading.
        from main import (
            transcribe_smart, load_history, save_history, load_settings,
            remember_exchange_async, GROQ_API_KEY,
        )
        import agent

        voice_state.set_state("thinking")
        text, _engine_used, _err = transcribe_smart(audio_path)

        if not text or not text.strip():
            log("[wakeword] No speech detected in command window, ignoring.")
            return

        log(f"[wakeword] Heard: {text}")

        history = load_history(WAKEWORD_SESSION_ID)
        history.append({"role": "user", "content": text})

        # Same agent as chat and Telegram, so spoken requests can create reminders,
        # search the web or control music too -- previously voice was the only
        # surface with no access to any of Mira's actual capabilities. The "voice"
        # surface tells it to answer in short spoken prose rather than markdown.
        reply, meta = agent.run_agent(
            history=history,
            user_message=text,
            model_pref="auto",
            settings=load_settings(),
            groq_key=GROQ_API_KEY,
            surface="voice",
        )
        if not reply:
            reply = "Sorry, I couldn't work that one out."

        history.append({"role": "assistant", "content": reply})
        save_history(WAKEWORD_SESSION_ID, history)
        remember_exchange_async(text, reply)

        log(f"[wakeword] Reply: {reply}")

        if load_settings().get("voice_response_enabled", True):
            speak_reply(reply)
    finally:
        voice_state.set_state("idle")


def speak_reply(text: str):
    """Uses the same macOS `say` mechanism as the /speak endpoint, but plays directly
    since this runs inside the daemon process (no HTTP round-trip needed)."""
    import uuid
    from main import load_settings, resolve_tts_voice

    TTS_DIR = Path.home() / "Mira" / "daemon" / "tts_output"
    TTS_DIR.mkdir(parents=True, exist_ok=True)
    filepath = TTS_DIR / f"{uuid.uuid4().hex}.aiff"

    voice = resolve_tts_voice(text, load_settings())
    cmd = ["say", "-o", str(filepath)]
    if voice:
        cmd += ["-v", voice]
    cmd.append(text)

    subprocess.run(cmd, capture_output=True)
    # speak_reply is also the path proactive.py's spoken delivery uses (see
    # proactive.py's _speak), so this is the one place that covers every kind
    # of speech Mira produces, not just wakeword replies -- the notch's
    # speaking animation should show for all of it.
    voice_state.set_state("speaking", text)
    try:
        subprocess.run(["afplay", str(filepath)])
    finally:
        voice_state.set_state("idle")


class WakewordController:
    """Wraps the listen loop with real start/stop control. When paused, the PyAudio
    stream and model are fully released -- not just internally ignored -- so the
    menu bar mic indicator actually disappears while wake-word listening is off."""

    def __init__(self):
        self._thread = None
        self._running = threading.Event()  # set = should be actively listening
        self._stop_requested = threading.Event()
        self._manual_trigger = threading.Event()  # set = a manual (button/shortcut) trigger is pending

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

    def trigger_manual(self):
        """Skips the 'Hey Mira' phrase and goes straight to recording a command --
        used by the global keyboard shortcut / UI button. If the continuous listener
        loop is already running, this just flags it to handle the next iteration
        (reusing that loop's own mic stream, exactly like a real wake-word detection
        would). If the listener is currently paused, spins up a one-off mic session
        instead so the manual trigger still works even with wake-word listening off."""
        if self.is_running():
            self._manual_trigger.set()
        else:
            threading.Thread(target=self._manual_one_shot, daemon=True).start()

    def _manual_one_shot(self):
        log("[wakeword] Manual trigger (listener currently paused) -- starting one-off session.")
        pa = pyaudio.PyAudio()
        try:
            handle_wake_detected(pa, chime=True)
        except Exception:
            import traceback
            log("[wakeword] Error handling manual trigger:")
            log(traceback.format_exc())
        finally:
            pa.terminate()

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
                if self._manual_trigger.is_set():
                    self._manual_trigger.clear()
                    log("[wakeword] Manual trigger received, recording command...")
                    last_trigger_time = time.time()

                    stream.stop_stream()
                    try:
                        handle_wake_detected(pa, chime=True)
                    except Exception:
                        import traceback
                        log("[wakeword] Error handling manual trigger (listener continues):")
                        log(traceback.format_exc())
                    stream.start_stream()
                    continue

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


def trigger_manual_command():
    """Called by main.py's /wakeword/trigger endpoint (keyboard shortcut / UI button).
    Skips the 'Hey Mira' phrase and records+answers a command immediately."""
    global _controller
    if _controller is None:
        log("[wakeword] Controller not initialized yet, cannot trigger.")
        return False
    _controller.trigger_manual()
    return True
