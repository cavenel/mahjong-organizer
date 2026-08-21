# Deployment

Production runs as a Docker Compose stack behind nginx with TLS. This guide
covers a single-host deployment; see [configuration.md](configuration.md) for
every environment variable.

## Prerequisites

- A host with **Docker** and the **Docker Compose** plugin.
- A **domain** you control, with:
  - a **wildcard DNS** record `*.<BASE_DOMAIN>` (and `<BASE_DOMAIN>`) pointing at
    the host — tenants are served at `<subdomain>.<BASE_DOMAIN>`;
  - a **wildcard TLS certificate** for `<BASE_DOMAIN>` and `*.<BASE_DOMAIN>`.
- A filled-in `.env` (`cp .env.example .env`).

## The stack

`docker-compose.yml` defines the app services; `docker-compose.prod.yml` layers
on nginx, TLS and the production commands. Always deploy with both files:

```bash
docker compose -f docker-compose.yml -f docker-compose.prod.yml up -d --build
```

Services: **web** (gunicorn/ASGI), **nginx** (TLS termination + static),
**db** (PostgreSQL), **pgbouncer**, **redis** (cache), **redis_bus** (Channels +
scan queue) and **scan_worker**. Named volumes persist
`postgres_data`, `letsencrypt`, `static_files` and the redis data.

nginx renders its vhost from `nginx/mahjong.conf.template` at start, substituting
`${BASE_DOMAIN}` into `server_name` and the cert paths
(`/etc/letsencrypt/live/<BASE_DOMAIN>/`).

## TLS certificate (wildcard, DNS-01)

A wildcard cert requires a **DNS-01** challenge, so certbot needs your DNS
provider's plugin. The compose file ships a `certbot` service (profile `certbot`)
configured for one provider as an example — **swap the image and `--dns-*`
flags/credentials for your provider** (`certbot/dns-cloudflare`,
`certbot/dns-route53`, …). Issue once:

```bash
docker compose -f docker-compose.yml -f docker-compose.prod.yml \
  --profile certbot run --rm certbot certonly \
    --dns-<provider> --dns-<provider>-credentials /etc/letsencrypt/<provider>.ini \
    -d "$BASE_DOMAIN" -d "*.$BASE_DOMAIN" \
    --agree-tos -m you@example.org --non-interactive
```

Renewal: `... --profile certbot run --rm certbot renew --quiet` (cron it).

## First run

Migrations apply automatically on container start. Then:

```bash
# 1. Create the platform superuser (cross-tenant operator)
docker compose exec web python manage.py createsuperuser
```

Access is **per-tenant** (see [`docs/dev/data-model.md`](../dev/data-model.md) →
*Access control*). The
superuser bypasses membership, so they can:

2. **Create a tenant** — log in at `https://<BASE_DOMAIN>/admin` → **Administration
   → Tenants → Create** (name + subdomain). (The Django admin at `/admin_db/` also
   works.)
3. **Add that tenant's users** — from the Tenants list, click **Manage users →**
   (or open `https://<tenant>.<BASE_DOMAIN>/admin` → **Administration → User
   management**) and *Add a user*, ticking **Admin** for the tenant's first admin.
   Roles (Admin / Scorer / Display operator / Publisher) are stored as per-tenant
   `Membership` rows — there are no global Scorer/Display_op/Publisher groups.

From then on each tenant admin manages their own tenant's users. Open
`https://<tenant>.<BASE_DOMAIN>/admin`, then **Configuration → Tournament
settings** and **Import from template**. The **Dashboard** tracks setup and live
progress.

For a non-fresh install, the `0010_seed_memberships` migration best-effort maps
the old global roles onto memberships when exactly one tenant exists; with
several tenants it can't guess, so grant access manually with
`manage.py assign_membership <user> <subdomain> --roles=tenant_admin,scorer,…`.

**Standalone build** ([`STANDALONE.md`](STANDALONE.md)): single-tenant and single-operator —
it auto-creates a superuser on first launch, so it works out of the box with no
membership setup.

## Updating

```bash
git pull
docker compose -f docker-compose.yml -f docker-compose.prod.yml up -d --build web
```

Migrations run on the new container's start; no manual `migrate` step.

### Reclaiming build-cache disk

Every `--build` leaves layers in the builder cache, and they are never reclaimed on
their own. On a small VPS with `/var/lib/docker` on the root filesystem this is what
fills the disk — after a few dozen deploys the cache was 68 GB, none of it in use, and
`/` had 216 MB free. Docker's own disk accounting is the fastest way to see it:

```bash
docker system df                                  # RECLAIMABLE is the number that matters
docker builder prune -af --filter until=168h      # drop cache older than a week
docker image prune                                # drop untagged images from old builds
```

Worth running every month or so, or whenever the disk gets tight. The `until` filter
keeps the recent cache so the next deploy is still fast.

**Never `docker volume prune`.** `postgres_data` is a named volume, and pruning takes
the database with it. Use the backup/restore paths below instead.

## Backups & restore

Backups are **per tournament**, not per server, and they need no host scripts or
cron: every web publish uploads a full tournament dump — settings, players,
seating, all scores, published rounds, schedule, screens, the round timer — to
the tenant's publish target over SFTP, next to the static site.

- **Where they land**: a `mahj-backups/` directory in the SFTP user's **login
  directory** — never under the remote site path, so the dumps aren't
  web-fetchable (a dump holds every score, including a withheld final round the
  public site is still hiding). It is not derived from the site path at all: for a
  target like `public_html/2026` there is no way to tell the docroot from a
  subfolder of it, so anything derived could end up served. To put them elsewhere,
  set **Backup directory** on *Administration → Publish target*. The newest 20 per
  tenant are kept.
- **On demand**: *Administration → Backup & restore* downloads a dump any time.
  Tenants with no publish target configured have only this, so download one after
  each round.
- **To restore**: the same page — upload a dump and retype the subdomain. It
  replaces that tenant's whole tournament and leaves user accounts and the publish
  target alone. A dump restores into *any* tenant on *any* install running the
  same app version, which is what makes the venue-laptop fallback below work.

Because a dump is per tenant, restoring one never touches another tournament on
the same server. The database itself needs no application-level backup path: take
host-level snapshots of the `postgres_data` volume if you want whole-server
recovery.

**If the server is unreachable mid-tournament**, run the standalone build on a
venue laptop and restore the latest dump into it — see
[STANDALONE.md](STANDALONE.md). Publish from the laptop for the rest of the event,
then dump from the laptop and restore that back onto the server afterwards.
