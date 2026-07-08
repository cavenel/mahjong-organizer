#!/usr/bin/env bash
#
# Rehearse a restore of a Postgres dump produced by backup_db.sh.
# A backup you've never restored is not a backup — run this BEFORE the event.
#
# Restores into a throwaway database (never touches the live one), prints row
# counts for the core scoring tables, then drops the scratch DB.
#
# Usage:
#   scripts/restore_test.sh                 # newest dump in $LOCAL_DIR
#   scripts/restore_test.sh /path/to.dump   # a specific dump
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
cd "$PROJECT_DIR"

# Parse DB vars (not source — .env is Docker env_file format, not valid shell).
[ -f .env ] || { echo "restore_test: .env not found" >&2; exit 1; }
read_env() { grep -E "^$1=" .env | head -n1 | cut -d= -f2-; }
DB_NAME="$(read_env DB_NAME)"
DB_USER="$(read_env DB_USER)"
DB_PASSWORD="$(read_env DB_PASSWORD)"
# backup_db.env is our own valid-shell file (optional here, for overrides).
[ -f scripts/backup_db.env ] && { set -a; . ./scripts/backup_db.env; set +a; }

LOCAL_DIR="${LOCAL_DIR:-/opt/mahj-backups}"
COMPOSE="${COMPOSE:-docker compose -f docker-compose.yml -f docker-compose.prod.yml}"
SCRATCH="restore_test_$(date -u +%H%M%S)"

dump="${1:-$(ls -1t "$LOCAL_DIR"/mahj_*.dump 2>/dev/null | head -1 || true)}"
[ -n "$dump" ] && [ -f "$dump" ] || { echo "restore_test: no dump found ($LOCAL_DIR)" >&2; exit 1; }
echo "restore_test: restoring $dump into scratch DB '$SCRATCH'"

psql() { $COMPOSE exec -T -e PGPASSWORD="$DB_PASSWORD" db psql -U "$DB_USER" "$@"; }

drop_scratch() { psql -d postgres -c "DROP DATABASE IF EXISTS $SCRATCH;" >/dev/null 2>&1 || true; }
trap drop_scratch EXIT

psql -d postgres -c "CREATE DATABASE $SCRATCH;" >/dev/null

# --no-owner/--no-acl so the dump restores cleanly regardless of role names.
$COMPOSE exec -T -e PGPASSWORD="$DB_PASSWORD" db \
  pg_restore -U "$DB_USER" -d "$SCRATCH" --no-owner --no-acl < "$dump"

echo "restore_test: row counts in restored copy —"
psql -d "$SCRATCH" -c 'SELECT
  (SELECT count(*) FROM mahj_player) AS players,
  (SELECT count(*) FROM mahj_seat)   AS seats,
  (SELECT count(*) FROM mahj_hand)   AS hands;'

echo "restore_test: OK (scratch DB will be dropped)"
