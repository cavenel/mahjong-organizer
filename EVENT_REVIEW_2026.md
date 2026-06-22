# OEMC 2026 — Pre-event hardening review (independent pass)

Reviewed the day before the live event. This is an **independent re-derivation**, not a
re-read of `review.md`. Where I agree with `review.md` I say so briefly; the bulk below is
risks `review.md` **missed or that have regressed**. Findings are ranked by what will bite us
live, each with `file:line`, the failure scenario, the user-visible impact, and the minimal fix.

The three hard invariants are addressed explicitly in §A. The headline is that **invariant 2
is currently violated by a dependency `review.md` never examined**: every projector screen
loads its JavaScript from public CDNs, and when that fails the screen freezes *without* the
disconnect banner ever appearing.

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
- **The real threat to invariant 1 is finding 🔴-1**: if jQuery fails to load, the `$.post` on
  line 186 throws, the counter never renders or reconnects, and the screen sits frozen at
  `00:00:00`. The counter *data* is safe in Postgres; the counter *screen* is not.

### Invariant 2 (displays never die silently) — **VIOLATED** (see 🔴-1)
The reconnect/backoff/heartbeat/banner machinery in
[display_socket.js](mahj/static/js/display_socket.js) is genuinely good and covers WS drop,
half-open sockets, and server restart. **But it only fires if the script runs at all.** Every
display template loads jQuery (and Bootstrap/Alpine) from a third-party CDN *before* the inline
script that calls `connectDisplaySocket`. A CDN/DNS/captive-portal failure means the banner code
never executes → blank or frozen screen, no error shown. This is the exact failure mode the
invariant forbids.

### Invariant 3 (no silent write loss / disconnect) — HOLDS for scorer cell edits
- `update_hand_points` is version-checked, returns **409** on conflict
  ([score_entry.py:193-214](mahj/views/score_entry.py#L193)); `create_hand_points` bumps
  `version=F('version')+1` atomically ([:155](mahj/views/score_entry.py#L155)). A losing writer
  is told. ✅
- Channel layer + scan queue on a **noeviction** Redis ([docker-compose.yml:41](docker-compose.yml#L41))
  so a live socket's group membership can't be evicted. ✅
- Broadcasts are best-effort *after* the committed DB write ([signals.py:59-78](mahj/signals.py#L59)),
  so a Redis blip can't 500 a committed score. A missed broadcast resyncs on the next
  reconnect/refresh. ✅
- **Gap:** the *first-touch* row creation paths are **not** concurrency-safe (finding 🟠-2):
  two scorers/scanners opening the same table at once can create **duplicate Hand rows**, which
  silently double-count. This is write *corruption*, not write *loss*, but it's silent.

---

## 🔴 Critical

### 🔴-1. Every projector/display screen loads its JS from public CDNs → silent dead screen on any network hiccup
**Where:**
- [display_counter.html:11](mahj/templates/mahj/display_counter.html#L11) — `ajax.googleapis.com` jQuery
- [display_black.html:8](mahj/templates/mahj/display_black.html#L8),
  [display_no_screen.html:8](mahj/templates/mahj/display_no_screen.html#L8),
  [display_scores_per_player_table.html:11-12](mahj/templates/mahj/display_scores_per_player_table.html#L11),
  [display_scores_per_player_total_only.html:10-11](mahj/templates/mahj/display_scores_per_player_total_only.html#L10),
  [display_schedule.html:10-13](mahj/templates/mahj/display_schedule.html#L10) — jQuery + Bootstrap CDN
- [desktop.html:12](mahj/templates/mahj/desktop.html#L12) — Alpine from `unpkg.com` (spectator `/`)
- [modal_details_player.html:11-12](mahj/templates/mahj/modal_details_player.html#L11),
  [modal_details_team.html:10](mahj/templates/mahj/modal_details_team.html#L10) — Chart.js (`jsdelivr`) + Alpine

**Scenario:** The venue Wi-Fi has a captive portal, a corporate firewall blocks `unpkg`/`googleapis`,
DNS hiccups, or any of those CDNs has a 5-second blip *at the moment a projector loads or
auto-reloads*. The library never arrives.

**User-visible impact (Critical because it breaks invariant 2):**
- **Counter screen:** `$` is undefined → `$.post("counter_start")` on
  [display_counter.html:186](mahj/templates/mahj/display_counter.html#L186) throws → the inline
  script halts *before* `connectDisplaySocket` is ever called
  ([:197](mahj/templates/mahj/display_counter.html#L197)). Result: timer frozen at `00:00:00`,
  **no countdown, no gong, and crucially no disconnect banner** — the very safety net that's
  supposed to make a dead screen visible never initializes. Silent, frozen, wrong.
- **Spectator `/`:** the `<body>` carries `x-cloak` ([desktop.html:14](mahj/templates/mahj/desktop.html#L34))
  and `[x-cloak]{display:none}` ([:14](mahj/templates/mahj/desktop.html#L14)). Alpine removes
  `x-cloak` on init; if Alpine never loads, **the entire page body stays `display:none`** — hundreds
  of spectators get a blank white page.
- **Score/schedule screens:** jQuery-dependent rendering breaks the same way.

**Aggravating factor:** `unpkg.com/alpinejs@3.x.x` and `chart.js@3.9.1` are **floating/redirect
tags** resolved by the CDN at request time — you don't control which build loads, and `3.x.x`
adds an extra resolver round-trip that's the first thing to fail on a flaky link.

**Minimal fix (cheap, do today):** vendor all four libraries into `mahj/static/js/` (and the one
CSS into `mahj/static/css/`) and serve them via the existing nginx `/static/` (already
`immutable, 1y`, content-hashed by `CompressedManifestStaticFilesStorage`). Concretely:
```
mahj/static/js/jquery-3.7.1.min.js     # one version everywhere; retire 3.3.1 + 3.7.1 split
mahj/static/js/alpine-3.14.x.min.js
mahj/static/js/chart-3.9.1.min.js
mahj/static/js/bootstrap-3.3.7.min.js  # + bootstrap.min.css
```
then replace every CDN `<script src="https://…">` / `<link href="https://…">` with
`{% static '…' %}`. Run `collectstatic`. This also closes 🟠-3 (mixed content) for free.
**Verify:** load each projector page with DevTools "offline after first byte" / block
`*.googleapis.com,*.unpkg.com,*.jsdelivr.net,*.bootstrapcdn.com` → every screen must still
render and, on WS drop, must show the red banner.

---

## 🟠 Important

### 🟠-2. `Hand` has no unique constraint → concurrent first-touch creates duplicate rows → silent scoring corruption — ✅ FIXED
**Resolution:** Added `UniqueConstraint(tenant, round_nb, table_nb, hand_nb)` named
`unique_hand_per_cell` to `Hand.Meta` ([models.py:91](mahj/models.py#L91)) + migration
[0018_hand_unique_per_cell.py](mahj/migrations/0018_hand_unique_per_cell.py) (also drops the now-redundant
4-field non-unique index). Audit query returned **0 duplicate rows** before migrating; migration applied
clean on staging. `get_or_create` is now atomic (loser gets `IntegrityError` + re-fetch); the `.save()` /
`create_hand_points` paths now raise on a genuine double-insert instead of silently double-counting.

**Where:** [models.py:80-95](mahj/models.py#L80) — `Hand` declares only non-unique `indexes`,
**no** `unique_together`/`UniqueConstraint` on `(tenant, round_nb, table_nb, hand_nb)`.
(Contrast `PublishedRound` [models.py:164](mahj/models.py#L164) and `CeremonyState`
[models.py:190](mahj/models.py#L190), which *do* have unique constraints.)

Four paths create rows non-atomically:
- `admin_scores_per_hand` auto-creates the 16+1 rows on open via bare `.save()`
  ([score_entry.py:84-93](mahj/views/score_entry.py#L84))
- `create_hand_points` `.update()`-then-`.create()` ([score_entry.py:153-160](mahj/views/score_entry.py#L153))
- `scan_prefill` `get_or_create` ([scan.py:334](mahj/views/scan.py#L334))
- `validate_score_sheet` `get_or_create` ([score_entry.py:248](mahj/views/score_entry.py#L248))

**Scenario:** Table T's sheet has no rows yet. Scorer A opens `admin_scores_per_hand` for T while
Scorer B (or a `scan_prefill` from a phone photo) hits T at the same instant. Both read "row
missing", both INSERT. `get_or_create`/`.save()` are **not** atomic without a DB uniqueness
guarantee, so you now have two `hand_nb=5` rows for the same table.

**User-visible impact:** the scoring aggregation counts both rows → that hand's points are
**double-counted in the leaderboard**, silently. With ~10 scorers and a parallel scan workflow,
two people touching one table in its first seconds is plausible. Worst case it surfaces as a
score that "doesn't add up" mid-event with no obvious cause.

**Minimal fix:** add the constraint + migration; `get_or_create` then becomes atomic (loser gets
IntegrityError and re-fetches):
```python
class Meta:
    constraints = [
        models.UniqueConstraint(
            fields=['tenant', 'round_nb', 'table_nb', 'hand_nb'],
            name='unique_hand_per_cell'),
    ]
```
`makemigrations && migrate`. **Before migrating, dedupe** any existing duplicates (see test plan
#6) or the migration will fail — which is itself the cheapest way to find out whether you already
have corruption. **Verify:** the dup-detection query returns zero rows after the migration.

### 🟠-3. `display_schedule.html` loads Bootstrap JS over plain `http://` → blocked as mixed content
**Where:** [display_schedule.html:13](mahj/templates/mahj/display_schedule.html#L13) —
`<script src="http://maxcdn.bootstrapcdn.com/...">` on an HTTPS page.
**Impact:** browsers block active mixed content outright, so even *with* internet the schedule
screen's Bootstrap JS silently never loads. Subsumed by 🔴-1's fix (vendor locally), but flagged
separately because it fails even when the CDN is reachable.

### 🟠-4. Nothing auto-heals a *hung* (not crashed) web container — still open from `review.md` I-E
**Where:** [docker-compose.prod.yml:24-28](docker-compose.prod.yml#L24) — the web healthcheck only
tests `test -S /run/gunicorn/mahjong.sock` (socket *existence*). `restart: unless-stopped` recovers
a crash/OOM, but a wedged-but-alive gunicorn (all 8 workers stuck on a slow query, event loop
blocked) keeps the socket present, so nothing restarts it and nginx keeps proxying into the void.
**Impact:** every screen and `/` stalls; the counter screens self-heal on their own clock only if
their *socket* is fine. **Fix:** either a real HTTP healthcheck hitting a cheap `/healthz` plus an
autoheal sidecar (`willfarrell/autoheal`), or accept it **only** because an operator is watching a
live projector + the counter (the runbook assumes this). Given no debugging during the event, I'd
add the sidecar today — it's a 6-line compose addition.

---

## 🟡 Secondary

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
Same class as the (fixed) `create_hand_points`, noted in `review.md` and still true
([scan.py:334-342](mahj/views/scan.py#L334), [score_entry.py:248-253](mahj/views/score_entry.py#L248)).
Lower risk than 🟠-2 because these don't run under per-cell concurrent editing, but a scan that
lands while a scorer is mid-edit can clobber the cell with no 409. Bring them onto the
`.filter(...).update(version=F('version')+1, …)` pattern if touched. The 🟠-2 unique constraint is
the higher-leverage fix.

### 🟡-8. Floating dependency tags
`alpinejs@3.x.x` (×3) and `chart.js@3.9.1` resolve at the CDN. Pinning is moot once 🔴-1 vendors
them locally — just make sure you vendor a *specific* known-good build.

---

## What `review.md` got right (and is still true)
- Counter is genuinely server-authoritative and bulletproof against restart/reload/storm (§A-1).
- WS reconnect/backoff/heartbeat/banner logic in `display_socket.js` is solid *when it runs*.
- nginx `/` microcache (20 s, cookie-independent, `proxy_cache_lock`, `use_stale`) correctly
  shields the origin; the `desktop_html` + per-modal `leaderboard_gen`-keyed caches are sound;
  the `/` desktop view fetches positions/hands once and shares them (no N+1 in the hot path).
- noeviction bus / LRU cache split, Postgres sessions, `CONN_MAX_AGE=0` + pgbouncer transaction
  pool are all the right calls.
- The `locustfile.py` is **already well past** what `review.md`'s test plan asks for — it has
  `DisplayUser` (persistent WS, ping/pong RTT, missed-broadcast tally), `ScannerUser`,
  `ScorerUser` (409 path), `SpectatorUser`, `OrganizerUser`. The last run (2000 users, 15 min)
  reported **0 missed broadcasts** and only `X-Cache: EXPIRED` "failures" (benign
  stale-while-revalidate). Update `review.md`'s plan — those additions are done.

---

## B. Pre-event test plan (runnable today against STAGING)

> Run everything against a staging copy, never prod mid-setup.

**1. CDN-failure drill (covers 🔴-1 — do this first, it's the headline risk).**
On a test projector machine, block the CDNs at the OS/hosts level or DevTools:
`ajax.googleapis.com unpkg.com cdn.jsdelivr.net maxcdn.bootstrapcdn.com code.jquery.com`.
Open `/1` (counter), a scores screen, the schedule screen, and `/`.
**PASS:** every screen renders from local assets; on a WS drop the red banner still appears.
**FAIL (current state):** counter frozen at 00:00:00 with no banner; `/` blank. → ship the vendor fix, re-run.

**2. Load `/` + modals (locust — already built).**
`locust -f locustfile.py --headless -u 300 -r 20 -t 15m --host https://staging…`
(the committed run used 2000 users — keep that as the stretch run). **PASS:** `/` mostly
`X-Cache: HIT`; `details_player`/`details_team`/`detailed_scores` p95 flat from 100→300 users;
"ws display broadcast" shows received≈expected, missed=0 (last run: 400 received, 0 missed).

**3. Scan burst + responsiveness.** Fire 5–10 concurrent `POST /scan` (real JPEGs) while locust
drives 200 spectators and a real projector is open. **PASS:** projector keeps ticking, `/`
sub-second, every job reaches `done`/`error` (none stuck `pending`/`expired`),
`redis-cli -h redis_bus llen scan:queue` drains to 0.

**4. Concurrency / duplicate-Hand drill (covers 🟠-2).** Script two parallel requests against a
*fresh* table: one `GET admin_scores_per_hand/<r>/<t>` and one `POST scan_prefill` for the same
`(r,t)`, fired together, looped ~50×. Then run the dup query (#6). **PASS:** zero duplicates (after
the unique-constraint fix). **FAIL (current):** occasional duplicate `hand_nb` rows.

**5. Chaos drills** (PASS = screens recover AND counter survives):
- `docker compose restart web` mid-round → counter resumes correct remaining; screens reconnect+reload.
- `docker compose restart redis` (LRU cache) → staff stay logged in (sessions in PG), displays
  reconnect, note `evicted_keys`.
- `docker compose restart redis_bus` → displays reconnect; **confirm scan_worker survives** (it
  currently flaps, 🟡-5) and drains the queue; in-flight scans show `expired` (recoverable).
- `docker compose restart pgbouncer` → writes pause then resume, no 500 storm.
- `docker kill` one `scan_worker` replica → other replica drains the queue.
- **Network-bounce a display 30 s** → red banner appears within ~5 s, then reconnect + reload.
- **Spectator console `fetch('counter_start?action=stop',{method:'POST'})`** → **403** (invariant 1).
- **Hung-web drill (🟠-4):** `docker compose exec web kill -STOP 1`-equivalent (or pause the
  container) → confirm whether anything restarts it. Today: nothing does → decide on the sidecar.

**6. Duplicate-Hand audit + soak.**
Audit query (run now against prod data, before the event):
```sql
SELECT tenant_id, round_nb, table_nb, hand_nb, count(*)
FROM mahj_hand GROUP BY 1,2,3,4 HAVING count(*) > 1;
```
**PASS:** zero rows. If non-zero you already have corruption — dedupe before adding the constraint.
**Soak:** counter running + idle screens 4 h+. **PASS:** no drift/reset, web RSS flat well below the
`max_requests=50000` recycle, `evicted_keys` ~0 on `redis_bus`.

---

## C. Event-day runbook (one page)

**Golden rule:** never `git pull` / `--build` / `down` during the event. Deploy once, freeze.
Vendor the CDN libs (🔴-1) **before** doors open — it is the single highest-value change.

**Monitor continuously:** one real projector + the counter on a screen you can see · `docker stats`
(web RSS/CPU) · `redis-cli -h redis_bus info stats | grep evicted_keys` (must stay ~0) · nginx
`X-Cache-Status` on `/`.

| Symptom | Look at | Recovery |
|---|---|---|
| **One projector frozen, no banner** | Did its JS load? (DevTools console: `$ is not defined` / Alpine missing) | Network/CDN issue. F5 the screen. **If 🔴-1 unfixed this is the #1 risk — vendor libs pre-event.** |
| Projector shows red "DISPLAY DISCONNECTED" banner | It's self-recovering | Leave it; it reconnects+reloads. Only F5 if banner persists >60 s. |
| All screens stale | web healthy? `redis-cli -h redis_bus ping` | `docker compose restart web` — **safe for counter** (persisted in PG); screens reconnect+reload. |
| Counter stopped/jumped | Was it an admin Start/Stop? | An admin re-issues Start from `/admin?page=display`. **Never "restart to fix" — restarts can't change it**; only an admin `action=start/stop` POST does. |
| Counter screen at 00:00:00 during a round | Its JS failed to load (🔴-1) | F5; if it recurs, the venue can't reach the CDN — confirm libs are vendored. |
| Scorer "conflict" (409) | Expected on concurrent cell edits | Scorer reloads sheet, re-enters. Working as designed. |
| A score "doesn't add up" | `mahj_hand` duplicate audit (test plan #6) | Likely a duplicate row (🟠-2). Delete the extra `hand_nb` row in admin; add the unique constraint after the event if not done. |
| Scans stuck "expired" | `scan_worker` logs; `redis-cli -h redis_bus llen scan:queue` | `docker compose restart scan_worker`; bus is noeviction so rising `evicted_keys` is only the LRU cache, not scans. |
| Site slow under crowd | nginx `X-Cache-Status`; `docker stats` web CPU | Confirm `/` is mostly HIT; modals are 30 s-cached — confirm they hit. |
| DB issues | `docker compose logs pgbouncer db` | `restart pgbouncer` first; `db` only if truly wedged. |
| All screens AND `/` dead but containers "up" | hung web (🟠-4) | `docker compose restart web`. (Add the autoheal sidecar to make this automatic.) |

**Restart-without-stopping-the-counter:** `web`, `redis`, `redis_bus`, `pgbouncer`, `nginx`,
`scan_worker` can all be restarted freely — the counter is a Postgres-persisted absolute
timestamp and is unaffected. Only an admin Start/Stop changes it.

**Backups:** `pg_dump` hourly off-box; snapshot the `postgres_data` volume on a schedule.

---

## Suggested order today
1. **🔴-1** vendor the CDN libraries locally + `collectstatic` (also fixes 🟠-3). Re-run test #1.
2. **🟠-2** audit for duplicate Hands (test #6), dedupe, add the unique constraint + migration.
3. **🟠-4** add the autoheal sidecar (or commit to live operator watch).
4. **🟡-5** wrap `scan_worker` `dequeue` in a reconnect loop.
5. Re-run chaos drills (#5) end to end.
