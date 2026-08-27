# Configuration

The app is configured with environment variables, read from a `.env` file in
production. Copy [`.env.example`](../../.env.example) to `.env` and fill it in.
That file is the commented source of truth for every variable.

`manage.py` defaults to `apps.settings.dev`. Production needs
`DJANGO_SETTINGS_MODULE=apps.settings.prod`.

## Required in production

| Variable | Purpose |
|----------|---------|
| `DJANGO_SECRET_KEY` | Django secret. Generate one with `python -c "from django.core.management.utils import get_random_secret_key; print(get_random_secret_key())"`. |
| `BASE_DOMAIN` | The apex domain the instance is served under. Tournaments live at `<subdomain>.<BASE_DOMAIN>`. It sets the CSRF trusted origins, the nginx `server_name` and TLS cert path, the default `ALLOWED_HOSTS`, and the advertised spectator URL. Needs a wildcard DNS record (`*.<BASE_DOMAIN>`) and a wildcard TLS certificate. |
| `DB_NAME` `DB_USER` `DB_PASSWORD` `DB_HOST` `DB_PORT` | PostgreSQL connection. Django connects through PgBouncer (`DB_HOST=pgbouncer`), which connects to the `db` service. |

## Optional

| Variable | Default | Purpose |
|----------|---------|---------|
| `ALLOWED_HOSTS` | `BASE_DOMAIN` and its subdomains | Allowed `Host` values, comma-separated. Setting it **replaces** the default, so include `example.org,.example.org` yourself or every tournament subdomain answers 400. Extra hosts are not added to `CSRF_TRUSTED_ORIGINS`, which comes from `BASE_DOMAIN` alone, so a login posted from an extra host fails with a 403. |
| `VENUE_TZ` | `UTC` | IANA timezone for the projector clock. Storage stays UTC. |
| `SESAME_MAX_AGE` | `2592000`, 30 days | How long the passwordless login links from *User management* stay valid, in seconds. There is no per-link expiry. |

## PgBouncer

`PGBOUNCER_POOL_MODE` (`transaction`), `PGBOUNCER_MAX_CLIENT_CONN` (`300`) and
`PGBOUNCER_DEFAULT_POOL_SIZE` (`25`). The defaults are set in
`docker-compose.yml`.

## Redis

| Variable | Purpose |
|----------|---------|
| `REDIS_URL` | The Django cache. `allkeys-lru`, and disposable. |
| `REDIS_BUS_URL` | The Channels layer and the scan queue. This must be a separate `noeviction` instance, so the cache's LRU cannot evict a live socket's group membership. Falls back to `REDIS_URL` for a single-instance dev setup. |

## Publishing the spectator site

This is set per tournament in the admin, not by environment variable. On each
round publish, the public page can be rendered to static files and uploaded to a
plain web host over SFTP. **Publish to web** runs it again on demand.

Staff set a target under **Administration → Publish target**: host, port, user,
remote path, and either a password or a private key. An optional host-key line
pins the host. **Test connection** checks it. Leave a tournament's target
disabled and it is not published. On a multi-tenant instance each enabled
tournament publishes to its own host.

The same page holds the **Spectator URL**, which is the address shown on the
screens as a QR code and printed on the player cards. Leave it blank to use
`<subdomain>.<BASE_DOMAIN>`, or set it to the published site so spectators land
there.

Credentials are encrypted at rest with Fernet, keyed off `DJANGO_SECRET_KEY`.
Rotating that key makes them unreadable. The target then shows as not configured
and the credentials have to be entered again.

## Score-sheet scanning

This is also per tournament. There is no platform-wide API key and no
environment variable for it. Each tournament enters its own key under
**Setup → Scanning** and is billed for its own scans. One photo costs about a
cent, so a 16-table, 9-round event is roughly $2. A tournament with no key
cannot scan. The QR code disappears from its score sheets and `/scan` says
scanning is not switched on. Manual entry still works.

The same page holds that tournament's score sheet: a picture of its blank sheet,
and the box around the score columns. Photos are matched against it. There is no
default sheet, so a tournament that has not uploaded one does not scan either. A
guessed sheet would fail to match every photo and players would be asked to
retake it. An example sheet ships in `mahj/static/template.jpg` for anyone who
does not have one, and the page links to it. **Test alignment** checks the setup
for free. **Test scan** does one real read.

Keys are encrypted the same way as the publish credentials, so rotating
`DJANGO_SECRET_KEY` means every tournament has to enter its key again. The page
says so instead of showing the key as configured.

Know what that encryption does and does not cover. It protects a leaked database
dump. It does not protect against anyone who has the app host, because
`DJANGO_SECRET_KEY` is in `.env` on that same host. As the operator you hold
other organisations' API credentials. If the host is compromised, tell every
tournament to rotate their key. Recommend that they use a separate key with a
spend limit.

Two more things to know before an event:

- The scan page needs no login, and its per-device rate limit stops working if
  the cache is unavailable. A burst of abuse is billed to the tournament, not to
  you. There is no monthly cap.
- The four `scan_worker` replicas share one queue. A tournament whose key is
  rate-limited can delay another tournament's scans.

## Backups

There is nothing to configure. See
[deployment.md](deployment.md#backups-and-restore).
