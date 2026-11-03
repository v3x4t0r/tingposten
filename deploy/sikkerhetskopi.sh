#!/usr/bin/env bash
# Nightly backup of the comments (data/debatt/debatt.db): readers' comments, votes and
# accounts can't be made again, unlike everything else in data/ (issue #1).
#
# 1. A consistent copy with SQLite's own backup (safe while the comment server writes),
#    checked with «PRAGMA integrity_check», to data/backup/debatt/ on the SD card.
# 2. The same file to a second disk, TINGPOSTEN_BACKUP_KOPI (the Pi's USB disk,
#    /media/data/tingposten-backup), so a dead SD card doesn't take the copies with it.
# Each place keeps BEHOLD_DAGER days. Started by a timer on the Pi (deploy/ansible).
#
# The database holds readers' e-mail addresses: the copies are readable by the owner only
# (the USB disk is exFAT, which has no file permissions; it is the owner's own disk).
#
# Exit codes: 0 ok, 1 the copy failed or the second disk is missing (the timer shows failed).
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO"
DB="${TINGPOSTEN_DEBATT_DB:-data/debatt/debatt.db}"
LOKALT="${TINGPOSTEN_BACKUP_LOKALT:-data/backup/debatt}"
KOPI="${TINGPOSTEN_BACKUP_KOPI:-}"
BEHOLD_DAGER="${BEHOLD_DAGER:-30}"

LOG_DIR="data/logs/sikkerhetskopi"
mkdir -p "$LOG_DIR"
exec >>"$LOG_DIR/$(date +%F).log" 2>&1
find "$LOG_DIR" -name '*.log' -type f -mtime +60 -delete
log() { echo "$(date -Is) $*"; }

log "start"
if [ ! -f "$DB" ]; then
  log "ingen database på $DB, ingenting å ta kopi av"
  log "done"
  exit 0
fi

umask 077
mkdir -p "$LOKALT"
chmod 700 "$LOKALT"
navn="debatt-$(date +%F).db"
python3 - "$DB" "$LOKALT/$navn.part" <<'EOF'
import sqlite3, sys
kilde = sqlite3.connect(f"file:{sys.argv[1]}?mode=ro", uri=True)
mal = sqlite3.connect(sys.argv[2])
kilde.backup(mal)
svar = mal.execute("PRAGMA integrity_check").fetchone()[0]
mal.close()
kilde.close()
if svar != "ok":
    sys.exit(f"integrity_check: {svar}")
EOF
mv "$LOKALT/$navn.part" "$LOKALT/$navn"
log "kopi: $LOKALT/$navn ($(stat -c %s "$LOKALT/$navn") byte)"
find "$LOKALT" -name 'debatt-*.db' -type f -mtime +"$BEHOLD_DAGER" -delete

if [ -n "$KOPI" ]; then
  # The disk must really be mounted: never fill the SD card through an empty mount point.
  disk="$(dirname "$KOPI")"
  if ! mountpoint -q "$disk"; then
    log "FEIL: $disk er ikke montert, så kopien ble bare lagret på SD-kortet"
    exit 1
  fi
  mkdir -p "$KOPI"
  chmod 700 "$KOPI" 2>/dev/null || true
  cp "$LOKALT/$navn" "$KOPI/$navn.part"
  mv "$KOPI/$navn.part" "$KOPI/$navn"
  log "kopi: $KOPI/$navn"
  find "$KOPI" -name 'debatt-*.db' -type f -mtime +"$BEHOLD_DAGER" -delete
fi
log "done"
