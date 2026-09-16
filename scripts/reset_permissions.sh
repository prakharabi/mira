#!/bin/bash
# Resets the macOS TCC (privacy permission) grants Mira depends on, so they
# can be re-granted from a clean state.
#
# Why this is needed at all: every helper here (ax_helper/tab_tap for
# Accessibility, ocr_helper/system_audio_helper for Screen Recording,
# mic_helper for Microphone, speech_helper for Speech Recognition) is spawned
# as a plain child process of either Mira.app or the daemon's Python
# interpreter, not launched as its own standalone app -- so macOS checks the
# PARENT's TCC identity, not the helper's own bundle id. Concretely: granting
# "AXHelper" or "TabTap" in System Settings does nothing; the grant that
# actually matters is Mira.app's (com.mira.desktop). Ad-hoc code signing
# (no paid Apple Developer ID) makes this worse -- every `node
# scripts/build_app.js` gives Mira.app a new code hash, and macOS keys these
# grants to that hash, so a rebuild silently revokes all of them with no
# visible error. Predictive typing (Tab-to-accept) and OCR just quietly stop
# working until someone thinks to check here.
#
# This script only RESETS -- it can't grant anything back. macOS re-prompts
# for each permission the next time Mira actually tries to use it (or open
# the relevant System Settings pane yourself, see the links printed below).
# Safe to run any time; resetting a permission nobody granted is a no-op.
#
#   ./scripts/reset_permissions.sh            reset everything below
#   ./scripts/reset_permissions.sh Accessibility    reset just one service

set -uo pipefail  # not -e: tccutil exits non-zero for a service/bundle-id with nothing to reset, which isn't a failure here

MIRA_APP="com.mira.desktop"

# Bundle ids that exist only as .app wrappers around a plain binary Electron
# or the daemon spawns directly -- see the note above for why resetting these
# specifically does nothing useful. Included anyway, harmlessly, in case a
# future build ever launches one of them standalone instead.
HELPER_APPS=(com.mira.axhelper com.mira.tabtap com.mira.michelper com.mira.speechhelper)

SERVICES=(Accessibility ScreenCapture Microphone SpeechRecognition AppleEvents)

reset_one() {
  local service="$1"
  echo "== $service =="
  tccutil reset "$service" "$MIRA_APP" 2>&1 | sed 's/^/   /'
  for bundle in "${HELPER_APPS[@]}"; do
    tccutil reset "$service" "$bundle" >/dev/null 2>&1
  done
}

if [ $# -eq 1 ]; then
  reset_one "$1"
else
  for service in "${SERVICES[@]}"; do
    reset_one "$service"
  done
fi

cat <<'EOF'

Done. Each permission above will be asked for again the next time Mira
actually needs it -- or grant it ahead of time from System Settings:

  Accessibility:      x-apple.systempreferences:com.apple.preference.security?Privacy_Accessibility
  Screen Recording:   x-apple.systempreferences:com.apple.preference.security?Privacy_ScreenCapture
  Microphone:         x-apple.systempreferences:com.apple.preference.security?Privacy_Microphone
  Speech Recognition: x-apple.systempreferences:com.apple.preference.security?Privacy_SpeechRecognition
  Automation:         x-apple.systempreferences:com.apple.preference.security?Privacy_Automation

Grant these to "Mira" (com.mira.desktop) specifically -- not AXHelper,
TabTap, MicHelper or SpeechHelper, which do nothing on their own (see the
comment at the top of this script for why).
EOF
