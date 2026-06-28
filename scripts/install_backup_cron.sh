#!/usr/bin/env bash
#
# One-time helper: schedule backup_db.sh on the current user's crontab and
# create the local dump directory. Idempotent — re-running replaces the entry,
# never duplicates it. Run this ON THE PROD HOST as the user whose crontab
# should own the job (its $SSH_KEY in scripts/backup_db.env does the off-host push).
#
# Usage:
#   scripts/install_backup_cron.sh             # every 5 min (default)
#   INTERVAL_MIN=15 scripts/install_backup_cron.sh
#   scripts/install_backup_cron.sh --remove    # remove the scheduled job
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
cd "$PROJECT_DIR"

# Pick up LOCAL_DIR override if the operator set one in backup_db.env.
[ -f scripts/backup_db.env ] && { set -a; . ./scripts/backup_db.env; set +a; }
LOCAL_DIR="${LOCAL_DIR:-/opt/mahj-backups}"
BACKUP="$SCRIPT_DIR/backup_db.sh"
LOG="${BACKUP_LOG:-$LOCAL_DIR/backup.log}"
INTERVAL_MIN="${INTERVAL_MIN:-5}"
TAG="# mahj-backup (managed by install_backup_cron.sh)"

# flock -n: a slow run (rsync hung on a flaky link) must never let the next
# 5-min tick stack a second pg_dump on top of it; -n just skips this tick.
# bash -lc: cron's PATH is bare (/usr/bin:/bin), a login shell sources the
# profile so `docker` resolves wherever it's installed.
LINE="*/$INTERVAL_MIN * * * * flock -n /tmp/mahj-backup.lock bash -lc '$BACKUP' >> '$LOG' 2>&1 $TAG"

# Drop any existing managed entry; add the new one only unless --remove.
new_cron="$(crontab -l 2>/dev/null | grep -vF "$TAG" || true)"
if [ "${1:-}" = "--remove" ]; then
  printf '%s\n' "$new_cron" | crontab -
  echo "install_backup_cron: removed the scheduled backup."
  exit 0
fi

# Make sure the dump dir + log exist and are writable by this user.
if ! mkdir -p "$LOCAL_DIR" 2>/dev/null; then
  echo "install_backup_cron: creating $LOCAL_DIR needs elevated rights — using sudo"
  sudo mkdir -p "$LOCAL_DIR"
  sudo chown "$(id -un):$(id -gn)" "$LOCAL_DIR"
fi
touch "$LOG"

printf '%s\n%s\n' "$new_cron" "$LINE" | crontab -
echo "install_backup_cron: scheduled — every $INTERVAL_MIN min:"
echo "    $LINE"
echo "install_backup_cron: dumps → $LOCAL_DIR (+ off-host rsync), log → $LOG"
echo "install_backup_cron: remove with: scripts/install_backup_cron.sh --remove"
