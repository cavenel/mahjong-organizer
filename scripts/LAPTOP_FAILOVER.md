# Venue laptop — rehearsal & emergency card

Concrete commands for the venue laptop, end to end. Do **Part 1 (setup) and
Part 2 (rehearsal) before the event, with internet**; Parts 3–4 are the emergency
reference. Replace the `<…>` placeholders.

**Golden rule:** only one instance accepts score writes. While the laptop is
live, **nobody scores on `your-domain`** — a Postgres restore replaces the whole DB,
it does not merge.

---

## PART 1 — One-time setup (do early, with internet)

```bash
# 1. Tools (Debian/Ubuntu): git, rsync, flock — plus Docker Engine + compose plugin
sudo apt update && sudo apt install -y git rsync util-linux
docker compose version    # the "docker compose" plugin (v2+), not legacy docker-compose

# 2. Clone
git clone <REPO_URL> mahj && cd mahj

# 3. Environment — copy the VPS .env verbatim (SECRET_KEY + DB creds),
#    then add the tenant line. (scp over port 19022, or a USB stick.)
scp -P 19022 <YOU>@<VPS>:/path/to/mahj/.env .env
printf '\nLOCAL_TENANT=<LIVE_SUBDOMAIN>\n' >> .env     # e.g. myevent

# 4. Backup config for THIS box (venue source, dev stack, home dir, SSH port)
cat > scripts/backup_db.env <<EOF
REMOTE="<BACKUP_USER>@<SECOND_SERVER>:/srv/mahj-backups"
SSH_KEY="$HOME/.ssh/mahj_backup"
SSH_PORT="19022"
BACKUP_SOURCE="venue"
COMPOSE="docker compose"
LOCAL_DIR="$HOME/mahj-backups"
EOF

# 5. SSH key the laptop uses to pull dumps (copy the same key, lock it down)
scp -P 19022 <YOU>@<VPS>:~/.ssh/mahj_backup ~/.ssh/mahj_backup
chmod 600 ~/.ssh/mahj_backup

# 6. Pre-build the image so failover is fast later
docker compose build

# 7. Mirror dumps down every 5 min, and prove one pull works now
scripts/pull_backups.sh            # should report "synced … (N dumps held)"
scripts/pull_backups.sh --install  # schedule it
```

Why these matter: plain `docker compose` (no `-prod.yml`) runs **dev** settings →
port 8000 on the LAN, `DEBUG=True`, `ALLOWED_HOSTS=['*']`, no forced HTTPS.
`LOCAL_TENANT` only takes effect under DEBUG, and points the laptop at the live
tenant despite the bare LAN IP. `LOCAL_DIR` in your home dir avoids needing
`sudo` for `/opt`.

---

## PART 2 — Rehearsal (before the event, with internet)

Prove failover actually works end to end.

```bash
# 1. Bring up the local stack (dev compose → port 8000 on the LAN)
docker compose up -d --build

# 2. Restore the newest pulled dump into it (pick a mahj_cloud_* dump)
COMPOSE="docker compose" scripts/restore_db.sh
#    type the DB name (mahj) to confirm; watch the row counts it prints

# 3. Find the laptop's LAN address + open the firewall if one is active
hostname -I                                          # e.g. 192.168.1.50
sudo ufw allow 8000/proto tcp 2>/dev/null || true
```

Then **from another device on the same network**, open `http://<laptop-ip>:8000`
and verify all four:

- [ ] Leaderboard shows the **real tournament** (not `myevent`/empty) → `LOCAL_TENANT` works
- [ ] You can **log in**
- [ ] You can **submit a score** → CSRF works over plain HTTP/IP (the one unknown)
- [ ] A score change shows up live on the display page → WebSockets work

Tear down when satisfied: `docker compose down` (keeps the DB volume;
`down -v` wipes it).

> Expected-degraded offline: score-sheet **scanning** (cloud OCR + camera needs
> HTTPS) and ceremony **iframe previews**. Manual entry works.

---

## PART 3 — DURING A DISASTER (go local)

When the VPS or the venue internet is gone and you need to keep scoring:

```bash
cd ~/mahj
docker compose up -d --build
COMPOSE="docker compose" scripts/restore_db.sh       # pick newest mahj_cloud_* dump
hostname -I                                          # announce http://<ip>:8000
```

Announce the temporary address **`http://<laptop-ip>:8000`** and make sure
**nobody keeps scoring on `your-domain`**. The laptop's cron now writes
`mahj_venue_*` dumps — those hold the live data.

---

## PART 4 — FAILBACK to cloud (when the VPS is back)

**If internet is back** (laptop can rsync):

```bash
# on the LAPTOP — push its venue dumps off-host
scripts/backup_db.sh
# on the VPS — restore the newest VENUE dump into the live cloud DB
scripts/restore_db.sh --remote        # pick newest mahj_venue_*
```

**If there's still no internet** (move the file by USB/LAN):

```bash
# copy the newest mahj_venue_*.dump from the laptop to the VPS, then on the VPS:
scripts/restore_db.sh /path/to/mahj_venue_<stamp>.dump
```

`restore_db.sh` stops the writers, drops/recreates the DB, restores, and brings
the stack back (web re-runs migrations on boot). The cloud is authoritative again
— point everyone back at `https://your-domain`.
