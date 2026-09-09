#!/bin/bash
# Starts a local n8n for Mira's automations.
#
# n8n is not bundled with Mira and never will be -- it is a workflow engine in
# its own right, and Mira's job is only to fire named webhooks at it. This
# script exists so "get n8n running" is one command rather than a support
# thread.
#
# Two ways to run it. Docker is the default because n8n's npm install pulls a
# native sqlite3 that has to compile against whatever Node you happen to have,
# and that is the step that breaks.
#
#   ./scripts/n8n.sh            # Docker (recommended)
#   ./scripts/n8n.sh npm        # global npm install, if you prefer no Docker
#   ./scripts/n8n.sh stop       # stop the Docker container

set -euo pipefail

PORT=5678
CONTAINER=mira-n8n
DATA_VOLUME=n8n_data

case "${1:-docker}" in
  stop)
    docker stop "$CONTAINER" >/dev/null 2>&1 && echo "Stopped $CONTAINER." || echo "Not running."
    exit 0
    ;;

  npm)
    command -v n8n >/dev/null 2>&1 || {
      echo "n8n not installed. Run:  npm install -g n8n"
      exit 1
    }
    echo "Starting n8n on http://localhost:$PORT (Ctrl-C to stop)..."
    exec n8n start
    ;;

  docker)
    command -v docker >/dev/null 2>&1 || {
      echo "Docker not found. Install Docker Desktop, or run: ./scripts/n8n.sh npm"
      exit 1
    }

    # The CLI being present says nothing about the daemon running, and Docker's
    # own error for this is a socket path -- unhelpful if you just forgot to
    # open Docker Desktop.
    if ! docker info >/dev/null 2>&1; then
      echo "Docker is installed but not running. Starting Docker Desktop..."
      open -a Docker 2>/dev/null || true
      printf "Waiting for the Docker daemon"
      for _ in $(seq 1 60); do
        if docker info >/dev/null 2>&1; then echo " -- up."; break; fi
        printf "."
        sleep 2
      done
      docker info >/dev/null 2>&1 || {
        echo
        echo "Docker did not start. Open Docker Desktop yourself, or run: ./scripts/n8n.sh npm"
        exit 1
      }
    fi

    if docker ps -a --format '{{.Names}}' | grep -qx "$CONTAINER"; then
      docker start "$CONTAINER" >/dev/null
      echo "Restarted existing $CONTAINER."
    else
      # Named volume, not a bind mount: workflows and credentials must survive
      # the container being recreated on an n8n upgrade.
      docker volume create "$DATA_VOLUME" >/dev/null
      docker run -d --name "$CONTAINER" \
        --restart unless-stopped \
        -p "$PORT:5678" \
        -v "$DATA_VOLUME:/home/node/.n8n" \
        -e GENERIC_TIMEZONE="$(readlink /etc/localtime | sed 's|.*/zoneinfo/||')" \
        docker.n8n.io/n8nio/n8n >/dev/null
      echo "Created $CONTAINER."
    fi
    ;;

  *)
    echo "usage: $0 [docker|npm|stop]"
    exit 1
    ;;
esac

printf "Waiting for n8n"
for _ in $(seq 1 60); do
  if curl -fsS -m 2 "http://localhost:$PORT/" >/dev/null 2>&1; then
    echo " -- ready."
    echo
    echo "Open http://localhost:$PORT and create the owner account."
    echo
    echo "To wire a workflow into Mira:"
    echo "  1. Add a Webhook trigger node and copy its Production URL."
    echo "  2. Mira -> Automations -> add it with a name and a description."
    echo "     The description is what voice and chat match against, so write it"
    echo "     the way you would ask for it (\"send the daily standup summary\")."
    echo "  3. Mira POSTs JSON including source and triggered_at."
    echo
    echo "n8n has to be running to receive a webhook. The container restarts"
    echo "with Docker (--restart unless-stopped), so the only manual step is"
    echo "Docker itself -- turn on Docker Desktop > Settings > General >"
    echo "\"Start Docker Desktop when you sign in\" to remove that too."
    exit 0
  fi
  printf "."
  sleep 2
done

echo
echo "n8n did not answer on port $PORT within two minutes."
echo "Docker logs:  docker logs $CONTAINER"
exit 1
