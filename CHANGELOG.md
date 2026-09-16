# Changelog

Mira doesn't follow a formal release/version scheme yet, so entries are
grouped by date instead of a version number.

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
