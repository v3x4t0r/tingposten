#!/usr/bin/env bash
# Daily Tingposten run on the Pi. Called from cron, see deploy/crontab.example.
#
#   hent → last-ned → kjor → bygg → (publiser, only if PUBLISER exists)
#
# Exit codes: 0 ok (also when publishing is off or STOP is set),
# 2 Bergen kommune refused the agenda fetch, other = failure.
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO"
export PATH="$HOME/.local/bin:$PATH"

LOG_DIR="$REPO/data/logs"
mkdir -p "$LOG_DIR"

# Never let two runs overlap; backoffs and model calls can make a run slow.
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

# 1. Agenda metadata. If Bergen refuses us, nothing else happens today.
rc=0
run hent || rc=$?
if [ "$rc" -eq 2 ]; then
  log "hent: server keeps refusing us (blocked), stopping for today"
  exit 2
elif [ "$rc" -ne 0 ]; then
  log "hent failed (exit $rc), stopping"
  exit "$rc"
fi

# 2. PDFs. A no-op until config.ALLOW_PDF_FETCH is turned on (robots.txt).
rc=0
run last-ned || rc=$?
if [ "$rc" -eq 2 ]; then
  log "last-ned: server refused PDF downloads; carrying on with the PDFs we have"
elif [ "$rc" -ne 0 ]; then
  log "warning: last-ned failed (exit $rc); carrying on with the PDFs we have"
fi

# 3. Writer → fact-checker → classifier → editor. Uses the Claude and Codex
# subscription CLIs; a session limit leaves some saker unfinished, and the
# next run picks up exactly what is missing. So a failure here is a warning.
for cli in claude codex; do
  command -v "$cli" >/dev/null || log "warning: $cli not found on PATH; its steps will fail"
done
rc=0
run kjor || rc=$?
if [ "$rc" -ne 0 ]; then
  log "warning: kjor exit $rc (often a model session limit); the next run redoes what is missing"
fi

# 4. Build site/ from what is publishable. A failed build must not be published.
rc=0
run bygg || rc=$?
if [ "$rc" -ne 0 ]; then
  log "bygg failed (exit $rc), not publishing"
  exit "$rc"
fi

# 5. Publishing is opt-in: `touch PUBLISER` in the repo root turns it on.
if [ ! -e PUBLISER ]; then
  log "PUBLISER not set: site/ built locally, not published"
  log "done"
  exit 0
fi

# Kill switch, which always wins: STOP in the repo root here, or a STOP file
# committed to main on GitHub (works from a phone). Fetching only updates
# origin/main; the code that runs here is never changed.
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

run publiser
log "done"
