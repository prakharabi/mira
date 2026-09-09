#!/bin/bash
# Removes the Mira daemon LaunchAgent. Leaves your data alone -- memory, chat
# history, settings and captures all stay in daemon/ so a reinstall picks up
# where you left off. Delete that directory yourself if you want a clean slate.

set -euo pipefail

LABEL="com.mira.daemon"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"

launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
rm -f "$PLIST"

echo "Removed $LABEL."
echo "Your data is untouched in daemon/ (memory_store, chat_history, settings.json)."
