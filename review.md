# OEMC 2026 — Deployment & Event-Day Review

Consolidated review covering (1) deploy/infra hygiene for the prod stack, (2) live-event
resilience under the real load pattern — ~10 scorers writing in parallel + parallel scan
uploads, hundreds of viewers on `/`, projector screens at `/1 /2 …` that nobody can easily
refresh, and one operator driving displays + the ceremony — and (3) an independent pre-event
hardening pass that re-derived the risks from scratch. The three originally-separate documents
are merged here into one.

Prod stack (`docker compose -f docker-compose.yml -f docker-compose.prod.yml up --build -d`)
brings up **db** (postgres), **redis**, **redis_bus** (noeviction, channel layer + scan
queue), **pgbouncer**, **web** (gunicorn/uvicorn, prod settings), **scan_worker** (×2, OCR
queue consumers), and **nginx**. `certbot` is profile-gated and does not run.

**Only open issues are listed below.** Resolved items have been removed; see git history.
Among the resolved: the unauthenticated `counter_start` timer (now server-authoritative,
display-op gated), silently-stale projectors on WS drop (auto-reconnect + disconnect banner),
`detailed_scores` write-on-read + N+1 + caching, `create_hand_points` optimistic-concurrency,
splitting the durable channel-layer/scan bus onto a `noeviction` Redis, the scan-queue rework,
broadcast resilience, ceremony-screen hardening, per-player/team modal caching, dependency
pinning, the nginx cold-start healthcheck, sessions in Postgres + a Redis eviction policy,
the hung-but-not-crashed web container (the healthcheck now does a real curl-over-socket
liveness probe instead of stat-ing the socket, so a wedged gunicorn surfaces as `(unhealthy)`
for an operator to restart), and — most recently — the projector CDN dependency (jQuery /
Bootstrap / Alpine / Chart.js are now **vendored into `mahj/static/`** and served from the
content-hashed `/static/`), the duplicate-`Hand` scoring-corruption risk (now a DB
`UniqueConstraint`), the scores-screen QR (generated locally with `segno`, no external service),
the `display_schedule.html` mixed-content load, and the floating dependency tags. The `I#` /
`S#` / 🟡 IDs are carried over from the original reviews so they stay stable. **No 🔴 Critical
or 🟠 Important items remain open.**

---

## A. The three hard invariants — verified

### Invariant 1 (counter never stops/resets except by admin) — HOLDS, with one caveat
Traced every read/write:
- **Write** is only `set_counter()` ([helpers.py:21](mahj/views/helpers.py#L21)), called only
  from `counter_start()` ([admin_views.py:447](mahj/views/admin_views.py#L447)) behind
  `request.method=='POST' and is_display_op(...)` ([admin_views.py:440](mahj/views/admin_views.py#L440)).
  No other code path writes `Variable.counter`. A spectator GET/console cannot perturb it (403).
- The stored value is an **absolute epoch-ms gong moment**, not a tick count. `set_counter`
  uses `.update()` (no signals) and busts only the `variables:` cache. Worker recycle, deploy,
  Redis restart, page reload, broadcast storm — none touch the DB row, so the value is
  invariant under all of them. ✅
- **Client is a pure renderer**: [display_counter.html:112-118](mahj/templates/mahj/display_counter.html#L112)
  computes everything from `goTime` + a server-time offset, refreshed from every server reply
  ([:176](mahj/templates/mahj/display_counter.html#L176)). On load it immediately re-fetches the
  authoritative value via `$.post("counter_start")` ([:186](mahj/templates/mahj/display_counter.html#L186)),
  which reads the DB directly through `get_counter` ([helpers.py:16](mahj/views/helpers.py#L16)).
  No client tick ever writes back. ✅
- **Caveat:** the *initial* server render reads `variables.counter` from the **cached** Variable
  ([display.py:84](mahj/views/display.py#L84) → `get_variables`, 300 s TTL). `set_counter` deletes
  that cache key, so it's correct after a start/stop, and the client overwrites it within one RTT
  regardless. Not a defect — just noting the DB is the source of truth, the template value is
  cosmetic.

### Invariant 2 (displays never die silently) — HOLDS
The reconnect/backoff/heartbeat/banner machinery in
[display_socket.js](mahj/static/js/display_socket.js) covers WS drop, half-open sockets, and
server restart: capped jittered backoff reconnect, ping/pong half-open watchdog,
reload-on-reconnect to resync, and a default-on disconnect banner after a 4 s grace. **And it now
reliably runs**, because every display template loads jQuery/Bootstrap/Alpine/Chart.js from local
`/static/` rather than a third-party CDN — a CDN/DNS/captive-portal failure can no longer prevent
the banner code from executing. (The scores screens' QR is likewise generated locally via `segno`,
so a projector with no internet still shows a scannable code.)

### Invariant 3 (no silent write loss / disconnect) — HOLDS
- `update_hand_points` is version-checked, returns **409** on conflict
  ([score_entry.py:193-214](mahj/views/score_entry.py#L193)); `create_hand_points` bumps
  `version=F('version')+1` atomically ([:155](mahj/views/score_entry.py#L155)). A losing writer
  is told. ✅
- Channel layer + scan queue on a **noeviction** Redis ([docker-compose.yml:41](docker-compose.yml#L41))
  so a live socket's group membership can't be evicted. ✅
- Broadcasts are best-effort *after* the committed DB write ([signals.py:59-78](mahj/signals.py#L59)),
  so a Redis blip can't 500 a committed score. A missed broadcast resyncs on the next
  reconnect/refresh. ✅
- First-touch row creation is now concurrency-safe: the `Hand` unique constraint makes
  `get_or_create`/`.save()` atomic, so two scorers/scanners opening the same fresh table can no
  longer create duplicate double-counting rows. (Residual: the scan write paths still use
  last-write-wins rather than the 409 guard — see 🟡-7.)

---

## Architecture & the two critical lifecycles

- Django + Channels (ASGI), gunicorn + UvicornWorker (8 workers), nginx in front
  (microcache on `/`, 20 s, cookie-independent). Postgres via pgbouncer (transaction pool,
  `CONN_MAX_AGE=0`). `redis` = LRU cache (`allkeys-lru`, 256 mb); `redis_bus` = channel layer
  + scan queue (`noeviction`). Two `scan_worker` replicas consume a Redis FIFO for OCR.
- **Counter** = `Variable.counter` (BigInt, persisted in Postgres), an **absolute epoch-ms
  "gong moment"** (round start). `≤0` = stopped. Reads (`get_counter`) hit the DB directly;
  writes go through `set_counter` (`.update()` + busts the `variables` cache). Server-
  authoritative + time-based ⇒ a reload/restart/reconnect recomputes from the persisted value
  and cannot perturb it.
- **Display socket** ([display_socket.js](mahj/static/js/display_socket.js)): capped jittered
  backoff reconnect, ping/pong half-open watchdog, reload-on-reconnect to resync, default-on
  disconnect banner. `TenantConsumer` joins `leaderboard_*` + `display_*`. Broadcasts are
  best-effort and swallow Redis errors so a messaging blip never 500s a committed write
  ([signals.py:77](mahj/signals.py#L77)).
- **Web liveness:** the prod healthcheck curls the gunicorn socket
  ([docker-compose.prod.yml:34-39](docker-compose.prod.yml#L34)), so a *wedged* (not crashed)
  gunicorn — socket still bound, workers not answering — surfaces as `(unhealthy)` in `docker ps`.
  By design there is **no automatic watchdog**: `restart: unless-stopped` only fires on process
  exit, so an operator restarts a wedged web by hand (an operator is watching the screens live).
  If you want hands-off recovery, an autoheal sidecar (`willfarrell/autoheal`) is a ~6-line compose
  addition — deliberately declined here.

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

### 🟡-5. `scan_worker` loop doesn't guard `dequeue()` → a `redis_bus` restart crashes the worker
**Where:** [scan_worker.py:39-43](mahj/management/commands/scan_worker.py#L39) — only `_process`
is wrapped in try/except ([:54](mahj/management/commands/scan_worker.py#L54)); `scan_queue.dequeue`
→ `blpop` ([scan_queue.py:69](mahj/scan_queue.py#L69)) is not. A `redis_bus` restart raises
`ConnectionError` out of the loop, the command exits, and the container restarts.
**Impact:** recoverable (`restart: unless-stopped`, [docker-compose.yml:98](docker-compose.yml#L98)),
but in-flight scans become `expired` and the worker flaps for a few seconds. Low risk because
`scan_status` already surfaces `expired` to the scorer ([scan.py:140-143](mahj/views/scan.py#L140)).
**Fix:** wrap the `dequeue` call in `try/except redis.RedisError: time.sleep(1); continue` so the
worker rides out a bus blip without exiting.

### 🟡-6. Channel layer has no `capacity`/`expiry` tuning
**Where:** [base.py:91-98](apps/settings/base.py#L91) — `CHANNEL_LAYERS` sets only `hosts`.
`channels_redis` default capacity is 100 msgs/channel. A wedged/slow display socket can backfill
its channel; `group_send` then raises `ChannelFull`, which `_broadcast` swallows
([signals.py:77](mahj/signals.py#L77)) → that one screen misses an update and resyncs on its next
heartbeat/reconnect. Not dangerous, but set `'capacity': 300, 'expiry': 10` explicitly so a slow
screen drops *stale* frames fast rather than holding 100 old ones.

### 🟡-7. `scan_prefill` / `validate_score_sheet` write without the version guard
`validate_score_sheet` / `scan_prefill` use `get_or_create` + a full `.save()` (no `version`
handling) — the same class as the now-fixed `create_hand_points`
([scan.py:334-342](mahj/views/scan.py#L334), [score_entry.py:248-253](mahj/views/score_entry.py#L248)).
Lower risk because these run off the scan path rather than under concurrent per-cell editing, but
a scan that lands while a scorer is mid-edit can clobber the cell with no 409. Bring them onto the
`.filter(...).update(version=F('version')+1, …)` pattern if touched. (The `Hand` unique constraint
is now in place, so duplicate-row corruption is no longer the concern here — only last-write-wins.)

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

- Counter is genuinely server-authoritative and bulletproof against restart/reload/storm (§A-1).
- WS reconnect/backoff/heartbeat/banner logic in `display_socket.js` is solid, and now always
  runs (libraries served locally).
- Clean settings split (`base`/`dev`/`prod`/`test`) with secrets read from env and
  `SECRET_KEY`/`ALLOWED_HOSTS` failing loudly when unset in prod. Sensible prod hardening: HSTS,
  secure cookies, `SECURE_SSL_REDIRECT`, `SECURE_PROXY_SSL_HEADER`, `CSRF_TRUSTED_ORIGINS`.
- `CONN_MAX_AGE = 0` with pgbouncer in transaction-pool mode is the correct combination for ASGI
  workers, and the pgbouncer tuning is thoughtful.
- Optimistic concurrency on `Hand` (`version` + `F('version')+1`, 409 on conflict) plus the new
  `unique_hand_per_cell` constraint make multi-scorer editing both conflict-aware and
  corruption-proof.
- nginx `/` microcache (20 s) absorbs the spectator crowd: it ignores the app's `Vary: Cookie`,
  so every non-staff viewer shares one cached entry (~1 origin compute / 20 s,
  `proxy_cache_lock`, `use_stale`), while staff bypass via the `auth` cookie. The heavy
  per-player / per-team stats modals (not on `/`) are app-cached in Django, keyed by a
  `leaderboard_gen` counter that every real write bumps — so they bust on a write, not just TTL.
  The `/` desktop view fetches positions/hands once and shares them (no N+1 in the hot path).
- Scan-sheet OCR runs off the request path in dedicated `scan_worker` processes consuming a Redis
  queue, so a burst of parallel scans can't starve the web workers.
- noeviction bus / LRU cache split, Postgres sessions, so a Redis restart doesn't log staff out,
  can't fail writes by filling up, and can't silently evict a live socket's group membership.
- The web healthcheck curls the gunicorn socket, so a wedged (not crashed) gunicorn surfaces as
  `(unhealthy)` for an operator to restart (see Architecture).
- Real test coverage: auth/CSRF on the scorer endpoints, golden scoring tests, and the
  `locustfile.py` is **well past** a basic plan — `DisplayUser` (persistent WS, ping/pong RTT,
  missed-broadcast tally), `ScannerUser`, `ScorerUser` (409 path), `SpectatorUser`,
  `OrganizerUser`. The last run (2000 users, 15 min) reported **0 missed broadcasts** and only
  `X-Cache: EXPIRED` "failures" (benign stale-while-revalidate).

---

## B. Pre-event test plan (run today, against a STAGING copy only)

> Run everything against a staging copy, never prod mid-setup.

**1. CDN-failure drill (deploy verification — do this first).** The libraries are vendored, but the
vendored assets only ship with a rebuilt image. On a test projector machine, block the CDNs at the
OS/hosts level or DevTools:
`ajax.googleapis.com unpkg.com cdn.jsdelivr.net maxcdn.bootstrapcdn.com code.jquery.com`.
Open `/1` (counter), a scores screen, the schedule screen, and `/`.
**PASS:** every screen renders from local assets and the QR on the scores screens still shows; on a
WS drop the red banner still appears. **Run this against the redeployed image** to confirm the
vendored assets are actually in the served container (the dev container has static baked in, not
mounted).

**2. Load `/` + modals (locust — already built).**
`locust -f locustfile.py --headless -u 300 -r 20 -t 15m --host https://staging…`
(the committed run used 2000 users — keep that as the stretch run). **PASS:** `/` mostly
`X-Cache: HIT`; `details_player`/`details_team`/`detailed_scores` p95 flat from 100→300 users;
"ws display broadcast" shows received≈expected, missed=0 (last run: 400 received, 0 missed).

**3. Query profiling.** `test_queries.sh` is a single-request query profiler (good for N+1 on
`desktop`). Add a query-count assertion; run cold vs warm to prove the HTML cache works.

**4. Scan burst + responsiveness.** Fire 5–10 concurrent `POST /scan` (real JPEGs) while locust
drives 200 spectators and a real projector is open. **PASS:** projector keeps ticking, `/`
sub-second, every job reaches `done`/`error` (none stuck `pending`/`expired`),
`redis-cli -h redis_bus llen scan:queue` drains to 0.

**5. Concurrency / duplicate-Hand regression check.** Script two parallel requests against a
*fresh* table: one `GET admin_scores_per_hand/<r>/<t>` and one `POST scan_prefill` for the same
`(r,t)`, fired together, looped ~50×. Then run the dup query (#7). **PASS:** zero duplicates — the
loser of the race gets an `IntegrityError` and re-fetches instead of inserting a second row.

**6. Chaos drills** (PASS = screens recover AND counter survives):
- `docker compose restart web` mid-round → counter resumes correct remaining; screens reconnect+reload.
- `docker compose restart redis` (LRU cache) → staff stay logged in (sessions in PG), displays
  reconnect, note `evicted_keys`.
- `docker compose restart redis_bus` → displays reconnect; **confirm scan_worker survives** (it
  currently flaps, 🟡-5) and drains the queue; in-flight scans show `expired` (recoverable).
- `docker compose restart pgbouncer` → writes pause then resume, no 500 storm.
- `docker kill` one `scan_worker` replica → other replica drains the queue.
- **Network-bounce a display 30 s** → red banner appears within ~5 s, then reconnect + reload.
- **Spectator console `fetch('counter_start?action=stop',{method:'POST'})`** → **403** (invariant 1).
- **Wedge web** (`docker pause web` ~40 s) → healthcheck goes `(unhealthy)` in `docker ps`, operator
  `restart web`, screens reconnect+reload, counter resumes correct (persisted in PG).

**7. Duplicate-Hand audit + soak.**
Audit query (the unique constraint is now in place, so this should always be empty; keep it as a
sanity check):
```sql
SELECT tenant_id, round_nb, table_nb, hand_nb, count(*)
FROM mahj_hand GROUP BY 1,2,3,4 HAVING count(*) > 1;
```
**PASS:** zero rows.
**Soak:** counter running + idle screens 4 h+. **PASS:** no drift/reset, web RSS flat well below the
`max_requests=50000` recycle, `evicted_keys` ~0 on `redis_bus`.

---

## C. Event-day runbook (one page)

**Golden rule:** never `git pull` / `--build` / `down` during the event. Deps are pinned; deploy
once, freeze. The frontend libraries are vendored locally — make sure the deploy you freeze is the
image that **contains the vendored assets** (run drill #1 against it before doors open).

**Monitor continuously:** one real projector + the counter on a screen you can see · `docker stats`
(web RSS/CPU) · `redis-cli -h redis_bus info stats | grep evicted_keys` (must stay ~0) · nginx
`X-Cache-Status` on `/`.

| Symptom | Look at | Recovery |
|---|---|---|
| **One projector frozen, no banner** | Did its JS load? (DevTools console: `$ is not defined` / Alpine missing) | JS is served from local `/static/`, so this should not recur. If it does, confirm the redeployed image actually contains the vendored assets, then F5. |
| Projector shows red "DISPLAY DISCONNECTED" banner | It's self-recovering | Leave it; it reconnects+reloads. Only F5 if banner persists >60 s. |
| All screens stale | `docker ps` (web `(unhealthy)`?), `redis-cli -h redis_bus ping` | `docker compose restart web` — **safe for counter** (persisted in PG); screens reconnect+reload. A *wedged* web (process up, not answering) shows `(unhealthy)` — that's the signal to restart. |
| Counter stopped/jumped | Was it an admin Start/Stop? | An admin re-issues Start from `/admin?page=display`. **Never "restart to fix" — restarts can't change it**; only an admin `action=start/stop` POST does. |
| Counter screen at 00:00:00 during a round | Its JS failed to load | F5; if it recurs, confirm the served image has the vendored libs (it should — they're local now). |
| Scorer "conflict" (409) | Expected on concurrent cell edits | Scorer reloads sheet, re-enters. Working as designed. |
| A score "doesn't add up" | `mahj_hand` duplicate audit (test plan #7) | Duplicate rows are now blocked by the unique constraint; if the audit is non-zero, investigate the aggregation rather than the rows. |
| Scans stuck "expired" | `scan_worker` logs; `redis-cli -h redis_bus llen scan:queue` | `docker compose restart scan_worker`; bus is noeviction so rising `evicted_keys` is only the LRU cache, not scans. |
| Site slow under crowd | nginx `X-Cache-Status`; `docker stats` web CPU | Confirm `/` is mostly HIT; modals are 30 s-cached — confirm they hit. |
| DB issues | `docker compose logs pgbouncer db` | `restart pgbouncer` first; `db` only if truly wedged. |

**Restart-without-stopping-the-counter:** `web`, `redis`, `redis_bus`, `pgbouncer`, `nginx`,
`scan_worker` can all be restarted freely — the counter is a Postgres-persisted absolute
timestamp and is unaffected. Only an admin Start/Stop changes it.

**Backups:** `pg_dump` hourly off-box; snapshot the `postgres_data` volume on a schedule.

---

## Suggested order today
1. **S3** slim the image / expand `.dockerignore` (biggest hygiene win; do off the event path).
2. **🟡-5** wrap `scan_worker` `dequeue` in a reconnect loop.
3. **🟡-6** set `capacity`/`expiry` on the channel layer.
4. Redeploy the image with the vendored assets, then run CDN-failure drill #1 against it.
5. Re-run chaos drills (#6) end to end.
