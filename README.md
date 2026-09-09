# Mira — a CEO's personal AI assistant for Mac

Local-first, OS-integrated, and built to actually run your day rather than
chat about it.

Mira runs on your own machine, keeps your data there by default, and can
actually *do* things on it — create reminders, open apps, control playback,
search the web, capture notes, transcribe meetings, read your mail and
calendar — rather than only talking about them.

Built for the kind of day where the same commitment gets raised in three
different places and none of them talk to each other: mention something on
Telegram, ask about it out loud, and close it out in the app.

## What it does

**Assistant, everywhere.** Chat in the app, message it on Telegram, or say
"Hey Mira" out loud. All three route through the same agent, so a capability
added once works everywhere.

**Knows what it can do.** Capabilities are declared once as tools the model can
call, and summarized into its system prompt. It doesn't claim it can't create a
reminder while sitting inside an app that creates reminders.

**Remembers.** A long-term memory of who you are, your products, people and
preferences — seeded by you, and extended automatically from conversation. You
can read, pin, edit and delete every fact in the Memory view.

**Tracks open loops.** Say in passing that you still owe someone a deck —
in the app, on Telegram, or out loud — and it stays on a list you can see,
gets brought up when relevant, and nudges you when it comes due or goes quiet.
Separate from Reminders on purpose: these are commitments, not alarms.

**Predictive typing, system-wide.** Inline suggestions in any app, accepted with
Tab, plus in-word completion and mid-word typo correction. It learns from what
you actually type, which is also what keeps it fast.

**Captures.** ⌘⇧D opens a Spotlight-style Quick Capture that files a thought
into the Dump Box, where it becomes a summary and action items. ⌘⇧O runs OCR on
any region of the screen.

**Meetings.** Records mic + system audio, transcribes (locally or via cloud),
and generates minutes.

**Runs your workflows.** Fires named n8n webhooks by voice or chat, matched
against how you describe them. The Automations view shows whether n8n is
actually running, and starts it — no Docker required.

**Proactive.** Optionally speaks first — a meeting starting soon, mail addressed
directly to you — delivered over Telegram so it reaches you with the lid shut.

## Architecture

```
Electron (UI, OS integration)  ⇄  FastAPI daemon (the brain)  ⇄  Ollama / cloud
        │                                    │
        └── Swift helpers                    └── memory, tasks, tools, web, agent
            ax_helper    accessibility / text insertion
            tab_tap      global Tab interception
            ocr_helper   Vision-framework OCR
            media_key    system media keys
            mic_helper / system_audio_helper
```

**Why the split.** The daemon is a headless LaunchAgent, and macOS won't grant
one an Automation (Apple Events) consent prompt — its `osascript` call just
hangs on a dialog nobody sees. So anything needing Apple Events (Reminders) is
queued by the daemon and executed by Electron, which as a foreground app *can*
get consent. Same reason system-audio capture lives in Electron.

Playback control deliberately uses **media keys**, not AppleScript — no per-app
Automation grant needed, and it controls whatever is actually playing,
including a browser.

## Setup

Requirements: macOS (Apple Silicon tested), Python 3.11+, Node 18+,
[Ollama](https://ollama.com), and `ffmpeg` (`brew install ffmpeg`).

```bash
git clone https://github.com/<your-username>/mira.git && cd mira

# daemon
cd daemon
python3 -m venv venv
./venv/bin/pip install --upgrade pip     # stock macOS pip is too old to resolve these
./venv/bin/pip install -r requirements.txt
cp .env.example .env          # optional: add GROQ_API_KEY for the cloud model
cd ..
./scripts/install_daemon.sh   # generates the LaunchAgent and starts it

# local model
ollama pull qwen3:1.7b

# app
cd electron && npm install
node scripts/build_app.js     # produces dist/Mira.app
open dist/Mira.app
```

The daemon runs on port 11200 as a LaunchAgent (`com.mira.daemon`); Ollama uses
its own 11434. `./scripts/uninstall_daemon.sh` removes the agent and leaves
your data alone.

### Installing from the DMG instead

```bash
cd electron && node scripts/build_app.js
../scripts/make_dmg.sh          # produces electron/dist/Mira.dmg
```

Drag Mira onto Applications, then follow the Gatekeeper note below. You still
need the daemon and Ollama from the steps above — the DMG only carries the app.

### A note on Gatekeeper

Mira is signed ad-hoc, not with a paid Apple Developer ID. **Building it
yourself avoids this section entirely** — locally built apps are never
quarantined. But a copy *downloaded* from the internet is, and Gatekeeper will
refuse to launch it.

To allow it, open Mira once (macOS refuses), then go to **System Settings →
Privacy & Security**, scroll to Security, and click **Open Anyway**. On macOS 15
and later, right-clicking and choosing Open no longer works for this — Apple
removed that bypass, so Privacy & Security is the only GUI route.

Or from a terminal:

```bash
xattr -d com.apple.quarantine /Applications/Mira.app
```

None of this is a sign that anything is wrong with the download; it is what
macOS does with any app whose developer it cannot verify.

### Permissions

macOS will ask for these the first time each is used. All are optional except
Accessibility, which predictive typing depends on:

| Permission | Needed for |
|---|---|
| Accessibility | predictive typing, text insertion |
| Microphone | dictation, wake word, meeting recording |
| Screen Recording | OCR, meeting system audio |
| Automation (Reminders) | creating reminders |

**Rebuilding revokes permissions.** Builds are signed ad-hoc, so macOS keys
each grant to a code hash that changes every time you run `build_app.js`. After
a rebuild, predictive typing usually stops because `tab_tap` can no longer
create its event tap. Settings → Assistant shows this and links straight to the
Accessibility pane; re-grant, then press Restart. A real Developer ID signature
would make grants stick across builds.

### Optional integrations

All are off until you configure them, and **Mira ships no shared API keys** —
you supply your own, so your data only moves between your machine and your own
accounts:

- **Cloud model** — any OpenAI-compatible endpoint (Groq by default)
- **Google** — your own OAuth client for Gmail/Calendar/Drive ([setup guide](docs/google-setup.md))
- **Telegram** — your own bot from @BotFather
- **n8n** — your own webhook URLs; `./scripts/n8n.sh` runs one locally (no Docker needed)

Web search needs no key at all.

## Privacy

Chat history, memory, settings, recordings, transcripts and captures all stay
on disk in `daemon/` and are gitignored. The local model path never leaves the
machine. The cloud model is used only when routing selects it, and memory
extraction always runs locally.

## Development

```bash
cd electron && npm start                 # run unpackaged
MIRA_DEBUG_PREDICTIVE=1 npx electron .   # verbose predictive-typing logging
npm run build                            # rebuild Mira.app
npm run dmg                              # rebuild and package Mira.dmg
python3 scripts/generate_icon.py         # regenerate the app icon
python3 scripts/generate_tray_icon.py    # regenerate the menu bar icon
```

Settings → Assistant shows predictive typing's live status and can restart it,
which is usually faster than relaunching when tab_tap has exhausted its restart
budget after a permission change.

`workspace.html` carries one large inline script with no build step, so nothing
catches a syntax error before runtime — and a syntax error there takes down the
entire renderer, which presents as a window stuck on "Checking…". After editing
it, extract the script and run `node --check` on it.

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md). The short version: new capabilities go
in `daemon/tools.py` so they reach chat, Telegram and voice at once, and
anything needing Apple Events must be queued through `daemon/actions.py`.

## License

[MIT](LICENSE).
