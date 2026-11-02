#!/usr/bin/env bash
# Daily Stortinget run (issue #25): fetch → write and check → build the PRIVATE test copy.
# Never publishes: Stortinget is not on tingposten.no until the owner has approved it
# (config.STORTINGET_PUBLISERT), and the test copy is refused by publiserer anyway.
# Started by a timer on the Pi (deploy/README.md) and by the control panel's «Kjør nå».
#
# Exit codes: 0 ok, 2 Stortinget refused the fetch, other = failure.
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO"
export PATH="$HOME/.local/bin:$PATH"

LOG_DIR="$REPO/data/logs/stortinget"
mkdir -p "$LOG_DIR"

# Its own lock: a Stortinget run and a Bergen run may overlap (different data, both polite).
exec 9>"$REPO/data/run-stortinget.lock"
if ! flock -n 9; then
  echo "$(date -Is) previous Stortinget run still in progress, exiting" >&2
  exit 1
fi

exec >>"$LOG_DIR/$(date +%F).log" 2>&1
find "$LOG_DIR" -name '*.log' -type f -mtime +60 -delete

log() { echo "$(date -Is) $*"; }
steg() { log "== $*"; }
run() { uv run --no-sync tingposten "$@"; }

log "start (stortinget)"

# 1. Decided saker, their votes and documents. A refusal stops today's run.
steg hent
rc=0
run stortinget hent || rc=$?
if [ "$rc" -eq 2 ]; then
  log "hent: Stortinget keeps refusing us (blocked), stopping for today"
  exit 2
elif [ "$rc" -ne 0 ]; then
  log "hent failed (exit $rc), stopping"
  exit "$rc"
fi

# 2. Writer → fact-checker → classifier → editor → articles. A model session limit leaves
# some saker for the next run, so a failure here is a warning.
steg kjor
rc=0
run stortinget kjor || rc=$?
if [ "$rc" -ne 0 ]; then
  log "warning: stortinget kjor exit $rc (often a model session limit); the next run redoes what is missing"
fi

# 3. The private test copy: Bergen + /stortinget/ + /blandet/, noindex, never publishable.
steg bygg
rc=0
run bygg --til site-test --med-stortinget --testkopi || rc=$?
if [ "$rc" -ne 0 ]; then
  log "bygg failed (exit $rc)"
  exit "$rc"
fi
log "ko: $(run stortinget ko | grep -c "på forsiden $(date +%F)" || true) saker til forsiden i dag"
log "done"
