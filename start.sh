#!/usr/bin/env bash
# catan-solver: bring up everything needed to advise a live game.
#
#   ./start.sh              launch Chrome (if needed) + server + dashboard
#   ./start.sh --no-browser server only, if Chrome is already on :9222
#
# Chrome runs on a dedicated profile so it never touches your normal one, and
# the debug port is what lets us read colonist's websocket. Play in THAT
# window -- the dashboard follows whatever game it is showing.

set -euo pipefail
cd "$(dirname "$0")"

PORT="${PORT:-8017}"
CDP_PORT="${CDP_PORT:-9222}"
PROFILE="${CHROME_PROFILE:-$HOME/.chrome-catan-profile}"
LAUNCH_BROWSER=1
# consume our own flags so the rest can be forwarded to uvicorn untouched
if [[ "${1:-}" == "--no-browser" ]]; then
  LAUNCH_BROWSER=0
  shift
fi

say() { printf '\033[33m▸\033[0m %s\n' "$1"; }
die() { printf '\033[31m✗\033[0m %s\n' "$1" >&2; exit 1; }

command -v uv >/dev/null || die "uv is not installed — see https://docs.astral.sh/uv/"

find_chrome() {
  for c in \
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome" \
    "/Applications/Chromium.app/Contents/MacOS/Chromium" \
    "$(command -v google-chrome 2>/dev/null || true)" \
    "$(command -v chromium 2>/dev/null || true)"; do
    [[ -n "$c" && -x "$c" ]] && { echo "$c"; return; }
  done
}

say "syncing dependencies"
uv sync --quiet

# --- Chrome with the debug port ---------------------------------------------
if [[ $LAUNCH_BROWSER -eq 1 ]]; then
  if curl -sf "http://localhost:$CDP_PORT/json/version" >/dev/null 2>&1; then
    say "Chrome already listening on :$CDP_PORT"
  else
    CHROME="$(find_chrome)"
    [[ -n "$CHROME" ]] || die "no Chrome/Chromium found; start it yourself with --remote-debugging-port=$CDP_PORT"
    say "launching Chrome on :$CDP_PORT (profile: $PROFILE)"
    "$CHROME" \
      --remote-debugging-port="$CDP_PORT" \
      --user-data-dir="$PROFILE" \
      --no-first-run --no-default-browser-check \
      https://colonist.io >/dev/null 2>&1 &
    for _ in $(seq 1 20); do
      curl -sf "http://localhost:$CDP_PORT/json/version" >/dev/null 2>&1 && break
      sleep 0.5
    done
    curl -sf "http://localhost:$CDP_PORT/json/version" >/dev/null 2>&1 \
      || say "Chrome did not report ready — the feed will keep retrying"
  fi
fi

# --- server ------------------------------------------------------------------
if lsof -ti:"$PORT" >/dev/null 2>&1; then
  say "port $PORT busy — stopping the old server"
  lsof -ti:"$PORT" | xargs kill -9 2>/dev/null || true
  sleep 1
fi

say "dashboard → http://localhost:$PORT"
( sleep 2
  command -v open >/dev/null && open "http://localhost:$PORT" >/dev/null 2>&1 || true
) &

exec uv run uvicorn app.main:app --port "$PORT" "$@"
