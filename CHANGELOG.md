# Changelog

Entries are grouped by date; releases from v1.1.0 onward are also tagged in
git and published as GitHub Releases.

## v1.1.0 — 2026-09-23

### Added
- **Liquid Glass UI** — the notch, Quick Capture, the result pill, and the
  main workspace window were redesigned around real translucent materials:
  backdrop blur + saturation, a specular highlight along each surface's top
  edge, and rounder, more continuous corner radii, replacing the earlier flat
  vibrancy look. The holographic brand mark is kept as a small accent glyph
  rather than the base look.
- **OCR permission handling** — Screen Recording is checked before an OCR
  capture runs instead of failing silently; a denied/stale grant now shows a
  clear message in the notch with a direct link to System Settings, and
  Settings > Assistant has a live status row for it. (Screen Recording, like
  Accessibility, only takes effect after Mira is fully quit and reopened —
  not just refocused.)
- **Music ducking** — if Spotify or Music is playing, Mira lowers system
  volume proportionally before speaking and restores it afterward. Best
  effort: needs Automation permission to detect playback state, same as the
  existing "now playing" feature.
- **Personalized greetings** — unprompted speech (the morning digest,
  proactive alerts, the first line of a fresh conversation) now opens with
  the owner's name or a natural honorific instead of a generic "Hey there".
- **`list_calcom_event_types` tool** — lets booking always resolve to a real
  event type on the account instead of a model-invented name that
  `create_calcom_booking` would then reject.
- A close button on the notch's Ask panel (Control+S) — previously the only
  way out was Escape.

### Changed
- Global shortcuts remapped: OCR capture is now **Control+Q**, Quick Capture
  is **Control+D**, and "talk to Mira" is **Control+A** (notch Ask stays
  **Control+S**). Tray menu labels updated to match.
- Mail digest and inbox summaries now fetch each message's real body before
  summarizing, instead of summarizing Gmail's short auto-generated snippet —
  which for a short email was effectively the whole message, so the "summary"
  read as a rephrase rather than an actual distillation.
- Tab-to-accept (predictive typing) no longer swallows Tab when a modifier is
  held — Cmd+Tab, Ctrl+Tab, Option+Tab and Shift+Tab now reach the OS/app
  normally instead of being eaten by suggestion-accept.
- Cal.com bookings resolve a bare local time (e.g. "1pm") using this
  machine's real UTC offset and IANA timezone, instead of asking the model to
  convert to UTC itself — that conversion was happening wrong often enough to
  book outside business hours and report false "not available" slots.
- Tavily web search moved back to last resort in the provider order. It had
  been running first whenever a key was configured, which defeated the
  free-source-first design and billed a configured key on every search
  instead of only the rare one DuckDuckGo/Wikipedia couldn't answer.

## 2026-09-16

### Added
- **Persistent notch UI** — a chrome-less panel anchored under the menu bar,
  invisible at rest, that reveals itself on copy, ⌘⇧O (circle-and-ask over
  OCR), a mouse hover, or Mira listening/speaking. Replaces the old
  cursor-anchored pill for these flows.
- **Listening/speaking animation in the notch** — an animated waveform shows
  when Mira is actively listening (wake word or manual trigger) or speaking a
  reply, with the spoken/heard text as a caption. Covers wake-word replies
  and spoken proactive alerts.
- **Google Meet scheduling** — `create_calendar_event` can attach a real Meet
  link (`add_meet`), and now takes `attendees`/`location` too. Guests are
  actually notified on creation (Calendar's API defaults to silent otherwise).
- **Gmail drafts** — a `draft_email` tool creates a Gmail draft for the user
  to review and send; Mira still never sends mail on her own.
- **Cal.com integration** — connect a personal API key in Settings, then list,
  book, cancel, and reschedule on your Cal.com page by voice or chat. New
  bookings trigger a proactive alert automatically.
- **Auto-record scheduled meetings** — opt-in toggle in the Meetings section:
  when a calendar event with a Meet link starts, Mira starts recording (mic +
  system audio) on her own and transcribes it when the event ends.
- **Morning mail digest** — an alternative to per-email alerts: one summary of
  the last 24h of mail at a configurable time (default 10am), with action
  items called out, delivered through the existing screen-lock-aware channel.
- **Hindi support** — auto-detects Devanagari script for voice replies and
  chat, with a separate Hindi TTS voice setting.
- **Screen-lock-aware proactive delivery** — alerts go to Telegram while the
  screen is locked and are spoken aloud while it's unlocked (or pinned to one
  or the other), instead of always doing the same thing regardless of whether
  anyone's there to hear it.
- **VS Code workspace config** — interpreter path, debug configs for the
  daemon and Electron (together or separately), and a task to run
  `reset_permissions.sh` from the command palette.
- **`scripts/reset_permissions.sh`** — resets the macOS TCC grants Mira
  depends on (Accessibility, Screen Recording, Microphone, Speech Recognition,
  Automation) so they can be cleanly re-granted after a rebuild changes
  Mira.app's code signature.

### Fixed
- Predictive typing: accepting a spell-corrected suggestion with Tab could
  leave a leftover fragment of the original (wrong) word in place.
- Predictive typing: ghost-text was low-contrast/hard to read, and noticeably
  slower than actual typing speed.
- Predictive typing: Hinglish words the user had typed before were being
  flagged as misspelled instead of learned as real vocabulary.
- The floating pet got stuck wherever the last copy happened, instead of
  returning to its previous position once the pill/notch closed.
- The notch was positioned off-center (using the Dock-narrowed work area
  instead of the true screen width to center it) and measured a phantom
  ~113px of hidden content into its idle height.
- The notch didn't auto-return to idle after showing content, unlike the old
  pill's 6s auto-dismiss — it just stayed expanded until manually closed.
- Calendar events created with a string (not array) `attendees` value were
  silently mangled into one garbage "attendee" per character, which the
  Calendar API rejected outright.

### Removed
- The standalone `hermes` CLI agent and its `~/.hermes` directory (unused;
  distinct from any Ollama-hosted model).

## Earlier

See `git log` for everything before this point — a formal changelog starts
here.
