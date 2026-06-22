# OEMC 2026 — Deployment & Event-Day Review

Two reviews merged into one: (1) deploy/infra hygiene for the prod stack, and (2) live-event
resilience under the real load pattern — ~10 scorers writing in parallel + parallel scan
uploads, hundreds of viewers on `/`, projector screens at `/1 /2 …` that nobody can easily
refresh, and one operator driving displays + the ceremony.

Prod stack (`docker compose -f docker-compose.yml -f docker-compose.prod.yml up --build -d`)
brings up **db** (postgres), **redis**, **redis_bus** (noeviction, channel layer + scan
queue), **pgbouncer**, **web** (gunicorn/uvicorn, prod settings), **scan_worker** (×2, OCR
queue consumers), and **nginx**. `certbot` is profile-gated and does not run.

**Only open issues are listed below.** Resolved items have been removed; see git history.
Among them: the unauthenticated `counter_start` timer (now server-authoritative, display-op
gated), silently-stale projectors on WS drop (auto-reconnect + disconnect banner),
`detailed_scores` write-on-read + N+1 + caching, `create_hand_points` optimistic-concurrency,
splitting the durable channel-layer/scan bus onto a `noeviction` Redis, the scan-queue rework,
broadcast resilience, ceremony-screen hardening, per-player/team modal caching, dependency
pinning, the nginx cold-start healthcheck, the dead EMA endpoint, the display index guard,
moving sessions to Postgres + a Redis eviction policy, the dev-only tenant-resolution hacks,
the OCR `Winner`-nullability schema, dropping the dead WhiteNoise middleware in prod, making
the `/` microcache cookie-independent + tuning gunicorn `max_requests`, the `manage.py`
dead-code block, the `models.py` bare `except:`/`Tenant.get_default_pk` bugs, the orphaned
`apps/static/admin` copy, and the orphaned legacy `timer_options` view/template, and the hung-but-not-crashed web
container (the healthcheck now does a real curl-over-socket liveness probe instead of just
stat-ing the socket, so a wedged gunicorn surfaces as `(unhealthy)` for an operator to
restart). The `I#` / `S#` IDs are carried over from the original reviews so
they stay stable. No 🔴 Critical or 🟠 Important items remain open.

The three hard invariants all hold:
1. **Counter never stops/resets except by explicit admin action** — server-authoritative
   absolute epoch-ms "gong moment" persisted in Postgres; screens are pure renderers.
2. **Displays never die silently** — capped jittered reconnect + reload-on-reconnect, plus a
   built-in disconnect banner on every projector after a 4s grace.
3. **No silent disconnection / write loss** — `update_hand_points` and `create_hand_points`
   are both version-checked/monotonic (409 on conflict), and the channel layer lives on a
   `noeviction` Redis so group memberships can't be evicted out from under an open socket.

---

## Architecture & the two critical lifecycles

- Django + Channels (ASGI), gunicorn + UvicornWorker (8 workers), nginx in front
  (microcache on `/`, 20s, cookie-independent). Postgres via pgbouncer (transaction pool,
  `CONN_MAX_AGE=0`). `redis` = LRU cache (`allkeys-lru`, 256mb); `redis_bus` = channel layer
  + scan queue (`noeviction`). Two `scan_worker` replicas consume a Redis FIFO for OCR.
- **Counter** = `Variable.counter` (BigInt, persisted in Postgres), an **absolute epoch-ms
  "gong moment"** (round start). `≤0` = stopped. Reads (`get_counter`) hit the DB directly;
  writes go through `set_counter` (`.update()` + busts the `variables` cache). Server-
  authoritative + time-based ⇒ a reload/restart/reconnect recomputes from the persisted value
  and cannot perturb it.
- **Display socket** (`mahj/static/js/display_socket.js`): capped jittered backoff reconnect,
  ping/pong half-open watchdog, reload-on-reconnect to resync, default-on disconnect banner.
  `TenantConsumer` joins `leaderboard_*` + `display_*`. Broadcasts are best-effort and swallow
  Redis errors so a messaging blip never 500s a committed write (`signals.py:57`).

---

## 🟡 Secondary — quality, robustness, cost

### S3. Image is fat and keeps build tools at runtime
Single-stage build keeps `gcc` and `libpq-dev` in the final image, and `COPY . .` pulls
in `captures/`, `template_old.jpg` (2 MB), `plugins/` (a whole WordPress plugin with
hundreds of flag SVGs), `MahjongTemplate.xlsx`, tests, `*.sh`, and `TODO.md` —
none needed at runtime. `.dockerignore` only excludes `.venv/.env/databases/git`.
**Fix:** multi-stage build (wheels in builder, slim runtime), switch to `psycopg[binary]`
or drop `libpq-dev`/`gcc` from the final stage, and expand `.dockerignore`
(`captures/`, `plugins/`, `*_old.*`, `test_*.sh`, `*.md`, `mahj/tests/`,
`mahj/static/*.xlsx`).

### Scan write paths lack the version guard
`validate_score_sheet` / `scan_prefill` use `get_or_create` + a full `.save()` (no `version`
handling) — the same class of issue as the now-fixed `create_hand_points`, but lower risk
since these run off the scan path rather than under concurrent per-cell editing. Bring them
in line with `update_hand_points` (`.filter(...).update(version=F('version')+1, …)`) if
touched.

---

## ⚪ Nitpicks / housekeeping

- **Stale local data:** the root `captures/` dir sits in the working tree. It's
  gitignored/dockerignored, so it doesn't ship, but it's clutter. Untracked → deletion is
  irreversible and it may hold local scan data, so not auto-removed. (The 10 stale
  `databases/*.sqlite3` files have been removed — the app runs on Postgres.)
- **Loose files at root:** the load-test (`locustfile.py`) and query-profiling
  (`test_queries.sh`, `test_queries_2.sh`) tooling is kept local-only and gitignored —
  not committed and not shipped in the image (see also S3's `.dockerignore`).

---

## What's good (worth keeping)

- Clean settings split (`base`/`dev`/`prod`/`test`) with secrets read from env and
  `SECRET_KEY`/`ALLOWED_HOSTS` failing loudly when unset in prod.
- Sensible prod hardening already in place: HSTS, secure cookies, `SECURE_SSL_REDIRECT`,
  `SECURE_PROXY_SSL_HEADER`, `CSRF_TRUSTED_ORIGINS`.
- `CONN_MAX_AGE = 0` with pgbouncer in transaction-pool mode is the correct combination
  for ASGI workers, and the pgbouncer tuning is thoughtful.
- Optimistic concurrency on `Hand` (`version` + `F('version')+1`, 409 on conflict) is a
  nice touch for multi-scorer editing.
- nginx microcache on `/` (20s) absorbs the spectator crowd: it ignores the app's
  `Vary: Cookie`, so every non-staff viewer — cookie or not — shares one cached entry
  (~1 origin compute / 20s), while staff bypass via the `auth` cookie. The heavy per-player
  / per-team stats modals (not on `/`, so the microcache misses them) are app-cached in
  Django, keyed by a `leaderboard_gen` counter that every real write bumps — so they bust
  on a write rather than relying on TTL alone.
- Scan-sheet OCR runs off the request path in dedicated `scan_worker` processes consuming a
  Redis queue, so a burst of parallel scans can't starve the web workers.
- Auth/sessions live in Postgres and everything in the LRU Redis is disposable (regenerable
  cache), while the durable bus (channel layer + scan queue) sits on a separate `noeviction`
  Redis — so a Redis restart doesn't log staff out, can't fail writes by filling up, and can't
  silently evict a live socket's group membership.
- Real test coverage for auth/CSRF on the scorer endpoints and golden scoring tests.

---

## Pre-event test plan (run today, against a STAGING copy only)

1. **Load `/` + modals (locust).** `locustfile.py` is solid (cookie-carrying spectators,
   fixed scorer count, 409 path). Add: (a) a `DisplayUser` that opens
   `wss://…/ws/display/<sub>/`, pings every 25s, counts missed broadcasts (~20 of them);
   (b) a `ScannerUser` that POSTs a real JPEG to `/scan` and polls `/scan_status`. Run
   `locust --headless -u 300 -r 20 -t 15m --host https://staging…`. **Pass:** `/` mostly
   `X-Cache: HIT`; modal p95 flat 100→300 users; `/detailed_scores_*` p95 flat too.
2. **`test_queries.sh`** is a single-request query profiler (good for N+1 on `desktop`). Add a
   query-count assertion; run cold vs warm to prove the HTML cache works.
3. **Scan burst + responsiveness.** 10 simultaneous `/scan` uploads while locust drives 200
   spectators and a real projector is open. **Pass:** projector keeps updating, `/` sub-second,
   all scans reach `done`/`error` (none `expired`).
4. **Chaos drills** (pass = screens recover AND counter survives): `restart web` mid-round →
   counter resumes correct remaining; `restart redis` → staff stay logged in, displays
   reconnect, note `evicted_keys`; `restart pgbouncer` → writes pause/resume, no 500 storm;
   `docker kill` one `scan_worker` → in-flight scan `expired` (recoverable), other replica
   drains; **network bounce a display 30s** → banner appears, then reconnect+reload;
   **counter-stop attempt** from a spectator console → 403; **wedge web** (e.g.
   `docker pause web` ~40s) → healthcheck goes `(unhealthy)` in `docker ps`, operator
   `restart web`, screens reconnect+reload, counter resumes correct (persisted in PG).
5. **Soak (4h+).** Counter running + idle screens overnight. **Pass:** no drift/reset, no
   silent freeze, web RSS flat before the daily `max_requests=50000` recycle, `evicted_keys` ~0.

---

## Event-day runbook (one page)

**Golden rule:** never `git pull` / `--build` / `down` during the event. Deps are pinned;
deploy once, freeze.

| Symptom | Look at | Recovery |
|---|---|---|
| A projector frozen / wrong | Disconnect banner | Self-reloads; else F5. Counter screen self-heals on its own clock. |
| All screens stale | `docker ps` (web `(unhealthy)`?), `redis-cli info clients` | `restart web` — **safe for the counter** (persisted in PG); screens reconnect+reload. A *wedged* web (process up, not answering) shows `(unhealthy)` in `docker ps` — the healthcheck curls the socket, so this is the signal to `restart web`. |
| Counter stopped/jumped | Was it an admin action? | Admin re-issues Start from `/admin?page=display`. **Don't restart to "fix" it** — restarts don't reset it; only an admin `action=start/stop` does. |
| Scorer "conflict" (409) | Expected on concurrent cell edits | Scorer reloads the sheet, re-enters. Working as designed. |
| Scans "expired" | `scan_worker` logs; `redis-cli llen scan:queue`; `info stats \| grep evicted` | `restart scan_worker`; the bus is `noeviction`, so rising `evicted_keys` is only the LRU cache. |
| Site slow under crowd | nginx `X-Cache-Status`; `docker stats` web CPU | Confirm `/` HITs; per-table detail modal is now cached (30s) — confirm it's hitting. |
| DB issues | `logs pgbouncer db` | `restart pgbouncer` first; `db` only if wedged. |

**Monitor:** `docker stats` (web RSS/CPU), `redis-cli info stats | grep evicted_keys`, nginx
`X-Cache-Status`, and one real projector + the counter on a screen you can see.
**Backups:** `pg_dump` hourly off-box (snapshot the `postgres_data` volume on a schedule).
**Restart without stopping the counter:** `web`, `redis`, `redis_bus`, `pgbouncer`, `nginx`,
`scan_worker` can all be restarted freely — the counter is a Postgres-persisted absolute
timestamp and is unaffected. The only things that change it are an admin Start/Reset or a
`counter_start?action=` write (gated to display operators).

---

## Suggested order of operations

1. **S3** (slim image / expand `.dockerignore`).
