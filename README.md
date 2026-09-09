# Mira

A local-first, OS-integrated AI assistant for macOS.

Mira runs on your own machine, keeps your data there by default, and can
actually *do* things on it — create reminders, open apps, control playback,
search the web, capture notes, transcribe meetings, read your mail and
calendar — rather than only talking about them.

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

**Predictive typing, system-wide.** Inline suggestions in any app, accepted with
Tab, plus in-word completion and mid-word typo correction. It learns from what
you actually type, which is also what keeps it fast.

**Captures.** ⌘⇧D opens a Spotlight-style Quick Capture that files a thought
into the Dump Box, where it becomes a summary and action items. ⌘⇧O runs OCR on
any region of the screen.

**Meetings.** Records mic + system audio, transcribes (locally or via cloud),
and generates minutes.

**Proactive.** Optionally speaks first — a meeting starting soon, mail addressed
directly to you — delivered over Telegram so it reaches you with the lid shut.

## Architecture

```
Electron (UI, OS integration)  ⇄  FastAPI daemon (the brain)  ⇄  Ollama / cloud
        │                                    │
        └── Swift helpers                    └── memory, tools, web, agent
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
# daemon
cd daemon
python3 -m venv venv && ./venv/bin/pip install -r requirements.txt
cp .env.example .env          # optional: add GROQ_API_KEY for the cloud model

# local models
ollama pull qwen3:1.7b

# app
cd ../electron && npm install
node scripts/build_app.js     # produces dist/Mira.app
open dist/Mira.app
```

The daemon runs on port 11200 as a LaunchAgent (`com.mira.daemon`); Ollama uses
its own 11434.

### Permissions

macOS will ask for these the first time each is used. All are optional except
Accessibility, which predictive typing depends on:

| Permission | Needed for |
|---|---|
| Accessibility | predictive typing, text insertion |
| Microphone | dictation, wake word, meeting recording |
| Screen Recording | OCR, meeting system audio |
| Automation (Reminders) | creating reminders |

### Optional integrations

All are off until you configure them, and **Mira ships no shared API keys** —
you supply your own, so your data only moves between your machine and your own
accounts:

- **Cloud model** — any OpenAI-compatible endpoint (Groq by default)
- **Google** — your own OAuth client (Desktop app type) for Gmail/Calendar/Drive
- **Telegram** — your own bot from @BotFather
- **n8n** — your own webhook URLs

Web search needs no key at all.

## Privacy

Chat history, memory, settings, recordings, transcripts and captures all stay
on disk in `daemon/` and are gitignored. The local model path never leaves the
machine. The cloud model is used only when routing selects it, and memory
extraction always runs locally.

## Development

```bash
cd electron && npx electron .            # run unpackaged
MIRA_DEBUG_PREDICTIVE=1 npx electron .   # verbose predictive-typing logging
node scripts/build_app.js                # rebuild Mira.app
python3 scripts/generate_icon.py         # regenerate the app icon
```

`workspace.html` carries one large inline script with no build step, so nothing
catches a syntax error before runtime — and a syntax error there takes down the
entire renderer, which presents as a window stuck on "Checking…". After editing
it, extract the script and run `node --check` on it.

## License

Not yet chosen — to be settled before public release.
