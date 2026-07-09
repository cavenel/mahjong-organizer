# Configuration

All configuration is via environment variables, read from a `.env` file in
production. Copy [`.env.example`](../.env.example) to `.env` and fill it in —
that file is the authoritative, commented source; this page groups the same
variables by purpose and marks what is required.

`manage.py` defaults to `apps.settings.dev` for local work. **Production
requires `DJANGO_SETTINGS_MODULE=apps.settings.prod`** plus the variables below.

## Core (required in production)

| Variable | Purpose |
|----------|---------|
| `DJANGO_SECRET_KEY` | Django secret. Generate with `python -c "from django.core.management.utils import get_random_secret_key; print(get_random_secret_key())"`. |
| `BASE_DOMAIN` | Apex domain the instance is served under. Tenants live at `<subdomain>.<BASE_DOMAIN>`. Drives CSRF trusted origins, the nginx vhost `server_name` + TLS cert path, default `ALLOWED_HOSTS`, and the advertised spectator URL. Needs a wildcard DNS record (`*.<BASE_DOMAIN>`) and a wildcard TLS cert. |
| `DB_NAME` `DB_USER` `DB_PASSWORD` `DB_HOST` `DB_PORT` | PostgreSQL connection. Django connects through PgBouncer (`DB_HOST=pgbouncer`), which connects to the `db` service. |

## Optional

| Variable | Default | Purpose |
|----------|---------|---------|
| `ALLOWED_HOSTS` | `BASE_DOMAIN` + subdomains | Extra allowed `Host` values (comma-separated). |
| `ANTHROPIC_API_KEY` | *(unset → scanning off)* | Enables score-sheet photo OCR. Manual entry is unaffected. |
| `VENUE_TZ` | `UTC` | IANA timezone for the projector clock only; storage stays UTC. |
| `PUBLIC_SITE_URL` | `<tenant>.<BASE_DOMAIN>` | Spectator URL shown on screens (QR) and player cards. Set for the standalone build. |
| `LOCAL_TENANT` | *(unset)* | **Dev/standalone only** — pin every request to one tenant (venue-laptop failover). Ignored in prod. |

## PgBouncer tuning (optional)

`PGBOUNCER_POOL_MODE` (`transaction`), `PGBOUNCER_MAX_CLIENT_CONN` (`300`),
`PGBOUNCER_DEFAULT_POOL_SIZE` (`25`). Defaults are applied in
`docker-compose.yml`.

## Redis

| Variable | Purpose |
|----------|---------|
| `REDIS_URL` | Django cache (`allkeys-lru`, disposable). |
| `REDIS_BUS_URL` | Channels layer + scan queue — a **separate** `noeviction` instance so the cache's LRU can't evict a live socket's group membership. Falls back to `REDIS_URL` for single-instance dev. |

## Static spectator publishing (optional)

On each round publish, the public page can be rendered to static files and
SFTP-uploaded to a plain web host (also re-runnable from the admin console via
**Publish to web**).

It's configured **per tenant**, not via env. Staff set a target under
**Administration → Publish target**: host, port, user, remote path, and a
password *or* private key (plus an optional host-key line to pin the host).
Credentials are Fernet-encrypted at rest, keyed off `DJANGO_SECRET_KEY` — so
rotating that key means re-entering them. A **Test connection** button verifies
it. Leave a tenant's target disabled to not publish it; a multi-tenant instance
publishes each enabled tenant to its own host.

## In-app database restore (optional)

The admin console's **Database restore** page reads dumps from a host directory.
See [scripts/DB_RESTORE.md](../scripts/DB_RESTORE.md).

`MAHJ_BACKUP_DIR` (`/opt/mahj-backups`), `MAHJ_WORKER_UID`/`MAHJ_WORKER_GID`
(owner of that dir + the SSH key), and — for the **Pull from remote** button —
`BACKUP_REMOTE`, `BACKUP_SSH_PORT`, `MAHJ_BACKUP_SSH_KEY` (a group-readable
*copy* of the backup key). Leave `BACKUP_REMOTE` unset to hide pull; local
restore still works.
