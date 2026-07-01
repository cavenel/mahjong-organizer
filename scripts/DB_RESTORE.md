# In-app database restore (admin console)

Browser-driven restore/pull from the admin console, complementing the host
scripts (`backup_db.sh`, `restore_db.sh`, `pull_backups.sh`). It lets a staffer:

1. **Pull** the off-host dumps down to this box,
2. **List** the local dumps (grouped by origin — `cloud` / `venue`),
3. **Restore** a chosen dump into the live DB.

## Why a worker, not the web process

`restore_db.sh` runs on the **host** and uses `docker compose stop web … / up`, so
it can't run inside the `web` container — a request that stops `web` kills itself
mid-restore. Instead the destructive work runs in a dedicated **`restore_worker`**
service (a twin of `scan_worker`) that consumes a `redis_bus` queue. The web admin
only *enqueues* a job and polls for status.

## How a restore runs (no Docker socket, no container bounce)

The worker pauses the connection pool instead of stopping containers:

1. `PAUSE` pgbouncer — clients **stall** (they don't error); redis_bus-backed
   WebSocket displays keep ticking since they don't touch the DB.
2. Connect **directly to the `db` service** (bypassing the paused pooler) on the
   maintenance `postgres` DB: terminate stragglers, `DROP DATABASE … WITH (FORCE)`,
   `CREATE DATABASE … OWNER`.
3. `pg_restore --no-owner --no-acl` straight into `db`.
4. `manage.py migrate --noinput` with `DB_HOST=db` (pooler still paused) — catches
   an older dump up to current schema, as the `web` boot command does.
5. `RESUME` pgbouncer (in a `finally`, so a failure never leaves the pool paused).

A **pull** job just rsyncs the remote down into `/backups` (non-destructive).

## Security

- Page + every mutating endpoint require **staff + fresh reauth** (the existing
  600s sudo window), identical to User management.
- **Typed-confirm**: a restore is rejected unless the operator types the DB name
  into the confirm box (mirrors `restore_db.sh`'s typed-name prompt).
- **No free-text paths**: only a basename chosen from the server-listed `/backups`
  files is accepted; validated for the `PGDMP` header before enqueue and again in
  the worker.
- The worker has **no Docker socket** — only DB-network + `/backups` access.
- ⚠️ Restoring **wipes the live DB**. Golden rule still applies: only one instance
  accepts writes at a time (see `RUNBOOK.md`).

## Moving parts

| Piece | Path |
|---|---|
| Queue (redis_bus) | `mahj/restore_queue.py` |
| Worker | `mahj/management/commands/restore_worker.py` |
| Views (list/pull/run/status) | `mahj/views/restore_admin.py` |
| Page + sidebar | `mahj/templates/mahj/admin_database_restore.html`, `admin.html` |
| pgbouncer admin + worker service | `docker-compose.yml`, `docker-compose.prod.yml` |
| `pg_restore`/`rsync`/`ssh` in image | `Dockerfile` |

## Config (compose env / `.env`)

- `MAHJ_BACKUP_DIR` — host backups dir (default `/opt/mahj-backups`), mounted
  `:/backups` (rw in the worker, ro in `web`).
- `BACKUP_REMOTE`, `BACKUP_SSH_PORT`, `MAHJ_BACKUP_SSH_KEY` — for the pull job;
  mirror the values in `scripts/backup_db.env`. Pull is disabled if unset.

### Permissions (pull only)

`restore_worker` runs as the image's `app` user (uid 1000 — ssh needs a real
passwd entry, so we don't run it as an arbitrary uid). The backups dir and pull
key are usually owned by the *host* backup user (e.g. `cavenel`, uid 1001), so for
**pull** the worker must be able to read the key and write the dir without us
chowning those away from the backup cron. Grant access via the owning **group**:

- `MAHJ_WORKER_GID` in `.env` = the gid that owns `MAHJ_BACKUP_DIR` + the key
  (`id -g`). Compose adds it to the worker via `group_add`.
- The dir just needs to be group-writable: `chmod 775 "$MAHJ_BACKUP_DIR"`.
- **Do NOT chmod the cron's SSH key group-readable.** `backup_db.sh` runs as the
  key's owner, and OpenSSH refuses a private key that's group/world-readable when
  you own it ("permissions too open") — that silently breaks the off-host backup.
  Instead give the container a **separate, group-readable copy**, and point
  `MAHJ_BACKUP_SSH_KEY` at the copy (the cron key stays 600):
  ```sh
  cp "$KEY" "$KEY.container" && chmod 640 "$KEY.container"   # KEY stays 600
  # .env: MAHJ_BACKUP_SSH_KEY=/…/mahj_backup.container
  ```
  (ssh doesn't apply the strict-perms check to the copy because the container
  runs as `app`, which isn't the copy's owner — it reads it via the shared group.)

Restore and listing need none of this — dumps are world-readable (664) and the
restore path is pure DB/network. Only the pull's key-read + dir-write need the
group. Pulled copies land as `app`-owned 644 (readable by `web`'s listing and by
the cron's prune, which owns the dir).

## Verify

See the top-level plan; in short: bring the stack up, confirm `restore_worker`
is idle, pull, then restore an older dump (type the DB name to confirm) and watch
the phase advance to `done` with row counts — an in-flight request should stall,
not 5xx, and a bad dump must still leave pgbouncer **resumed**.
