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
scan queue), **scan_worker** and **restore_worker**. Named volumes persist
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

Access is **per-tenant** (see `docs/data-model.md` → *Access control*). The
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

**Standalone build** (`docs/STANDALONE.md`): single-tenant and single-operator —
it auto-creates a superuser on first launch, so it works out of the box with no
membership setup.

## Updating

```bash
git pull
docker compose -f docker-compose.yml -f docker-compose.prod.yml up -d --build web
```

Migrations run on the new container's start; no manual `migrate` step.

## Backups & restore

`scripts/backup_db.sh` dumps the database and (optionally) ships it to a remote
host; `scripts/install_backup_cron.sh` schedules it. Restore either from the
admin console (**Administration → Database restore**) or with
`scripts/restore_db.sh`. See [scripts/DB_RESTORE.md](../scripts/DB_RESTORE.md)
and, for the emergency venue-laptop instance, [STANDALONE.md](STANDALONE.md) +
[scripts/LAPTOP_FAILOVER.md](../scripts/LAPTOP_FAILOVER.md).
