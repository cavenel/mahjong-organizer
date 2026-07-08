#!/usr/bin/env bash
#
# Restore a Postgres dump (produced by backup_db.sh) into the LIVE database.
# This is the "roll back to a known-good point" button for use DURING the event.
#
# ⚠️  DESTRUCTIVE: drops and recreates the live '$DB_NAME' database, wiping every
#     row committed since the chosen dump. It stops the writers (web,
#     scan_worker) and pgbouncer for the duration so nothing writes mid-restore,
#     then brings the stack back up — web re-runs `migrate` on boot, so an older
#     dump is caught up to the current code automatically.
#
# To only *validate* a dump without touching live data, use restore_test.sh.
#
# Usage:
#   scripts/restore_db.sh                 # pick from local dumps (interactive)
#   scripts/restore_db.sh --latest        # newest local dump (still confirms)
#   scripts/restore_db.sh /path/to.dump   # a specific local dump file
#   scripts/restore_db.sh --remote        # list dumps ON the remote, pull & restore
set -euo pipefail

# --- locate the project + load config ---------------------------------------
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
cd "$PROJECT_DIR"

# DB vars: parsed, NOT sourced — .env is Docker env_file format, not valid shell.
[ -f .env ] || { echo "restore_db: .env not found in $PROJECT_DIR" >&2; exit 1; }
read_env() { grep -E "^$1=" .env | head -n1 | cut -d= -f2-; }
DB_NAME="$(read_env DB_NAME)"
DB_USER="$(read_env DB_USER)"
DB_PASSWORD="$(read_env DB_PASSWORD)"
# backup_db.env is our own valid-shell file (REMOTE/SSH_KEY + optional overrides).
[ -f scripts/backup_db.env ] && { set -a; . ./scripts/backup_db.env; set +a; }

LOCAL_DIR="${LOCAL_DIR:-/opt/mahj-backups}"
COMPOSE="${COMPOSE:-docker compose -f docker-compose.yml -f docker-compose.prod.yml}"

: "${DB_NAME:?DB_NAME unset in .env}"
: "${DB_USER:?DB_USER unset in .env}"
: "${DB_PASSWORD:?DB_PASSWORD unset in .env}"

# --- choose a dump -----------------------------------------------------------
dump=""
case "${1:-}" in
  --remote)
    : "${REMOTE:?REMOTE unset in scripts/backup_db.env}"
    : "${SSH_KEY:?SSH_KEY unset in scripts/backup_db.env}"
    SSH_OPTS="ssh -i $SSH_KEY -p ${SSH_PORT:-22} -o StrictHostKeyChecking=accept-new -o BatchMode=yes"
    remote_host="${REMOTE%%:*}"
    remote_path="${REMOTE#*:}"
    echo "restore_db: listing dumps on $REMOTE …"
    mapfile -t remotes < <($SSH_OPTS "$remote_host" \
      "ls -1t '$remote_path'/mahj_*.dump 2>/dev/null" || true)
    [ "${#remotes[@]}" -gt 0 ] || { echo "restore_db: no dumps found on remote" >&2; exit 1; }
    echo "Pick a dump to pull from the remote (newest first):"
    select choice in "${remotes[@]}"; do
      [ -n "${choice:-}" ] && break
    done
    mkdir -p "$LOCAL_DIR"
    dump="$LOCAL_DIR/$(basename "$choice")"
    echo "restore_db: pulling $choice → $dump"
    rsync -az -e "$SSH_OPTS" "$remote_host:$choice" "$dump"
    ;;
  --latest)
    dump="$(ls -1t "$LOCAL_DIR"/mahj_*.dump 2>/dev/null | head -1 || true)"
    [ -n "$dump" ] || { echo "restore_db: no local dump in $LOCAL_DIR" >&2; exit 1; }
    ;;
  "")
    mapfile -t locals < <(ls -1t "$LOCAL_DIR"/mahj_*.dump 2>/dev/null || true)
    [ "${#locals[@]}" -gt 0 ] || {
      echo "restore_db: no local dumps in $LOCAL_DIR (try --remote)" >&2; exit 1; }
    echo "Pick a dump to restore (newest first):"
    select choice in "${locals[@]}"; do
      [ -n "${choice:-}" ] && { dump="$choice"; break; }
    done
    ;;
  *)
    dump="$1"
    [ -f "$dump" ] || { echo "restore_db: dump not found: $dump" >&2; exit 1; }
    ;;
esac

[ -s "$dump" ] || { echo "restore_db: dump is empty: $dump" >&2; exit 1; }
head -c 5 "$dump" | grep -q "PGDMP" || {
  echo "restore_db: '$dump' is missing the PGDMP header — not a valid dump" >&2; exit 1; }

# --- confirm (destructive) ---------------------------------------------------
echo
echo "  ⚠️  This will OVERWRITE the live '$DB_NAME' database with:"
echo "         $dump  ($(du -h "$dump" | cut -f1), $(date -r "$dump" 2>/dev/null || echo '?'))"
echo "      Everything committed since this dump will be LOST."
echo
read -r -p "  Type the database name ('$DB_NAME') to proceed: " ans
[ "$ans" = "$DB_NAME" ] || { echo "restore_db: aborted (no match)"; exit 1; }

psql() { $COMPOSE exec -T -e PGPASSWORD="$DB_PASSWORD" db \
  psql -v ON_ERROR_STOP=1 -U "$DB_USER" "$@"; }

# --- restore -----------------------------------------------------------------
echo "restore_db: ensuring db container is up …"
$COMPOSE up -d --wait db

echo "restore_db: stopping writers (web, scan_worker) and pgbouncer …"
$COMPOSE stop web scan_worker pgbouncer

# DROP … WITH (FORCE) (PG13+) terminates any leftover sessions itself, but the
# stop above means there should be none. Connect to the maintenance 'postgres'
# DB since you can't drop the database you're connected to.
echo "restore_db: dropping + recreating '$DB_NAME' …"
psql -d postgres -c "DROP DATABASE IF EXISTS \"$DB_NAME\" WITH (FORCE);"
psql -d postgres -c "CREATE DATABASE \"$DB_NAME\" OWNER \"$DB_USER\";"

echo "restore_db: restoring …"
# --no-owner/--no-acl so the dump restores cleanly regardless of role names.
$COMPOSE exec -T -e PGPASSWORD="$DB_PASSWORD" db \
  pg_restore -U "$DB_USER" -d "$DB_NAME" --no-owner --no-acl < "$dump"

echo "restore_db: restored row counts —"
psql -d "$DB_NAME" -c 'SELECT
  (SELECT count(*) FROM mahj_player) AS players,
  (SELECT count(*) FROM mahj_seat)   AS seats,
  (SELECT count(*) FROM mahj_hand)   AS hands;'

echo "restore_db: bringing the stack back up (web re-runs migrate on boot) …"
$COMPOSE up -d --wait

echo "restore_db: done — restored from $(basename "$dump")"
$COMPOSE ps
