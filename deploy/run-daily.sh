#!/usr/bin/env bash
# Daily Tingposten run on the Pi. Called from cron, see deploy/crontab.example,
# and from the admin panel (tingposten admin) with a mode:
#
#   run-daily.sh            hent → last-ned → kjor → bygg → (publiser, only if PUBLISER exists)
#   run-daily.sh --sjekk    hent only: has Bergen published anything new? (the panel's «Sjekk nå»)
#   run-daily.sh --manuell  hent → the missing PDFs, with the same polite script a person runs
#                           by hand → kjor → bygg → publiser. Someone pressed the button, so
#                           PUBLISER isn't needed; STOP still wins.
#   run-daily.sh --publiser bygg → publiser only (the panel, after the editor published a
#                           debattinnlegg). STOP still wins.
#
# Exit codes: 0 ok (also when publishing is off or STOP is set),
# 2 Bergen kommune refused the agenda fetch, 64 unknown mode, other = failure.
set -euo pipefail

MODUS=daglig
case "${1:-}" in
  "") ;;
  --sjekk) MODUS=sjekk ;;
  --manuell) MODUS=manuell ;;
  --publiser) MODUS=publiser ;;
  *) echo "usage: $0 [--sjekk | --manuell | --publiser]" >&2; exit 64 ;;
esac

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
# The admin panel follows a run by these lines.
steg() { log "== $*"; }
# --no-sync: the daily run never changes the venv; updates are a manual step.
run() { uv run --no-sync tingposten "$@"; }

log "start ($MODUS)"

if [ "$MODUS" != publiser ]; then

# 1. Agenda metadata. If Bergen refuses us, nothing else happens today.
# A run started by hand skips this when the agenda was fetched in the last
# 30 minutes, so pressing the buttons twice doesn't double our requests.
steg hent
hentet=no
if [ "$MODUS" != daglig ] && [ -n "$(find data/bergen/agendapunkter -name '*.json' -mmin -30 2>/dev/null)" ]; then
  log "hent: the agenda was fetched less than 30 minutes ago, skipping"
else
  rc=0
  run hent || rc=$?
  if [ "$rc" -eq 2 ]; then
    log "hent: server keeps refusing us (blocked), stopping for today"
    exit 2
  elif [ "$rc" -ne 0 ]; then
    log "hent failed (exit $rc), stopping"
    exit "$rc"
  fi
  hentet=yes
fi

# 2. PDFs. A no-op until config.ALLOW_PDF_FETCH is turned on (robots.txt).
steg last-ned
rc=0
run last-ned || rc=$?
if [ "$rc" -eq 2 ]; then
  log "last-ned: server refused PDF downloads; carrying on with the PDFs we have"
elif [ "$rc" -ne 0 ]; then
  log "warning: last-ned failed (exit $rc); carrying on with the PDFs we have"
fi
# Until then: keep a ready script for the PDFs still missing, which a person
# runs by hand (bash data/bergen/last-ned-mangler.sh). Nothing is fetched here.
run pdf-urler --mangler --script data/bergen/last-ned-mangler.sh >/dev/null \
  || log "warning: could not write the manual download script"

if [ "$MODUS" = sjekk ]; then
  log "done"
  exit 0
fi

# A press of the panel's button is that person running the script: one file at
# a time, 5 s apart, identifiable User-Agent, stops at the first refusal.
if [ "$MODUS" = manuell ] && [ -f data/bergen/last-ned-mangler.sh ]; then
  # Space our requests out after the agenda fetch.
  [ "$hentet" = no ] || sleep 60
  rc=0
  bash data/bergen/last-ned-mangler.sh || rc=$?
  if [ "$rc" -ne 0 ]; then
    log "warning: PDF download stopped (exit $rc); carrying on with the PDFs we have"
  fi
fi

# 3. Writer → fact-checker → classifier → editor. Uses the Claude and Codex
# subscription CLIs; a session limit leaves some saker unfinished, and the
# next run picks up exactly what is missing. So a failure here is a warning.
steg kjor
for cli in claude codex; do
  command -v "$cli" >/dev/null || log "warning: $cli not found on PATH; its steps will fail"
done
rc=0
run kjor || rc=$?
if [ "$rc" -ne 0 ]; then
  log "warning: kjor exit $rc (often a model session limit); the next run redoes what is missing"
fi

fi  # not publiser

# 4. Build site/ from what is publishable. A failed build must not be published.
steg bygg
rc=0
run bygg || rc=$?
if [ "$rc" -ne 0 ]; then
  log "bygg failed (exit $rc), not publishing"
  exit "$rc"
fi

# 5. Publishing is opt-in: `touch PUBLISER` in the repo root turns it on for
# the daily run. A run started from the panel publishes without it.
steg publiser
if [ "$MODUS" = daglig ] && [ ! -e PUBLISER ]; then
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
  log "done"
  exit 0
fi

run publiser
log "done"
