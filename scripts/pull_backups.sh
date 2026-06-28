#!/usr/bin/env bash
#
# Mirror the off-host backups DOWN to this machine (the venue laptop), so an
# emergency local restore works with zero internet. Run from cron every few
# minutes WHILE you still have connectivity — the last successful pull is your
# restore point the moment the network (or the VPS) dies.
#
# Pulls from the same REMOTE that backup_db.sh pushes to. Read-only on the
# remote: rsync down, never --delete, so it can never harm the off-host copy
# (and never deletes the venue dumps this box writes during an outage).
#
# Usage:
#   scripts/pull_backups.sh             # one pull now
#   scripts/pull_backups.sh --install   # schedule every 5 min on this user's cron
#   scripts/pull_backups.sh --remove    # unschedule
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
cd "$PROJECT_DIR"

[ -f scripts/backup_db.env ] || {
  echo "pull_backups: scripts/backup_db.env not found (copy backup_db.env.example)" >&2; exit 1; }
set -a; . ./scripts/backup_db.env; set +a

LOCAL_DIR="${LOCAL_DIR:-/opt/mahj-backups}"
INTERVAL_MIN="${INTERVAL_MIN:-5}"
TAG="# mahj-pull (managed by pull_backups.sh)"

: "${REMOTE:?REMOTE unset in scripts/backup_db.env}"
: "${SSH_KEY:?SSH_KEY unset in scripts/backup_db.env}"

# --- cron (un)install --------------------------------------------------------
if [ "${1:-}" = "--install" ] || [ "${1:-}" = "--remove" ]; then
  LOG="${PULL_LOG:-$LOCAL_DIR/pull.log}"
  LINE="*/$INTERVAL_MIN * * * * flock -n /tmp/mahj-pull.lock bash -lc '$SCRIPT_DIR/pull_backups.sh' >> '$LOG' 2>&1 $TAG"
  new_cron="$(crontab -l 2>/dev/null | grep -vF "$TAG" || true)"
  if [ "$1" = "--remove" ]; then
    printf '%s\n' "$new_cron" | crontab -
    echo "pull_backups: removed the scheduled pull."; exit 0
  fi
  mkdir -p "$LOCAL_DIR"; touch "$LOG"
  printf '%s\n%s\n' "$new_cron" "$LINE" | crontab -
  echo "pull_backups: scheduled every $INTERVAL_MIN min → $LOCAL_DIR (log: $LOG)"
  echo "pull_backups: remove with: scripts/pull_backups.sh --remove"
  exit 0
fi

# --- one pull ----------------------------------------------------------------
SSH_OPTS="ssh -i $SSH_KEY -p ${SSH_PORT:-22} -o StrictHostKeyChecking=accept-new -o BatchMode=yes"
mkdir -p "$LOCAL_DIR"
# Trailing slashes: copy the CONTENTS of the remote dir into LOCAL_DIR. -a keeps
# mtimes so restore_db.sh's "newest" ordering and age-based logic stay correct.
rsync -az -e "$SSH_OPTS" "$REMOTE/" "$LOCAL_DIR/"
echo "pull_backups: synced $REMOTE → $LOCAL_DIR ($(ls -1 "$LOCAL_DIR"/mahj_*.dump 2>/dev/null | wc -l) dumps held)"
