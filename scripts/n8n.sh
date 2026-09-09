#!/bin/bash
# Runs a local n8n for Mira's automations. No Docker required.
#
# n8n is not bundled with Mira and never will be -- it is a workflow engine in
# its own right, and Mira's job is only to fire named webhooks at whatever n8n
# you point it at (local, self-hosted, or n8n Cloud). This script exists so
# that "get one running locally" is a single command instead of a support
# thread.
#
#   ./scripts/n8n.sh              start it (installs n8n if needed)
#   ./scripts/n8n.sh install      install/repair only, don't start
#   ./scripts/n8n.sh autostart    run in the background, and at login
#   ./scripts/n8n.sh stop         stop it
#   ./scripts/n8n.sh docker       run via Docker instead
#
# Requires Node 24+ (n8n 2.x declares `engines: node >=24`) and the Xcode
# command line tools, which the sqlite3 build below needs.

set -euo pipefail

PORT=5678
LABEL="com.mira.n8n"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"

# Find Node even when PATH is bare. This script is also invoked by the Mira
# daemon, which launchd starts with PATH=/usr/bin:/bin:/usr/sbin:/sbin -- so a
# Node installed by nvm or Homebrew is invisible, and the script would exit
# with "npm not found" while the UI still showed a spinner. Anything already
# on PATH keeps priority.
hydrate_path() {
  local candidates=()
  # newest nvm version first
  if [ -d "$HOME/.nvm/versions/node" ]; then
    while IFS= read -r d; do candidates+=("$d/bin"); done < <(
      find "$HOME/.nvm/versions/node" -maxdepth 1 -mindepth 1 -type d 2>/dev/null | sort -Vr
    )
  fi
  candidates+=(/opt/homebrew/bin /usr/local/bin)
  for d in "${candidates[@]}"; do
    [ -x "$d/node" ] && case ":$PATH:" in *":$d:"*) ;; *) PATH="$PATH:$d" ;; esac
  done
  export PATH
}
hydrate_path

# Bind to loopback, not n8n's default 0.0.0.0, which would put the editor on
# the local network -- including before an owner account exists.
export N8N_LISTEN_ADDRESS=127.0.0.1
# n8n marks its auth cookie Secure, and browsers drop a Secure cookie over
# plain HTTP. Chrome and Firefox special-case localhost; Safari does not, so
# sign-in simply fails there. Safe only because of the bind above -- the cookie
# never crosses a network. Put TLS in front of it if you ever expose this.
export N8N_SECURE_COOKIE=false

n8n_root() {
  local bin; bin="$(command -v n8n 2>/dev/null)" || return 1
  local real; real="$(readlink "$bin" 2>/dev/null || echo "$bin")"
  case "$real" in
    /*) ;;
    *) real="$(cd "$(dirname "$bin")" && cd "$(dirname "$real")" && pwd)/$(basename "$real")" ;;
  esac
  dirname "$(dirname "$real")"
}

# n8n stores its data in SQLite through a native addon. sqlite3 ships no
# prebuilt binary for current Node, so npm installs the source and leaves it
# uncompiled -- and n8n then reports "SQLite package has not been found
# installed", which sends you hunting for a missing package that is right
# there. Worse, `npm install` inside n8n's own tree dies on an unrelated
# peer-dependency conflict before it ever reaches the compile, so the fix is to
# invoke node-gyp directly.
#
# It must be a CURRENT node-gyp: the one vendored next to sqlite3 needs
# Python's distutils, which was removed in Python 3.12.
ensure_sqlite() {
  local root; root="$(n8n_root)" || return 1
  local mod="$root/node_modules/sqlite3"

  if [ ! -d "$mod" ]; then
    echo "  sqlite3 missing; fetching..."
    (cd "$root" && npm install sqlite3 --no-save --legacy-peer-deps >/dev/null 2>&1) || true
  fi
  [ -d "$mod" ] || { echo "  could not fetch sqlite3"; return 1; }

  if (cd "$root" && node -e "require('sqlite3')" >/dev/null 2>&1); then
    return 0
  fi

  echo "  building the sqlite3 native addon (a few minutes, once)..."
  (cd "$mod" && npx --yes node-gyp@latest rebuild >/tmp/mira-sqlite3-build.log 2>&1) || {
    echo "  build failed. Log: /tmp/mira-sqlite3-build.log"
    echo "  Most often the Xcode command line tools: xcode-select --install"
    return 1
  }
  (cd "$root" && node -e "require('sqlite3')" >/dev/null 2>&1)
}

ensure_installed() {
  if ! command -v n8n >/dev/null 2>&1; then
    command -v npm >/dev/null 2>&1 || { echo "npm not found. Install Node 24+ first."; exit 1; }
    local major; major="$(node -p 'process.versions.node.split(".")[0]' 2>/dev/null || echo 0)"
    [ "$major" -ge 24 ] || { echo "n8n 2.x needs Node 24+; you have $(node -v)."; exit 1; }
    echo "Installing n8n (several minutes)..."
    npm install -g n8n
  fi
  ensure_sqlite || { echo "n8n cannot start without a working sqlite3."; exit 1; }
}

wait_for_n8n() {
  printf "Waiting for n8n"
  for _ in $(seq 1 90); do
    if curl -fsS -m 2 "http://localhost:$PORT/" >/dev/null 2>&1; then
      echo " -- ready."
      return 0
    fi
    printf "."
    sleep 2
  done
  echo
  return 1
}

next_steps() {
  cat <<EOF

Open http://localhost:$PORT and create the owner account.

To wire a workflow into Mira:
  1. Add a Webhook trigger node and copy its Production URL.
  2. Mira -> Automations -> add it with a name and a description. The
     description is what voice and chat match against, so write it the way you
     would ask for it ("send the daily standup summary").
  3. Mira POSTs JSON including source and triggered_at.

n8n must be running to receive a webhook. './scripts/n8n.sh autostart' keeps it
running in the background and starts it at login.
EOF
}

case "${1:-start}" in
  install)
    ensure_installed
    echo "n8n is installed and its database driver works."
    ;;

  stop)
    launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null && echo "Stopped background n8n." || true
    pkill -f "n8n start" 2>/dev/null && echo "Stopped foreground n8n." || true
    docker stop mira-n8n >/dev/null 2>&1 && echo "Stopped Docker n8n." || true
    exit 0
    ;;

  autostart)
    ensure_installed
    mkdir -p "$HOME/Library/LaunchAgents" "$HOME/.n8n"
    # A LaunchAgent rather than Docker. n8n itself costs about the same either
    # way (~1GB resident, measured); what this avoids is Docker Desktop's own
    # ~600MB of daemon and VM on top, plus the requirement that a user install
    # Docker at all to use an optional integration.
    cat > "$PLIST" <<PLIST_EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>$LABEL</string>
    <key>ProgramArguments</key>
    <array>
        <string>$(command -v n8n)</string>
        <string>start</string>
    </array>
    <key>EnvironmentVariables</key>
    <dict>
        <key>PATH</key>
        <string>$(dirname "$(command -v node)"):/usr/bin:/bin:/usr/sbin:/sbin</string>
        <key>N8N_LISTEN_ADDRESS</key>
        <string>127.0.0.1</string>
        <key>N8N_SECURE_COOKIE</key>
        <string>false</string>
    </dict>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <true/>
    <key>StandardOutPath</key>
    <string>$HOME/.n8n/n8n.log</string>
    <key>StandardErrorPath</key>
    <string>$HOME/.n8n/n8n.error.log</string>
</dict>
</plist>
PLIST_EOF
    launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
    launchctl bootstrap "gui/$(id -u)" "$PLIST"
    wait_for_n8n || { echo "n8n did not start. Check $HOME/.n8n/n8n.error.log"; exit 1; }
    echo "n8n runs in the background and will start at login."
    next_steps
    exit 0
    ;;

  docker)
    command -v docker >/dev/null 2>&1 || { echo "Docker not found. Just run: ./scripts/n8n.sh"; exit 1; }
    if ! docker info >/dev/null 2>&1; then
      echo "Docker is installed but not running. Starting Docker Desktop..."
      open -a Docker 2>/dev/null || true
      for _ in $(seq 1 60); do docker info >/dev/null 2>&1 && break; sleep 2; done
      docker info >/dev/null 2>&1 || { echo "Docker did not start."; exit 1; }
    fi
    if docker ps -a --format '{{.Names}}' | grep -qx mira-n8n; then
      docker start mira-n8n >/dev/null
    else
      docker volume create n8n_data >/dev/null
      docker run -d --name mira-n8n --restart unless-stopped \
        -p "127.0.0.1:$PORT:5678" \
        -v n8n_data:/home/node/.n8n \
        -e N8N_SECURE_COOKIE=false \
        -e GENERIC_TIMEZONE="$(readlink /etc/localtime | sed 's|.*/zoneinfo/||')" \
        docker.n8n.io/n8nio/n8n >/dev/null
    fi
    wait_for_n8n || { echo "Check: docker logs mira-n8n"; exit 1; }
    next_steps
    exit 0
    ;;

  start)
    ensure_installed
    echo "Starting n8n on http://localhost:$PORT (Ctrl-C to stop)..."
    echo "Tip: './scripts/n8n.sh autostart' runs it in the background instead."
    exec n8n start
    ;;

  *)
    echo "usage: $0 [start|install|autostart|stop|docker]"
    exit 1
    ;;
esac
