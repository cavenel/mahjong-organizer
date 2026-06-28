# Backup & disaster-recovery runbook

The cloud VPS (`mahj.ovh`) is the **only** authoritative scoring instance. The
venue laptop is an emergency fallback for a real internet/server outage, run on a
plain LAN IP, by hand. Scanning/OCR is unavailable while local (manual entry only).

**Golden rule:** exactly one instance accepts score writes at a time. A
Postgres restore replaces the whole database — it does not merge. So while you
are live on the laptop, **nobody scores on `mahj.ovh`**, or you get an
unmergeable split brain.

---

## Components

| Script | Runs on | What it does |
|---|---|---|
| `backup_db.sh` | VPS (+ laptop during outage) | `pg_dump` every 5 min → local dir + rsync off-host. Names dumps `mahj_<source>_<stamp>.dump`. |
| `install_backup_cron.sh` | VPS (+ laptop during outage) | Schedules `backup_db.sh` every 5 min. |
| `pull_backups.sh` | laptop | Mirrors the off-host dumps down to the laptop so restore works offline. |
| `restore_db.sh` | VPS or laptop | Restores a dump into the **live** DB (destructive). |
| `restore_test.sh` | VPS or laptop | Validates a dump into a throwaway DB (non-destructive). |

`scripts/backup_db.env` (gitignored) holds `REMOTE`, `SSH_KEY`, and
`BACKUP_SOURCE`. **Set `BACKUP_SOURCE` differently per box:** `cloud` on the VPS,
`venue` on the laptop — that's what keeps the two dump streams apart.

---

## One-time setup

### On the VPS (cloud, authoritative)
```bash
cp scripts/backup_db.env.example scripts/backup_db.env   # set REMOTE, SSH_KEY, BACKUP_SOURCE=cloud
ssh-keygen -t ed25519 -f ~/.ssh/mahj_backup -N ''        # if you have no key yet
ssh-copy-id -i ~/.ssh/mahj_backup.pub <remote>
scripts/backup_db.sh         # confirm one manual dump + rsync works
scripts/restore_test.sh      # rehearse: validate the dump in a scratch DB
scripts/install_backup_cron.sh   # schedule every 5 min
```

### On the venue laptop (cold standby)
```bash
git clone <repo> && cd <repo>
cp /path/to/vps/.env .env                # same DB creds; ANTHROPIC_API_KEY may be blank
#   ⚠️ edit .env: set LOCAL_TENANT=<live tournament subdomain, e.g. varberg>
cp scripts/backup_db.env.example scripts/backup_db.env   # same REMOTE/SSH_KEY, BACKUP_SOURCE=venue
docker compose build                     # pre-build the image so failover is fast
scripts/pull_backups.sh --install        # start mirroring dumps down every 5 min
```
> The laptop uses the **dev** compose (plain `docker compose`, no `-prod.yml`):
> serves on `http://<laptop-ip>:8000`, `DEBUG=True`, `ALLOWED_HOSTS=['*']`, no
> forced HTTPS. Because of that, `restore_db.sh` and `backup_db.sh` on the laptop
> must override the stack — set `COMPOSE="docker compose"` in its `backup_db.env`.
>
> **Tenant:** the app picks the tenant from the request's subdomain, which a bare
> LAN IP doesn't have. `LOCAL_TENANT=<subdomain>` in the laptop's `.env` forces
> every request to the live tenant (DEBUG-gated; ignored by the prod cloud). Get
> the live subdomain from the cloud (Django admin → Tenants, or the URL you
> normally run on, e.g. `varberg` from `varberg.mahj.ovh`).

Rehearse before the event: bring the laptop stack up, restore the latest pulled
dump, and confirm you can **log in and submit a score** at `http://<laptop-ip>:8000`
from another device. (CSRF over a plain HTTP IP is the one thing worth verifying.)

---

## DISASTER → go local

When the VPS or the venue internet is gone and you need to keep scoring:

```bash
# on the laptop
docker compose up -d --build                 # dev stack on :8000
COMPOSE="docker compose" scripts/restore_db.sh   # pick the newest mahj_cloud_*.dump
```
Then announce the temporary address: **`http://<laptop-ip>:8000`** (find it with
`hostname -I`). Make sure **nobody keeps scoring on `mahj.ovh`**.

The laptop's own cron now writes `mahj_venue_*.dump` — these hold the live data.

---

## RECOVERY → fail back to cloud

Once `mahj.ovh` is reachable again and you're ready to move the venue's data back:

**If internet is back** (laptop can rsync):
```bash
# on the laptop — push its venue dumps off-host
scripts/backup_db.sh
# on the VPS — restore the newest VENUE dump into the live cloud DB
scripts/restore_db.sh --remote        # pick the newest mahj_venue_*.dump
```

**If there's still no internet** (move the file by USB/LAN):
```bash
# copy the newest mahj_venue_*.dump from the laptop to the VPS, then:
scripts/restore_db.sh /path/to/mahj_venue_<stamp>.dump
```

`restore_db.sh` stops the writers, drops/recreates the DB, restores, and brings
the stack back (web re-runs migrations on boot). The cloud is authoritative again
— point everyone back at `https://mahj.ovh`.

---

## Why the dump names matter

`mahj_cloud_*.dump` vs `mahj_venue_*.dump` is the whole trick. During an outage
the VPS (if alive) keeps writing **stale** `cloud` dumps while the laptop writes
**live** `venue` dumps into the same remote folder. On failback you pick the
newest `venue` dump and ignore the rest. Pruning is also per-source, so neither
box ever deletes the other's dumps.
