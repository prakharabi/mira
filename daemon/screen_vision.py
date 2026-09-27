"""Screen vision -- Mira looking at the screen when asked, and pointing at things on it.

Two capabilities, both only ever triggered by a request from the user:

  * look(question): "what's this error?", "summarize this page" -- a
    screenshot of the display under the cursor goes to a vision model along
    with the question, and the answer comes back as text for the agent.
  * point(target): "where's the export button?" -- the same screenshot, plus
    a highlight ring drawn over the element on the real screen.

The capture itself happens in the Electron app, not here: the daemon is a
headless LaunchAgent with no Screen Recording grant, while Mira.app already
has one for Control+Q OCR. So capturing and drawing go through the same
actions.py queue Reminders use.

Pointing does NOT trust the vision model's coordinates on their own. Asked for
a box around "Export CSV" on a 1600px-wide screenshot, qwen came back ~25px
off -- close, but enough to ring the wrong button in a dense toolbar. So the
model is asked *what* to point at (the element's visible label), and the
position comes from the local Vision-framework OCR boxes (ocr_helper --json),
which are exact. The model's own box is only the fallback for elements with
no text on them (icons).

Privacy: the screenshot is sent to the configured cloud provider only for
the one request, and deleted from disk as soon as it has been read.

Which model looks (Settings > Assistant > Vision model, `vision_provider`):
  * "auto" (default): the cloud vision model, but the moment Groq's free tier
    says "wait more than a few seconds" -- about every other step of a
    computer-control task, since it allows ~3 screenshots a minute -- that
    step goes to the local Ollama model instead of waiting. Also the fallback
    when the cloud is unreachable.
  * "cloud": cloud only, waiting out rate limits.
  * "local": the local model only; nothing leaves the Mac.
The local model (qwen3.5:9b by default, ~7GB loaded) is unloaded after the
same keep-alive as Mira's other local models, so it only holds memory while
it's actually being used.
"""

import base64
import difflib
import json
import os
import re
import subprocess
import tempfile
import time
from pathlib import Path

import requests

import actions

DEFAULT_VISION_MODEL = "qwen/qwen3.8-27b"
DEFAULT_LOCAL_VISION_MODEL = "qwen3.5:9b"
OLLAMA_URL = "http://localhost:11434"
LOCAL_SWITCH_WAIT = 3.0   # auto mode: a cloud wait longer than this goes local instead
AUTO_CLOUD_TIMEOUT = 15   # auto mode: a cloud call slower than this goes local too (one took 36s in testing)
LOCAL_TIMEOUT = 240       # first call includes loading ~7GB of weights
# Reading the image is most of a local step: on an M1 Pro, qwen3.5:9b took
# 18s for a 1600px screenshot and 12s at 1280, while still reading 11-13pt UI
# text. (The cloud's cost doesn't depend on size, so it keeps 1600.)
LOCAL_IMAGE_WIDTH = 1280
CAPTURE_TIMEOUT = 20     # action poll (2s) + capture + OCR on a full Retina screen
VISION_TIMEOUT = 60
MAX_RATE_LIMIT_WAIT = 20
VISION_ATTEMPTS = 3
POINTER_SECONDS = 7
CAPTURE_PREFIX = "mira_vision_"


def _settings() -> dict:
    from main import load_settings
    return load_settings()


def enabled() -> bool:
    return bool(_settings().get("screen_vision_enabled", True))


# ---------- capture ----------

def _capture(text_boxes: bool = False) -> dict:
    action_id = actions.enqueue("capture_screen", {"max_width": 1600, "text_boxes": text_boxes})
    result = actions.wait_for(action_id, timeout=CAPTURE_TIMEOUT)
    if result is None:
        raise RuntimeError("The Mira app didn't capture the screen in time. Is it running?")
    if not result.get("success"):
        raise RuntimeError(result.get("error") or "Screen capture failed.")
    return result


def _read_and_delete(path: str) -> bytes:
    """Only ever reads (and deletes) a capture Mira.app just wrote to the temp
    dir -- the path arrives over a local HTTP endpoint, so it's not trusted
    to point anywhere else."""
    p = Path(path or "").resolve()
    allowed = {Path(tempfile.gettempdir()).resolve(), Path("/private/var/folders").resolve(),
               Path("/private/tmp").resolve()}
    if not p.name.startswith(CAPTURE_PREFIX) or not any(a in p.parents for a in allowed):
        raise RuntimeError("Unexpected screenshot path from the app.")
    try:
        return p.read_bytes()
    finally:
        try:
            p.unlink()
        except OSError:
            pass


# ---------- model ----------

class _CloudUnavailable(RuntimeError):
    """The cloud can't answer right now without a wait longer than allowed
    (or at all) -- the cue for auto mode to use the local model."""


_local_check = {"at": 0.0, "models": set()}


def local_model_ready(model: str = "") -> bool:
    """Whether Ollama is up with the local vision model pulled (cached 60s)."""
    model = model or _settings().get("local_vision_model") or DEFAULT_LOCAL_VISION_MODEL
    if time.time() - _local_check["at"] > 60:
        try:
            tags = requests.get(f"{OLLAMA_URL}/api/tags", timeout=3).json()
            _local_check["models"] = {m.get("name", "") for m in tags.get("models", [])}
        except (requests.RequestException, ValueError):
            _local_check["models"] = set()
        _local_check["at"] = time.time()
    names = _local_check["models"]
    return model in names or f"{model}:latest" in names


def _vision(prompt: str, image: bytes, max_tokens: int = 600,
            max_wait: float = MAX_RATE_LIMIT_WAIT, on_wait=None, on_local=None) -> str:
    """One vision-model call, routed by `vision_provider` (see the module
    docstring). Rate-limit waits the cloud path is allowed to sit out are
    reported through `on_wait(seconds)`; a switch to the local model through
    `on_local()`, so a UI can say what's happening."""
    s = _settings()
    mode = s.get("vision_provider", "auto")
    local_model = s.get("local_vision_model") or DEFAULT_LOCAL_VISION_MODEL
    if mode == "local":
        return _vision_local(prompt, image, max_tokens, local_model, s)
    can_go_local = mode == "auto" and local_model_ready(local_model)
    try:
        return _vision_cloud(prompt, image, max_tokens,
                             LOCAL_SWITCH_WAIT if can_go_local else max_wait, on_wait, s,
                             timeout=AUTO_CLOUD_TIMEOUT if can_go_local else VISION_TIMEOUT)
    except _CloudUnavailable as e:
        if not can_go_local:
            raise RuntimeError(str(e))
        print(f"[screen_vision] cloud unavailable ({e}); using local {local_model}", flush=True)
        if on_local:
            on_local()
        return _vision_local(prompt, image, max_tokens, local_model, s)


def _shrink(image: bytes, width: int) -> bytes:
    """Downscale a JPEG with macOS's own sips (no imaging library needed)."""
    src = tempfile.NamedTemporaryFile(prefix=CAPTURE_PREFIX, suffix=".jpg", delete=False)
    dst = src.name[:-4] + "_small.jpg"
    try:
        src.write(image)
        src.close()
        subprocess.run(["sips", "-Z", str(width), "-s", "format", "jpeg", "-s", "formatOptions", "70",
                        src.name, "--out", dst], capture_output=True, timeout=10, check=True)
        return Path(dst).read_bytes()
    except (OSError, subprocess.SubprocessError):
        return image
    finally:
        for p in (src.name, dst):
            try:
                os.unlink(p)
            except OSError:
                pass


def _vision_local(prompt: str, image: bytes, max_tokens: int, model: str, s: dict) -> str:
    started = time.time()
    image = _shrink(image, LOCAL_IMAGE_WIDTH)
    try:
        resp = requests.post(f"{OLLAMA_URL}/api/chat", timeout=LOCAL_TIMEOUT, json={
            "model": model,
            "stream": False,
            # Thinking roughly triples the time per step for no gain on
            # "which button next" -- the prompt already asks for one sentence.
            "think": False,
            "keep_alive": s.get("local_model_keep_alive") or "60s",
            # qwen3.5 ships presence_penalty 1.5, which punishes repeating a
            # token -- exactly what a JSON reply with several steps does.
            "options": {"temperature": 0.2, "presence_penalty": 0, "num_predict": max_tokens,
                        "num_ctx": 6144},
            "messages": [{"role": "user", "content": prompt,
                          "images": [base64.b64encode(image).decode()]}],
        })
        data = resp.json()
    except (requests.RequestException, ValueError) as e:
        raise RuntimeError(f"Local vision model ({model}) didn't answer: {e}")
    if "message" not in data:
        raise RuntimeError(f"Local vision model ({model}) error: {data.get('error') or data}")
    text = data["message"].get("content") or ""
    print(f"[screen_vision] local {model} answered in {time.time() - started:.1f}s", flush=True)
    return re.sub(r"<think>.*?</think>", "", text, flags=re.S).strip()


def _vision_cloud(prompt: str, image: bytes, max_tokens: int, max_wait: float, on_wait, s: dict,
                  timeout: float = VISION_TIMEOUT) -> str:
    from main import GROQ_API_KEY
    api_key = s.get("cloud_api_key") or GROQ_API_KEY
    if not api_key:
        raise _CloudUnavailable("Screen vision needs a cloud API key (Settings > Assistant).")
    base_url = (s.get("cloud_base_url") or "https://api.groq.com/openai/v1").rstrip("/")
    payload = {
        "model": s.get("vision_model") or DEFAULT_VISION_MODEL,
        "temperature": 0.2,
        "max_tokens": max_tokens,
        "messages": [{"role": "user", "content": [
            {"type": "text", "text": prompt},
            {"type": "image_url",
             "image_url": {"url": "data:image/jpeg;base64," + base64.b64encode(image).decode()}},
        ]}],
    }
    msg = ""
    for attempt in range(VISION_ATTEMPTS):
        try:
            resp = requests.post(f"{base_url}/chat/completions", json=payload, timeout=timeout,
                                 headers={"Authorization": f"Bearer {api_key}"})
        except requests.RequestException as e:
            raise _CloudUnavailable(f"couldn't reach the cloud model: {e}")
        try:
            data = resp.json()
        except ValueError:
            data = {"error": {"message": f"HTTP {resp.status_code}: {resp.text[:200]}"}}
        if "choices" in data:
            text = data["choices"][0]["message"].get("content") or ""
            # Qwen can emit its reasoning inline; only the answer is wanted.
            return re.sub(r"<think>.*?</think>", "", text, flags=re.S).strip()
        err = data.get("error") or {}
        msg = err.get("message", "") if isinstance(err, dict) else str(err)
        wait = re.search(r"try again in ([\d.]+)(ms|s)", msg)
        if wait:
            secs = float(wait.group(1)) / (1000 if wait.group(2) == "ms" else 1)
            if secs > max_wait:
                raise _CloudUnavailable(f"rate-limited for {secs:.0f}s")
            if on_wait:
                on_wait(secs)
            time.sleep(secs + 0.5)
        elif resp.status_code >= 500 or "capacity" in msg.lower():
            # Groq's own advice for "over capacity": retry with backoff. Seen
            # in testing on the free tier; it usually clears within seconds.
            if attempt == VISION_ATTEMPTS - 1:
                raise _CloudUnavailable(msg or f"HTTP {resp.status_code}")
            time.sleep(1.5 * 2 ** attempt)
        else:
            break
    # Any cloud failure (bad key, unknown model, repeated capacity errors)
    # is a reason for auto mode to try the local model.
    raise _CloudUnavailable(f"Vision model error: {msg or 'no response'}")


# ---------- look ----------

def look(question: str) -> dict:
    if not enabled():
        return {"error": "Screen vision is turned off in Settings."}
    question = (question or "").strip() or "What's on the screen?"
    shot = _capture()
    image = _read_and_delete(shot["path"])
    prompt = (
        "This is a screenshot of the user's Mac screen right now.\n"
        f"The user asks: {question}\n\n"
        "Answer from what is actually visible. Be specific: quote exact text, numbers, names and "
        "error messages. If the answer isn't on screen, say so and briefly say what is. "
        "Plain text, no markdown, under 120 words."
    )
    return {"answer": _vision(prompt, image)}


# ---------- point ----------

def _parse_json(text: str) -> dict:
    m = re.search(r"\{.*\}", text or "", flags=re.S)
    if not m:
        return {}
    try:
        return json.loads(m.group(0))
    except json.JSONDecodeError:
        return {}


def _norm(s: str) -> str:
    return " ".join(re.sub(r"[^\w\s]", " ", (s or "").lower()).split())


def _snap_to_text(label: str, boxes: list, near=None):
    """The OCR line that best matches `label` (normalized 0..1 box, top-left
    origin). Several matches -- "Save" in two panes -- go to the one nearest
    the model's own guess."""
    want = _norm(label)
    if not want or not boxes:
        return None
    scored = []
    for b in boxes:
        have = _norm(b.get("text", ""))
        if not have:
            continue
        if have == want:
            score = 1.0
        elif want in have.split() or (len(want) > 3 and want in have):
            score = 0.9 if len(have) <= len(want) * 2.5 else 0.75
        elif len(have) > 3 and have in want:
            score = 0.8
        else:
            score = difflib.SequenceMatcher(None, want, have).ratio()
        if score >= 0.75:
            scored.append((score, b))
    if not scored:
        return None
    best = max(s for s, _ in scored)
    top = [b for s, b in scored if s >= best - 0.05]
    if near and len(top) > 1:
        cx, cy = near
        top.sort(key=lambda b: (b["x"] + b["w"] / 2 - cx) ** 2 + (b["y"] + b["h"] / 2 - cy) ** 2)
    return top[0]


def point(target: str, question: str = "") -> dict:
    if not enabled():
        return {"error": "Screen vision is turned off in Settings."}
    target = (target or "").strip()
    if not target:
        return {"error": "What should I point at?"}
    shot = _capture(text_boxes=True)
    image = _read_and_delete(shot["path"])
    boxes = shot.get("text_boxes") or []
    prompt = (
        "This is a screenshot of the user's Mac screen right now.\n"
        f"The user wants to find: {target}\n"
        + (f"Their question: {question}\n" if question else "")
        + "\nFind the single on-screen element they should look at or click. Reply with ONLY this JSON:\n"
        '{"found": true or false, "label": "the exact text written on or next to that element, '
        'copied from the screen, or \\"\\" if it has no text", "box": [x1, y1, x2, y2] around the element '
        'using 0-1000 coordinates relative to the image width and height, "answer": "one or two short '
        'sentences telling the user where it is and what to do"}'
    )
    reply = _parse_json(_vision(prompt, image, max_tokens=400))
    if not reply:
        return {"error": "Couldn't work out where that is on screen."}
    answer = str(reply.get("answer") or "").strip()
    if not reply.get("found"):
        return {"pointed": False, "answer": answer or f"I can't see {target} on your screen."}

    model_box = reply.get("box")
    near = None
    if isinstance(model_box, list) and len(model_box) == 4:
        try:
            x1, y1, x2, y2 = (float(v) / 1000 for v in model_box)
            model_box = (min(x1, x2), min(y1, y2), abs(x2 - x1), abs(y2 - y1))
            near = (model_box[0] + model_box[2] / 2, model_box[1] + model_box[3] / 2)
        except (TypeError, ValueError):
            model_box = None
    else:
        model_box = None

    hit = _snap_to_text(str(reply.get("label") or ""), boxes, near) or _snap_to_text(target, boxes, near)
    if hit:
        # OCR boxes hug the glyphs; pad so the ring sits around the control.
        pad_x, pad_y = hit["h"] * 0.9, hit["h"] * 0.6
        nx, ny, nw, nh = hit["x"] - pad_x, hit["y"] - pad_y, hit["w"] + 2 * pad_x, hit["h"] + 2 * pad_y
        precision = "exact (matched the text on screen)"
        label = hit["text"]
    elif model_box and model_box[2] > 0 and model_box[3] > 0:
        nx, ny, nw, nh = model_box
        precision = "approximate (no matching text on screen)"
        label = str(reply.get("label") or target)
    else:
        return {"pointed": False, "answer": answer or f"I can't pin down {target} on your screen."}

    d = shot["display"]  # the captured display, in global screen points
    rect = {"x": d["x"] + nx * d["width"], "y": d["y"] + ny * d["height"],
            "w": nw * d["width"], "h": nh * d["height"]}
    actions.enqueue("show_pointer", {**{k: round(v, 1) for k, v in rect.items()},
                                     "label": label[:60], "seconds": POINTER_SECONDS})
    print(f"[screen_vision] pointing at {label!r} {({k: round(v) for k, v in rect.items()})} ({precision})",
          flush=True)
    return {"pointed": True, "pointed_at": label, "precision": precision, "answer": answer}
