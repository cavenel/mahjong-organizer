# Configuration

All configuration is via environment variables, read from a `.env` file in
production. Copy [`.env.example`](../../.env.example) to `.env` and fill it in —
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
| `ALLOWED_HOSTS` | `BASE_DOMAIN` + subdomains | Allowed `Host` values (comma-separated). Setting it **replaces** the default, so include `example.org,.example.org` yourself or every tenant subdomain answers 400. Extra hosts are not added to `CSRF_TRUSTED_ORIGINS` (derived from `BASE_DOMAIN` only), so logins POSTed from an extra host fail with a 403. |
| `VENUE_TZ` | `UTC` | IANA timezone for the projector clock only; storage stays UTC. |
| `SESAME_MAX_AGE` | `2592000` (30 days) | Validity in seconds of the passwordless login links issued from *User management*; there is no per-link TTL. |
| `LOCAL_TENANT` | *(unset)* | **Standalone build only** — pins every request to one tenant (that build is single-tenant, reached at localhost). Ignored elsewhere. |

The spectator URL shown on screens (QR) and player cards is configured
**per tenant** in the admin (Administration → Publish target → *Spectator URL*),
defaulting to `<tenant>.<BASE_DOMAIN>` — see below.

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
rotating that key invalidates them: the target then shows as not configured
until you re-enter the credentials on this page. A **Test connection** button
verifies it. Leave a tenant's target disabled to not publish it; a multi-tenant instance
publishes each enabled tenant to its own host.

The same page has a **Spectator URL** field — the address advertised on screens
(QR) and printed cards. Leave it blank to use `<tenant>.<BASE_DOMAIN>`, or set it
to where the static site is published so spectators land on the published site.

## Score-sheet scanning (per tenant)

Photo scanning of paper score sheets is configured per tenant. There is no
platform-wide API key and no env var for it. Each tournament enters its own key
under **Setup → Scanning** and is billed for its own scans. One photo costs about
a cent, so a 16-table, 9-round event is roughly $2. A tournament with no key
cannot scan: the QR code disappears from its score sheets and `/scan` says
scanning is not switched on. Manual entry is unaffected.

The same page holds that tournament's score sheet: a picture of its blank sheet,
plus the box around the score columns. Photos are matched against it. There is no
default sheet, so a tournament that has not uploaded one does not scan either. If
the app guessed a sheet, every photo would fail to match and players would be
asked to take the photo again. An example sheet ships in
`mahj/static/template.jpg` for anyone who does not have one, and the page links
to it. The **Test alignment** button checks the setup for free, and **Test scan**
does one real read.

Keys are encrypted at rest with Fernet, keyed off `DJANGO_SECRET_KEY`. Rotating
that key makes every tenant's key unreadable, and each one has to be entered
again. The page says so rather than showing the key as configured.

Be clear about what that encryption covers. It protects a leaked database dump.
It does not protect against anyone who has the app host, because
`DJANGO_SECRET_KEY` is in `.env` on that same host. As operator you hold other
organisations' API credentials. If the host is compromised, tell every tenant to
rotate their key. Recommend that tenants use a separate key with a spend limit.

Two things worth knowing before an event:

- The scan page needs no login, and its per-device rate limit stops working if
  the cache is unavailable. A burst of abuse is billed to the tenant, not to the
  host. There is no monthly cap.
- The four `scan_worker` replicas share one FIFO queue. A tenant whose key is
  rate-limited can delay another tenant's scans. This did not happen when every
  tenant shared one host key.

## Backups

Nothing to configure here. Backups are per-tenant tournament dumps written by the
app itself and uploaded to the tenant's publish target on every publish; the
target (including an optional separate **Backup directory**) is stored per tenant
in the database, edited on *Administration → Publish target*. See
[deployment.md](deployment.md#backups--restore).
