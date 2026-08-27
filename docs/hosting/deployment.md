# Deployment

Production runs as a Docker Compose stack behind nginx with TLS, on a single
host. [configuration.md](configuration.md) lists every environment variable.

## Prerequisites

- A host with **Docker** and the **Docker Compose** plugin.
- A **domain** you control, with:
  - a **wildcard DNS** record `*.<BASE_DOMAIN>`, and `<BASE_DOMAIN>` itself,
    pointing at the host. Tournaments are served at `<subdomain>.<BASE_DOMAIN>`.
  - a **wildcard TLS certificate** for `<BASE_DOMAIN>` and `*.<BASE_DOMAIN>`.

## Get the code and the config

```bash
git clone https://github.com/cavenel/mahjong-organizer.git mahj && cd mahj
cp .env.example .env
```

In `.env`, set `DJANGO_SECRET_KEY`, `BASE_DOMAIN` and `DB_PASSWORD`. See
[configuration.md](configuration.md) for the rest.

## The stack

`docker-compose.yml` defines the app services. `docker-compose.prod.yml` adds
nginx, TLS and the production commands. Always deploy with both files:

```bash
docker volume create mahj_postgres_data          # once, before the first up
docker volume create mahj_letsencrypt            # once, before the first up
docker compose -f docker-compose.yml -f docker-compose.prod.yml up -d --build
```

The database lives in `mahj_postgres_data` and the TLS certificate in
`mahj_letsencrypt`. Both are declared **external** in the compose files, so no
compose command can delete them, including `docker compose down -v`. Create them
before the first `up`. Without them Compose stops with "external volume not
found" instead of creating throwaway ones.

The other named volumes (`static_files`, `certbot_www`, `nginx_cache` and the
redis data) are rebuilt on start. You can delete those freely.

The services are **web** (gunicorn on ASGI), **nginx** (TLS and static files),
**db** (PostgreSQL), **pgbouncer**, **redis** (cache), **redis_bus** (Channels
and the scan queue) and **scan_worker**.

nginx renders its vhost from `nginx/mahjong.conf.template` at start, substituting
`${BASE_DOMAIN}` into `server_name` and the cert paths
(`/etc/letsencrypt/live/<BASE_DOMAIN>/`).

## TLS certificate (wildcard, DNS-01)

A wildcard certificate needs a **DNS-01** challenge, so certbot needs your DNS
provider's plugin. The compose file ships a `certbot` service under the `certbot`
profile, set up for OVH as an example. **Swap the image and the `--dns-*` flags
and credentials for your own provider**, such as `certbot/dns-cloudflare` or
`certbot/dns-route53`.

The example service bind-mounts `./certbot/ovh.ini` for its API keys. Create that
file before the first run:

```bash
cp certbot/ovh.ini.example certbot/ovh.ini    # then fill in the keys
chmod 600 certbot/ovh.ini                     # it is gitignored
```

Without the file, Docker mounts an empty directory at that path and certbot fails
with a confusing credentials error. For another provider, also change the volume
line in `docker-compose.prod.yml` to that plugin's credentials file.

Issue the certificate once:

```bash
docker compose -f docker-compose.yml -f docker-compose.prod.yml \
  --profile certbot run --rm certbot certonly \
    --dns-<provider> --dns-<provider>-credentials /etc/letsencrypt/<provider>.ini \
    -d "$BASE_DOMAIN" -d "*.$BASE_DOMAIN" \
    --agree-tos -m you@example.org --non-interactive
```

To renew, run the same command with `renew --quiet` in place of `certonly` and
its flags. Put it in cron.

## First run

Migrations apply automatically when the containers start.

Create the platform superuser, who works across all tournaments:

```bash
docker compose exec web python manage.py createsuperuser
```

Access is granted per tournament. The superuser bypasses that, so they can set up
the first one. See [`docs/dev/access-control.md`](../dev/access-control.md).

1. **Create a tournament.** Log in at `https://<BASE_DOMAIN>/admin`, then
   **Administration → Tenants → Create**, with a name and a subdomain. The Django
   admin at `/admin_db/` also works.
2. **Add its users.** From the Tenants list, click **Manage users**. Or open
   `https://<subdomain>.<BASE_DOMAIN>/admin` and go to **Administration → User
   management**. Use *Add a user* and tick **Admin** for the first one.

Roles are Admin, Scorer, Display operator and Publisher. They are stored as
per-tournament `Membership` rows. There are no global role groups.

From then on each tournament admin manages their own users. The tournament itself
is set up under **Setup → Tournament settings** and **Excel import / export**.
The **Setup checklist** shows what is still missing, and the Run **Dashboard**
shows live progress.

Accounts that predate memberships, with the old global `is_staff` and group
roles, get no access automatically. Grant it with:

```bash
manage.py assign_membership <user> <subdomain> --roles=tenant_admin,scorer
```

The standalone build needs none of this. It serves one tournament and creates its
superuser on first launch. See [`standalone.md`](standalone.md).

## Updating

```bash
git pull
docker compose -f docker-compose.yml -f docker-compose.prod.yml up -d --build web
```

Migrations run when the new container starts. There is no manual `migrate` step.

**If a migration fails**, the `web` container exits before gunicorn starts and
nginx answers 502. Read the error with `docker compose -f … logs web`. The site
stays on the old code until the new container is healthy, so the quickest
recovery is to go back: `git checkout <previous commit>` and re-run the
`up -d --build web` line above. The tournaments' data is untouched, because
PostgreSQL rolls a failed migration back. Fix the migration from a checkout, not
on the server.

### Reclaiming build-cache disk

Every `--build` leaves layers in the builder cache, and nothing reclaims them.
On a small VPS with `/var/lib/docker` on the root filesystem, this is what fills
the disk. After a few dozen deploys the cache here was 68 GB, none of it in use,
with 216 MB free on `/`.

```bash
docker system df                                  # RECLAIMABLE is the number that matters
docker builder prune -af --filter until=168h      # drop cache older than a week
docker image prune                                # drop untagged images from old builds
```

Run this every month or so, or whenever the disk gets tight. The `until` filter
keeps the recent cache, so the next deploy is still fast.

### Never run docker volume prune

This also means `docker system prune --volumes`.

`docker compose down -v` leaves `mahj_postgres_data` and `mahj_letsencrypt`
alone, because they are declared external. `volume prune` does not. It deletes
any volume that no running container uses, and nothing in this stack copies the
database anywhere. Stop the stack, prune, and every tournament is gone. Use the
backup and restore paths below instead.

Losing the certificate volume is only an outage. nginx refuses to start until the
certificate is issued again. See *TLS certificate*.

## Backups and restore

Backups are per tournament, not per server, and they need no host scripts and no
cron. Every web publish uploads a full tournament dump to that tournament's
publish target over SFTP, next to the static site. A dump holds the settings,
players, seating, all scores, published rounds, the schedule, the screens and the
round timer.

**Where they land.** In a `backup/` directory under the target's remote site
path, next to an `.htaccess` that lets Apache list it. The newest 20 per
tournament are kept.

That directory is inside the served tree on purpose, so the dumps have a plain
URL. *Backup & restore* links to it, and an organizer can fetch a backup in a
browser without an SFTP client. The cost is that anyone who finds the address can
read a dump, which holds every entered score, including rounds not yet published
and the ceremony state. Dumps carry no credentials, because the publish target
and the scanning config are excluded. If that trade is wrong for your event,
publish to a site that is itself access-controlled.

**On demand.** *Administration → Backup & restore* downloads a dump at any time.
**A tournament with no publish target and no downloaded dump has no backup at
all.** Nothing else in the stack copies its data anywhere. For such a tournament,
download a dump after every round.

**To restore.** On the same page, upload a dump and retype the subdomain. It
replaces that tournament completely and leaves the user accounts and the publish
target alone. A dump restores into any tournament on any install running the same
app version, which is what makes the venue-laptop fallback work.

Restoring one tournament never touches another on the same server. The database
needs no backup path of its own. Take host-level snapshots of the
`mahj_postgres_data` volume if you want whole-server recovery.

**If the server is unreachable mid-tournament**, run the standalone build on a
venue laptop and restore the latest dump into it. See
[standalone.md](standalone.md).

### Starting over from a dump after the server died

When the database is gone, from a pruned volume or a dead disk, and all you have
is a dump per tournament:

1. Bring the stack up on a fresh database. Run `docker volume create
   mahj_postgres_data` if it no longer exists, then `docker compose -f … up -d`.
   Migrations apply on start. Create the platform superuser again, as in
   *First run*.
2. **Recreate each tournament with its old subdomain**, under
   *Administration → Tenants → Create*. The subdomain is what the advertised
   spectator URL, the QR codes and the projector addresses point at. A different
   one breaks all of them.
3. On `https://<subdomain>.<BASE_DOMAIN>/admin`, open *Administration → Backup &
   restore*, upload that tournament's latest dump and retype the subdomain. Fetch
   the dump from the `backup/` folder of its published site, over https or SFTP,
   or from a copy downloaded earlier.
4. **Re-enter what a dump does not carry.** The user accounts and memberships
   under *Administration → User management*, and the publish target with its
   credentials under *Administration → Publish target*.
5. Publish once from each restored tournament so the static site matches.
