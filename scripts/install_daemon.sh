#!/bin/bash
# Installs the Mira daemon as a per-user LaunchAgent.
#
# The plist has to carry absolute paths, and they differ per machine and per
# checkout location -- so it is generated here from wherever this repo actually
# lives rather than committed with someone else's home directory baked in.
#
# Safe to re-run: it reloads an existing agent rather than erroring.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DAEMON_DIR="$REPO_ROOT/daemon"
VENV_UVICORN="$DAEMON_DIR/venv/bin/uvicorn"
LABEL="com.mira.daemon"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
PORT=11200

if [ ! -x "$VENV_UVICORN" ]; then
  echo "error: $VENV_UVICORN not found."
  echo "Create the virtualenv first:"
  echo "  cd '$DAEMON_DIR' && python3 -m venv venv && ./venv/bin/pip install -r requirements.txt"
  exit 1
fi

mkdir -p "$HOME/Library/LaunchAgents"

cat > "$PLIST" <<PLIST_EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>$LABEL</string>
    <key>ProgramArguments</key>
    <array>
        <string>$VENV_UVICORN</string>
        <string>main:app</string>
        <string>--port</string>
        <string>$PORT</string>
    </array>
    <key>WorkingDirectory</key>
    <string>$DAEMON_DIR</string>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <true/>
    <key>StandardOutPath</key>
    <string>$DAEMON_DIR/mira.log</string>
    <key>StandardErrorPath</key>
    <string>$DAEMON_DIR/mira_error.log</string>
</dict>
</plist>
PLIST_EOF

# bootout first so a re-run picks up an edited plist instead of silently
# keeping the old one loaded
launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
launchctl bootstrap "gui/$(id -u)" "$PLIST"

echo "Installed $LABEL"
echo "Waiting for the daemon to come up on port $PORT..."
for _ in $(seq 1 30); do
  if curl -fsS -m 2 "http://127.0.0.1:$PORT/health" >/dev/null 2>&1; then
    echo "Daemon is running."
    exit 0
  fi
  sleep 1
done

echo "Daemon did not respond in 30s. Check $DAEMON_DIR/mira_error.log"
exit 1
