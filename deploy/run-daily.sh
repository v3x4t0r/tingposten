#!/usr/bin/env bash
# Daily Tingposten run on the Pi. Called from cron, see deploy/crontab.example.
# Exit codes: 0 ok (or kill switch), 2 Bergen kommune refused us, other = failure.
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO"
export PATH="$HOME/.local/bin:$PATH"

LOG_DIR="$REPO/data/logs"
mkdir -p "$LOG_DIR"

# Never let two runs overlap; backoffs can make a run slow.
exec 9>"$REPO/data/run-daily.lock"
if ! flock -n 9; then
  echo "$(date -Is) previous run still in progress, exiting" >&2
  exit 1
fi

exec >>"$LOG_DIR/$(date +%F).log" 2>&1
find "$LOG_DIR" -name '*.log' -type f -mtime +60 -delete

log() { echo "$(date -Is) $*"; }
# --no-sync: the daily run never changes the venv; updates are a manual step.
run() { uv run --no-sync tingposten "$@"; }

log "start"

rc=0
run hent || rc=$?
if [ "$rc" -eq 2 ]; then
  log "hent: server keeps refusing us (blocked), not publishing today"
  exit 2
elif [ "$rc" -ne 0 ]; then
  log "hent failed (exit $rc), not publishing"
  exit "$rc"
fi

# Phase 1:
# run klassifiser
# run skriv
# run faktasjekk
# run rediger
# run bygg

# Kill switch: STOP in the repo root here, or a STOP file committed to main
# on GitHub (works from a phone). Fetching only updates origin/main; the
# code that runs here is never changed.
remote_stop=no
if git fetch --quiet origin main 2>/dev/null; then
  if git cat-file -e origin/main:STOP 2>/dev/null; then
    remote_stop=yes
  fi
else
  log "warning: git fetch failed, checking local STOP only"
fi
if [ -e STOP ] || [ "$remote_stop" = yes ]; then
  log "STOP found, skipping publish"
  exit 0
fi

# --placeholder until `bygg` exists; then drop the flag.
run publiser --placeholder
log "done"
