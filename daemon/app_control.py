"""Opening and controlling other macOS apps.

Two mechanisms, chosen per operation by what permission each needs:

  * Launching / activating / quitting apps goes through LaunchServices
    (`open -a`), which needs no special consent at all.
  * Playback and volume go through system MEDIA KEYS via the media_key helper,
    not AppleScript. Driving Music.app or Spotify with Apple Events would
    require an Automation grant per target app, and the daemon is a headless
    LaunchAgent that cannot obtain one. Media keys additionally work for
    whatever is actually playing -- Spotify, Music, YouTube in a browser --
    instead of only the one app we thought to script.

Reading "what's playing right now" genuinely does need Apple Events, so that
one is best-effort and degrades to a clear message rather than pretending.
"""

import json
import re
import subprocess
from pathlib import Path

# Resolved from this file's location (repo_root/electron/media_key), not a
# hardcoded ~/Mira -- that only worked for a checkout living at exactly that
# path. Deliberately NOT named MIRA_HOME/an env override like the daemon's
# user-data paths elsewhere (memory_store, meetings, etc. under ~/Mira) --
# this points at a build artifact in the SOURCE tree, a different thing
# entirely, and reusing that name here would mean two different defaults for
# the same variable depending which file you're reading.
_REPO_ROOT = Path(__file__).resolve().parent.parent
MEDIA_KEY_PATH = _REPO_ROOT / "electron" / "media_key"

# Friendly names people actually say -> the app macOS knows about.
APP_ALIASES = {
    "music": "Music",
    "apple music": "Music",
    "itunes": "Music",
    "spotify": "Spotify",
    "browser": "Safari",
    "chrome": "Google Chrome",
    "vscode": "Visual Studio Code",
    "vs code": "Visual Studio Code",
    "code": "Visual Studio Code",
    "terminal": "Terminal",
    "notes": "Notes",
    "mail": "Mail",
    "calendar": "Calendar",
    "reminders": "Reminders",
    "messages": "Messages",
    "whatsapp": "WhatsApp",
    "slack": "Slack",
    "finder": "Finder",
    "preview": "Preview",
    "photos": "Photos",
    "settings": "System Settings",
    "system settings": "System Settings",
    "system preferences": "System Settings",
}

MEDIA_ACTIONS = {"playpause", "play", "pause", "next", "previous", "volumeup", "volumedown", "mute"}


def _resolve_app_name(name: str) -> str:
    key = (name or "").strip().lower()
    return APP_ALIASES.get(key, (name or "").strip())


def open_app(name: str) -> dict:
    """Launch or focus an app by name."""
    app = _resolve_app_name(name)
    if not app:
        return {"success": False, "error": "no app name given"}

    try:
        result = subprocess.run(["/usr/bin/open", "-a", app],
                                capture_output=True, text=True, timeout=15)
    except (subprocess.SubprocessError, OSError) as e:
        return {"success": False, "error": str(e)}

    if result.returncode != 0:
        err = (result.stderr or "").strip()
        return {"success": False, "error": err or f"could not open {app}"}
    return {"success": True, "app": app}


def open_url(url: str) -> dict:
    """Open a URL (or a deep link like spotify:track:...) in the default handler."""
    if not re.match(r"^[a-zA-Z][a-zA-Z0-9+.-]*:", url or ""):
        return {"success": False, "error": "not a valid URL or scheme"}
    try:
        subprocess.run(["/usr/bin/open", url], capture_output=True, text=True, timeout=15)
    except (subprocess.SubprocessError, OSError) as e:
        return {"success": False, "error": str(e)}
    return {"success": True, "url": url}


def quit_app(name: str) -> dict:
    """Quit an app. Uses `pkill -f` on the bundle path rather than Apple Events."""
    app = _resolve_app_name(name)
    if not app:
        return {"success": False, "error": "no app name given"}
    try:
        result = subprocess.run(["/usr/bin/pkill", "-x", app],
                                capture_output=True, text=True, timeout=10)
    except (subprocess.SubprocessError, OSError) as e:
        return {"success": False, "error": str(e)}
    # pkill exits 1 when nothing matched, which just means it wasn't running
    if result.returncode not in (0, 1):
        return {"success": False, "error": (result.stderr or "").strip()}
    return {"success": True, "app": app, "was_running": result.returncode == 0}


def media_control(action: str) -> dict:
    """Play/pause, skip, or change volume via system media keys."""
    action = (action or "").strip().lower().replace(" ", "").replace("_", "")
    if action in ("skip", "nexttrack"):
        action = "next"
    if action in ("back", "prev", "previoustrack"):
        action = "previous"
    if action in ("volup", "louder"):
        action = "volumeup"
    if action in ("voldown", "quieter"):
        action = "volumedown"

    if action not in MEDIA_ACTIONS:
        return {"success": False, "error": f"unknown media action '{action}'"}

    if not MEDIA_KEY_PATH.exists():
        return {"success": False, "error": "media_key helper is not built"}

    try:
        result = subprocess.run([str(MEDIA_KEY_PATH), action],
                                capture_output=True, text=True, timeout=10)
    except (subprocess.SubprocessError, OSError) as e:
        return {"success": False, "error": str(e)}

    try:
        return {**json.loads(result.stdout.strip()), "success": True}
    except (json.JSONDecodeError, ValueError):
        return {"success": result.returncode == 0, "action": action}


def set_volume(level: int) -> dict:
    """Set output volume 0-100. `set volume` is a system-level StandardAdditions
    command, so unlike scripting another app it needs no Automation grant."""
    try:
        level = max(0, min(100, int(level)))
    except (TypeError, ValueError):
        return {"success": False, "error": "level must be a number 0-100"}

    script = "on run argv\n  set volume output volume (item 1 of argv as integer)\nend run"
    try:
        result = subprocess.run(["osascript", "-e", script, "--", str(level)],
                                capture_output=True, text=True, timeout=10)
    except (subprocess.SubprocessError, OSError) as e:
        return {"success": False, "error": str(e)}

    if result.returncode != 0:
        return {"success": False, "error": (result.stderr or "").strip()}
    return {"success": True, "volume": level}


_NOW_PLAYING_SCRIPT = """
on run
    set out to ""
    tell application "System Events"
        set spotifyRunning to (exists (processes where name is "Spotify"))
        set musicRunning to (exists (processes where name is "Music"))
    end tell
    if spotifyRunning then
        tell application "Spotify"
            if player state is playing then
                set out to "Spotify: " & name of current track & " - " & artist of current track
            end if
        end tell
    end if
    if out is "" and musicRunning then
        tell application "Music"
            if player state is playing then
                set out to "Music: " & name of current track & " - " & artist of current track
            end if
        end tell
    end if
    if out is "" then set out to "Nothing is playing."
    return out
end run
"""


def now_playing() -> dict:
    """Best-effort current track.

    This one genuinely needs Apple Events, which the daemon may not have been
    granted, so a failure here is reported honestly rather than dressed up as
    "nothing playing" -- the two mean very different things to the user.
    """
    try:
        result = subprocess.run(["osascript", "-e", _NOW_PLAYING_SCRIPT],
                                capture_output=True, text=True, timeout=20)
    except subprocess.TimeoutExpired:
        return {"success": False,
                "error": "Timed out asking the music app -- macOS is likely waiting on an "
                         "Automation permission prompt for Mira."}
    except (subprocess.SubprocessError, OSError) as e:
        return {"success": False, "error": str(e)}

    if result.returncode != 0:
        err = (result.stderr or "").strip()
        if "-1743" in err or "not authorized" in err.lower():
            return {"success": False,
                    "error": "Mira needs Automation permission for Music/Spotify to read the "
                             "current track (System Settings > Privacy & Security > Automation)."}
        return {"success": False, "error": err or "could not read now playing"}

    return {"success": True, "now_playing": result.stdout.strip()}


_PLAYING_CHECK_SCRIPT = """
on run
    tell application "System Events"
        set spotifyRunning to (exists (processes where name is "Spotify"))
        set musicRunning to (exists (processes where name is "Music"))
    end tell
    if spotifyRunning then
        tell application "Spotify"
            if player state is playing then return "yes"
        end tell
    end if
    if musicRunning then
        tell application "Music"
            if player state is playing then return "yes"
        end tell
    end if
    return "no"
end run
"""


def is_music_playing() -> bool:
    """Best-effort, same Automation-permission caveat as now_playing() above --
    a denied/ungranted prompt just means this can't tell, so it reports False
    (don't duck) rather than raising. Used to decide whether to duck system
    volume before Mira speaks (see duck_for_speech below); a false negative
    here just means a reply plays without ducking, not that it fails."""
    try:
        result = subprocess.run(["osascript", "-e", _PLAYING_CHECK_SCRIPT],
                                capture_output=True, text=True, timeout=10)
    except (subprocess.SubprocessError, OSError):
        return False
    return result.returncode == 0 and result.stdout.strip() == "yes"


def get_volume() -> int:
    """Current system output volume 0-100, or -1 if it couldn't be read."""
    script = "output volume of (get volume settings)"
    try:
        result = subprocess.run(["osascript", "-e", script],
                                capture_output=True, text=True, timeout=10)
        return int(result.stdout.strip())
    except (subprocess.SubprocessError, OSError, ValueError):
        return -1


# How far to duck, as a fraction of the current volume -- not a fixed level,
# so someone who normally listens quiet doesn't get ducked to silence while
# someone who listens loud still gets a real, noticeable dip.
DUCK_FACTOR = 0.35


def duck_for_speech():
    """If music is audibly playing, lowers system volume before Mira speaks
    and returns what to hand back to restore_volume() afterward. Returns None
    when there's nothing to duck (no music, or volume couldn't be read) --
    callers should treat None as "nothing to restore" and skip the restore
    step entirely rather than calling set_volume with it."""
    if not is_music_playing():
        return None
    current = get_volume()
    if current < 0:
        return None
    ducked = max(5, round(current * DUCK_FACTOR))
    if set_volume(ducked).get("success"):
        return current
    return None


def restore_volume(previous):
    if previous is None:
        return
    set_volume(previous)


def list_running_apps() -> dict:
    """Visible apps the user could be asked about, via LaunchServices only."""
    script = ('tell application "System Events" to get name of every process '
              'whose background only is false')
    try:
        result = subprocess.run(["osascript", "-e", script],
                                capture_output=True, text=True, timeout=15)
    except (subprocess.SubprocessError, OSError) as e:
        return {"success": False, "error": str(e), "apps": []}
    if result.returncode != 0:
        return {"success": False, "error": (result.stderr or "").strip(), "apps": []}
    apps = [a.strip() for a in result.stdout.split(",") if a.strip()]
    return {"success": True, "apps": apps}
