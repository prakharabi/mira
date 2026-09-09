#!/bin/bash
# Builds Mira.dmg -- the drag-to-Applications installer.
#
# The app inside is signed ad-hoc, not with an Apple Developer ID. That is a
# deliberate choice, not an oversight: a Developer ID costs $99/year and this
# project ships without one. The practical consequence is that macOS quarantines
# the app when it is downloaded, and Gatekeeper refuses to launch it until the
# user explicitly allows it. See the "Gatekeeper" section of the README for the
# two ways out.
#
# Building from source has no such problem -- locally built apps are never
# quarantined -- so `node electron/scripts/build_app.js` stays the recommended
# path for anyone comfortable with a terminal.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
APP="$REPO_ROOT/electron/dist/Mira.app"
STAGING="$REPO_ROOT/electron/dist/dmg-staging"
DMG="$REPO_ROOT/electron/dist/Mira.dmg"
VOL_NAME="Mira"

if [ ! -d "$APP" ]; then
  echo "error: $APP not found."
  echo "Build it first:  cd electron && node scripts/build_app.js"
  exit 1
fi

echo "[dmg] staging..."
rm -rf "$STAGING" "$DMG"
mkdir -p "$STAGING"
# ditto, not cp -R: the app contains framework bundles with relative symlinks
# that cp rewrites, which breaks the code signature.
/usr/bin/ditto "$APP" "$STAGING/Mira.app"

# The /Applications symlink is what makes the window a drag-and-drop installer
# rather than just a folder holding an app.
ln -s /Applications "$STAGING/Applications"

# A short read-me travels inside the image, because the Gatekeeper prompt
# arrives with no explanation and this is the moment the user needs one.
cat > "$STAGING/READ ME FIRST.txt" <<'TXT'
Installing Mira
===============

1. Drag Mira onto the Applications folder shown here.

2. The first launch will be blocked. This is expected.

   Mira is open source and signed ad-hoc rather than with a paid Apple
   Developer ID, so macOS cannot verify the developer and quarantines it.
   Nothing is wrong with the download.

   To allow it:
     - Open Mira from Applications. macOS will refuse.
     - Go to System Settings > Privacy & Security, scroll to Security,
       and click "Open Anyway" next to the message about Mira.
     - Open Mira again and confirm.

   (On macOS 15 and later, right-clicking and choosing Open no longer
   works for this -- you have to use Privacy & Security.)

   Terminal alternative, if you prefer:
     xattr -d com.apple.quarantine /Applications/Mira.app

3. Mira also needs a running daemon and Ollama. Follow the setup steps in
   the README:  https://github.com/<your-username>/mira

If you would rather avoid the Gatekeeper step entirely, build from source --
locally built apps are never quarantined.
TXT

echo "[dmg] creating image..."
/usr/bin/hdiutil create \
  -volname "$VOL_NAME" \
  -srcfolder "$STAGING" \
  -ov -format UDZO \
  "$DMG" >/dev/null

rm -rf "$STAGING"

SIZE=$(du -h "$DMG" | cut -f1 | tr -d ' ')
echo "[dmg] done: $DMG ($SIZE)"
echo
echo "Note: this image is unsigned and un-notarized. Anyone who downloads it"
echo "will hit Gatekeeper on first launch and must allow it in"
echo "System Settings > Privacy & Security."
