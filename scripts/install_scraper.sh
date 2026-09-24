#!/bin/bash
# Builds the Google Maps scraper Mira's lead finder uses (daemon/leads.py).
#
# The scraper is gosom/google-maps-scraper (MIT, by Georgios Komninos). It is
# built natively here rather than run from its Docker image: Docker on a Mac
# keeps a Linux VM holding gigabytes of RAM for as long as it runs, and the
# published image is amd64-only, so on Apple Silicon it would also run under
# emulation. Built from source it is one arm64/x86_64 binary that the daemon
# starts for the length of a search and that exits on its own afterwards.
#
#   ./scripts/install_scraper.sh              build the latest release
#   ./scripts/install_scraper.sh v1.18.1      build a specific tag
#
# Safe to re-run -- that's also how you update it.
#
# Requires Go (brew install go) and git. First run also downloads the headless
# Chromium the scraper drives (~150MB, into ~/Library/Caches/ms-playwright).

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BIN_DIR="$REPO_ROOT/daemon/bin"
BIN="$BIN_DIR/google-maps-scraper"
UPSTREAM="https://github.com/gosom/google-maps-scraper"

# Homebrew's bin isn't on PATH when this is run from a bare shell.
for d in /opt/homebrew/bin /usr/local/bin /usr/local/go/bin; do
  [ -d "$d" ] && case ":$PATH:" in *":$d:"*) ;; *) PATH="$PATH:$d" ;; esac
done
export PATH

command -v git >/dev/null || { echo "error: git not found. Install the Xcode command line tools: xcode-select --install"; exit 1; }
command -v go  >/dev/null || { echo "error: Go not found. Install it with: brew install go"; exit 1; }

REF="${1:-}"
if [ -z "$REF" ]; then
  # Latest release tag rather than the default branch, which can be mid-change.
  # sed rather than `head -1`: head exits early, git dies of SIGPIPE, and
  # pipefail turns that into the whole script failing.
  REF="$(git ls-remote --tags --refs --sort=-v:refname "$UPSTREAM" 'v*' | sed -n '1s#.*refs/tags/##p')"
  [ -n "$REF" ] || { echo "error: could not read the latest release from $UPSTREAM"; exit 1; }
fi

SRC="$(mktemp -d)"
trap 'rm -rf "$SRC"' EXIT

echo "Fetching gosom/google-maps-scraper $REF..."
git clone --quiet --depth 1 --branch "$REF" "$UPSTREAM" "$SRC/src"

# The project pins a Go version in go.mod; GOTOOLCHAIN=auto lets an older
# local Go download the one it needs instead of refusing to build.
echo "Building (the first build downloads dependencies and takes a few minutes)..."
mkdir -p "$BIN_DIR"
(cd "$SRC/src" && GOTOOLCHAIN=auto CGO_ENABLED=0 go build -ldflags="-w -s" -o "$BIN.new" .)

# Browser first, binary into place second: Mira treats "the binary exists" as
# "the scraper is installed", so a failed download must not leave one behind.
echo "Installing the headless Chromium it drives..."
if ! PLAYWRIGHT_INSTALL_ONLY=1 "$BIN.new"; then
  rm -f "$BIN.new"
  echo "error: could not download Chromium. Check your connection and re-run this script."
  exit 1
fi
mv "$BIN.new" "$BIN"

echo "$REF" > "$BIN_DIR/google-maps-scraper.version"
echo
echo "Installed $REF at $BIN"
echo "Ask Mira something like: \"find dentists in Indiranagar, Bangalore\"."
