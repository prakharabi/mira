# Contributing to Mira

Thanks for taking a look. Mira is a personal assistant that runs on your own
machine, so almost every change touches either the OS or someone's private
data — the notes below exist to keep both safe.

## Getting set up

See [README.md](README.md) for the full setup. In short:

```bash
cd daemon && python3 -m venv venv
./venv/bin/pip install --upgrade pip && ./venv/bin/pip install -r requirements.txt
cp .env.example .env
../scripts/install_daemon.sh
cd ../electron && npm install && npx electron .
```

`npx electron .` runs the app unpackaged, which is what you want while
developing. `node scripts/build_app.js` produces `dist/Mira.app` — only needed
to test Launch at Login or anything else that behaves differently in a real
bundle.

## Where things live

| Path | What it is |
|---|---|
| `daemon/` | FastAPI service on :11200 — models, memory, tools, integrations |
| `electron/src/` | The app: windows, global shortcuts, OS integration |
| `electron/*.swift` | Small single-purpose helpers (accessibility, OCR, audio, media keys) |
| `scripts/` | LaunchAgent install/uninstall |

Two conventions worth knowing before you add a feature:

**New capabilities go in `daemon/tools.py`.** Declare it once there and it is
immediately available in chat, on Telegram and by voice, and it shows up in
Mira's own description of what she can do. Adding it to only one surface is
almost always a mistake.

**Anything needing Apple Events goes through `daemon/actions.py`.** The daemon
is a headless LaunchAgent, and macOS will not show it an Automation consent
prompt — `osascript` just hangs on a dialog nobody can see. So the daemon
queues the work and Electron, which *can* get consent, performs it.

## Rebuilding the Swift helpers

They are committed as binaries so a fresh clone runs without Xcode. If you
change one:

```bash
cd electron
swiftc -O ax_helper.swift -o AXHelper.app/Contents/MacOS/ax_helper
codesign --force --sign - AXHelper.app
```

Rebuilding a helper changes its code signature, which macOS treats as a new
identity — you will have to re-grant its permission in System Settings.

`speech_helper` is the one exception, and needs its Info.plist embedded
directly into the binary at link time rather than living only in the
surrounding .app bundle:

```bash
cd electron
cat > /tmp/speech_helper_info.plist <<'PLIST'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>CFBundleIdentifier</key>
    <string>com.mira.speechhelper</string>
    <key>CFBundleName</key>
    <string>SpeechHelper</string>
    <key>NSSpeechRecognitionUsageDescription</key>
    <string>Mira uses speech recognition to turn your dictation into text on this Mac.</string>
    <key>NSMicrophoneUsageDescription</key>
    <string>Mira uses the microphone for voice dictation.</string>
</dict>
</plist>
PLIST
swiftc -O speech_helper.swift -o SpeechHelper.app/Contents/MacOS/speech_helper \
  -Xlinker -sectcreate -Xlinker __TEXT -Xlinker __info_plist \
  -Xlinker /tmp/speech_helper_info.plist
codesign --force --sign - SpeechHelper.app
```

Why: macOS aborts a process outright (`SIGABRT`) if it can't find
`NSSpeechRecognitionUsageDescription` for whichever process TCC considers
*responsible* — and for a helper Mira launches, that's Mira.app's own
Info.plist (see `build_app.js`), not the helper's. The embedded plist here is
what lets `speech_helper` also run correctly stand-alone, e.g. for the
`check`/`locales` commands during development.

## Things to be careful about

**Never commit personal data.** `daemon/memory_store/`, `chat_history/`,
`settings.json`, `dumpbox/` and `.env` are gitignored because they hold real
memories, conversations and API keys. Check `git status` before committing.

**Mira ships no shared API keys.** Every integration uses credentials the user
supplies. Don't add a default key, even a free-tier one.

**`workspace.html` has one large inline script and no build step**, so nothing
catches a syntax error before runtime — and a syntax error there takes down the
whole renderer, which shows up as a window stuck on "Checking…". After editing
it:

```bash
cd electron/src && python3 -c "
import re; print(re.findall(r'<script(?![^>]*src=)[^>]*>(.*?)</script>', open('workspace.html').read(), re.S)[0])
" > /tmp/ws.js && node --check /tmp/ws.js
```

**Checking a view without clicking through to it.** `MIRA_OPEN_VIEW` opens the
workspace straight at a named view on launch, which is how to look at one
without driving synthetic clicks:

```bash
MIRA_OPEN_VIEW=automations electron/dist/Mira.app/Contents/MacOS/Mira
```

`MIRA_SCROLL_TO` takes a CSS selector and scrolls it into view; `MIRA_CLICK`
clicks one and logs whether it was found, hidden, or clicked. Between them a
control's whole handler chain can be exercised the way a user would, without
driving synthetic input across the screen:

```bash
MIRA_OPEN_VIEW=automations MIRA_CLICK='#n8n-open-btn' \
  electron/dist/Mira.app/Contents/MacOS/Mira
```

`MIRA_EVAL` runs a snippet in the renderer and logs the result, which is how to
exercise a renderer-side code path in place:

```bash
MIRA_OPEN_VIEW=chat MIRA_EVAL='(async()=>await transcribeBlob(b))()' \
  electron/dist/Mira.app/Contents/MacOS/Mira
```

**Test against a real permission state.** Predictive typing, OCR and audio all
fail in ways that look like bugs when a TCC grant is missing. Settings →
Assistant shows predictive typing's live status and has a Restart button.

## Pull requests

- One change per PR, with a description of what broke or was missing.
- Say what you actually tested. "Verified on macOS 15, Apple Silicon" is worth
  more than a green checkmark.
- Match the surrounding style. Comments here explain *why* something is the way
  it is — especially where macOS forced the design — rather than restating the
  code.

## Reporting bugs

Include your macOS version, whether you are on Apple Silicon or Intel, which
model you were using (local or cloud), and the relevant part of
`daemon/mira_error.log` or `electron/electron.log`.

If it involves predictive typing, run the app with verbose logging first:

```bash
cd electron && MIRA_DEBUG_PREDICTIVE=1 npx electron .
```
