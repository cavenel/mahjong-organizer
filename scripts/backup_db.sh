#!/usr/bin/env bash
#
# Logical Postgres backup for the Mahjong championship stack.
#
#   1. pg_dump (custom format) INSIDE the db container — bypasses pgbouncer,
#      which is transaction-pooled and can't serve pg_dump's session needs.
#   2. Write atomically into a host dir OUTSIDE the Docker volume, so an
#      accidental `down -v` / bad migration / dropped table is recoverable.
#   3. rsync the new dump off-host (the VPS is a single point of failure;
#      an on-box-only backup doesn't survive losing the box).
#   4. Prune old dumps locally and remotely.
#
# Designed for cron — non-interactive, fails loudly with a non-zero exit so a
# cron MAILTO / monitor catches a broken backup BEFORE the event needs it.
#
# Config: DB creds come from the project .env; remote/SSH from
# scripts/backup_db.env (copy from scripts/backup_db.env.example).
#
# Restore with scripts/restore_test.sh (rehearse this before the event!).
set -euo pipefail

# --- locate the project + load config ---------------------------------------
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
cd "$PROJECT_DIR"

# DB_NAME / DB_USER / DB_PASSWORD — parsed, NOT sourced: .env is Docker env_file
# format (e.g. an unquoted SECRET_KEY with ()/# chars), which is not valid shell
# and would break `. .env`. cut -d= -f2- mirrors how Docker reads each line.
[ -f .env ] || { echo "backup_db: .env not found in $PROJECT_DIR" >&2; exit 1; }
read_env() { grep -E "^$1=" .env | head -n1 | cut -d= -f2-; }
DB_NAME="$(read_env DB_NAME)"
DB_USER="$(read_env DB_USER)"
DB_PASSWORD="$(read_env DB_PASSWORD)"

# REMOTE / SSH_KEY (+ optional overrides) — this file is OURS, written as valid
# shell (see backup_db.env.example), so sourcing it is safe.
[ -f scripts/backup_db.env ] || {
  echo "backup_db: scripts/backup_db.env not found (copy backup_db.env.example)" >&2; exit 1; }
set -a; . ./scripts/backup_db.env; set +a

LOCAL_DIR="${LOCAL_DIR:-/opt/mahj-backups}"
COMPOSE="${COMPOSE:-docker compose -f docker-compose.yml -f docker-compose.prod.yml}"
LOCAL_RETENTION_MIN="${LOCAL_RETENTION_MIN:-720}"     # 12h
REMOTE_RETENTION_MIN="${REMOTE_RETENTION_MIN:-4320}"  # 3 days

: "${DB_NAME:?DB_NAME unset in .env}"
: "${DB_USER:?DB_USER unset in .env}"
: "${DB_PASSWORD:?DB_PASSWORD unset in .env}"
: "${REMOTE:?REMOTE unset in scripts/backup_db.env}"
: "${SSH_KEY:?SSH_KEY unset in scripts/backup_db.env}"

mkdir -p "$LOCAL_DIR"
stamp="$(date -u +%Y%m%dT%H%M%SZ)"
final="$LOCAL_DIR/mahj_${stamp}.dump"
tmp="$LOCAL_DIR/.mahj_${stamp}.partial"

# --- 1+2. dump atomically ----------------------------------------------------
# Write to a .partial first; only promote to the real name if pg_dump exits 0,
# so a failed/half-written dump is never mistaken for a good backup.
cleanup() { rm -f "$tmp"; }
trap cleanup EXIT

# -e PGPASSWORD makes this work regardless of the container's pg_hba.
$COMPOSE exec -T -e PGPASSWORD="$DB_PASSWORD" db \
  pg_dump -U "$DB_USER" -d "$DB_NAME" -Fc > "$tmp"

# pg_dump custom-format dumps start with the magic bytes "PGDMP"; a 0-byte or
# truncated file means the dump failed even if the pipe didn't.
[ -s "$tmp" ] || { echo "backup_db: dump is empty — aborting" >&2; exit 1; }
head -c 5 "$tmp" | grep -q "PGDMP" || {
  echo "backup_db: dump missing PGDMP header — aborting" >&2; exit 1; }

mv "$tmp" "$final"
trap - EXIT
echo "backup_db: wrote $final ($(du -h "$final" | cut -f1))"

# --- 3. push off-host --------------------------------------------------------
SSH_OPTS="ssh -i $SSH_KEY -o StrictHostKeyChecking=accept-new -o BatchMode=yes"
rsync -az -e "$SSH_OPTS" "$final" "$REMOTE/"
echo "backup_db: rsynced to $REMOTE/"

# --- 4. prune ----------------------------------------------------------------
find "$LOCAL_DIR" -maxdepth 1 -name 'mahj_*.dump' -mmin "+$LOCAL_RETENTION_MIN" -delete
# Remote prune: split user@host from path, run find over SSH.
remote_host="${REMOTE%%:*}"
remote_path="${REMOTE#*:}"
$SSH_OPTS "$remote_host" \
  "find '$remote_path' -maxdepth 1 -name 'mahj_*.dump' -mmin +$REMOTE_RETENTION_MIN -delete" \
  || echo "backup_db: remote prune skipped (non-fatal)" >&2

echo "backup_db: done"
