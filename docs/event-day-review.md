# Event-Day Robustness Review — OEMC 2026

Scope: failure modes that can bite **during** the championship, under the real load
pattern: ~10 scorers writing in parallel + parallel scan uploads, hundreds of viewers
on `/`, projector screens at `/1 /2 …` that nobody can easily refresh, and one operator
driving displays + the ceremony. This complements `review.md` (infra/deploy hygiene);
the items here are about live-event resilience, not build/deploy.

Severity: 🔴 fix before the event · 🟠 should fix · 🟡 nice to have / housekeeping.

---

## 🔴 E1 — `details_player_ema` blocks a worker on an external site with no timeout — ✅ FIXED (endpoint deleted)

**Resolution:** the endpoint was dead (no template/JS/`reverse()`/`{% url %}` referenced it),
so the view, its `/details_player_ema_<id>` route, the `views/__init__.py` export, and the
now-unused `urllib.request.urlopen` / `bs4` imports were removed. `manage.py check` passes.

Original finding below for the record.



`mahj/views/public_modals.py:44` — `urlopen(url)` fetches `mahjong-europe.org` with **no
timeout** and parses it with BeautifulSoup/lxml, on the request path, from an
**unauthenticated** endpoint (`/details_player_ema_<id>`).

Why it matters at the event:
- No timeout → if mahjong-europe.org is slow or down, the call blocks until the OS socket
  default (effectively forever). Each blocked call ties up a sync worker thread.
- With only 4 gunicorn workers, a handful of these in flight can exhaust the thread pool,
  and **the whole site stalls** — score saves, projector polling, the homepage.
- The scrape also assumes a fixed HTML layout (`find_all("img")[1]`, `[COL_RANK]`); any
  layout change throws an unhandled 500.

Fix: add a short timeout (`urlopen(url, timeout=3)`), wrap the whole body in try/except
returning a graceful empty/`""` response, and cache the result per EMA_ID for the event.
If the EMA-rank feature isn't used live, disable the route entirely. (It isn't linked from
any template, so disabling is the safest pre-event move.)

## 🔴 E2 — Synchronous OCR scan path can starve all workers during parallel scanning — ✅ FIXED (moved to a worker queue)

**Resolution:** OCR was moved off the request path entirely. `POST /scan` now
stages the uploaded image on the shared `captures/scan_jobs/` volume, pushes a job
onto a Redis FIFO queue (`mahj/scan_queue.py`), and returns a `job_id` instantly;
the client polls `GET /scan_status?job_id=`. A dedicated `scan_worker` management
command consumes the queue and runs the OpenCV alignment + Anthropic OCR one job
at a time, writing the result back to Redis. Compose runs **2 `scan_worker`
replicas** sharing the one queue, so scan concurrency is bounded to 2 and can never
tie up the gunicorn request workers. Also addressed here: the Anthropic client now
has `timeout=30, max_retries=1`; debug crop writes are gated behind `settings.DEBUG`;
the response's first *text* block is selected defensively (review.md S8); and the
single-threaded worker removes the shared-OpenCV thread-safety concern (review.md
S1). The gunicorn-vs-nginx `/scan` timeout mismatch (S4) is now moot for OCR.

Remaining knob: if a burst regularly exceeds 2, raise `scan_worker` replicas.

Original finding below for the record.

`mahj/views/scan.py` runs, inside a single request: image decode → OpenCV ORB homography
→ a multi-second Anthropic `messages.create` call → DB writes. With 10 scorers uploading
sheets in parallel and only 4 workers:

- The Anthropic client is created with **no `timeout=`** (SDK default is minutes), so a slow
  upstream call holds a worker thread a long time.
- `gunicorn.conf.py timeout = 30` disagrees with nginx `/scan` `proxy_read_timeout 60s`
  and with how long OCR actually takes — a slow scan can be cut off mid-flight.
- Enough concurrent scans exhaust the sync thread pool; HTTP rendering for *everyone*
  (displays, homepage, score saves) queues behind them.
- `_orb` / `_matcher` are module-level singletons shared across the worker's threads;
  OpenCV doesn't guarantee thread-safety for shared `Feature2D`/`BFMatcher`, so concurrent
  scans can corrupt results or crash (also flagged in review.md S1).

Fix (in order of payoff):
1. `anthropic.Anthropic(api_key=..., timeout=20.0, max_retries=1)` so a stuck call can't
   pin a worker.
2. Reconcile the three timeouts (pick one budget, e.g. gunicorn 60 = nginx 60).
3. Build per-call ORB/matcher (cheap) instead of sharing singletons.
4. Best: move OCR off the request path (background task + poll), so scanning can never
   starve score entry or the displays. If that's too big before the event, at least raise
   worker count and cap concurrent scans client-side.
5. Gate the unconditional debug-image writes + DEBUG logging of raw OCR text behind
   `settings.DEBUG` (review.md I1) — they write to disk and log player data on every scan.

## 🔴 E3 — A Redis blip can turn a *successful* score save into a 500 to the scorer

Every score-entry path (`update_hand_points`, `update_positions_bulk`,
`create_hand_points`, `validate_score_sheet`, scan write, publish) does its DB write and
**then** calls a broadcast (`signals._broadcast` → `async_to_sync(group_send)`).
`_broadcast` only guards against the channel layer being `None`; a transient Redis
connection error **raises**, propagating out of the already-committed view as a 500.

Effect: the data is saved, but the scorer sees an error and re-submits → duplicate effort,
confusion, and (for `create_hand_points`) potential double-writes.

Fix: wrap the `group_send` in `_broadcast` in `try/except Exception: log & return`. A
messaging hiccup must never fail a persisted write. Cheap, safe, high value.

## 🔴 E4 — Single Redis is a SPOF for sessions + cache + channel layer

`apps/settings/base.py` puts **sessions** (`SESSION_ENGINE = cache`), the **cache**, and the
**Channels layer** all on one Redis. If Redis restarts or is flushed mid-event:
- All 10 scorers + the display operator are **logged out** (sessions live only in Redis).
- Every page recomputes from scratch (cold cache) under full viewer load.
- All websockets drop.

`restart: unless-stopped` brings Redis back, but the login loss is disruptive in the middle
of scoring. Also note: no `maxmemory`/eviction policy is set — under sustained load Redis
default is `noeviction`, so if it ever fills, **writes start failing** (cache sets, session
writes) rather than evicting.

Fix options:
- Move sessions to the database (`SESSION_ENGINE = django.contrib.sessions.backends.db`,
  or `signed_cookies`) so a Redis hiccup doesn't log everyone out. Cheapest resilience win.
- Set a `maxmemory` + `maxmemory-policy allkeys-lru` (or `volatile-lru`) on the redis
  service, and confirm persistence (AOF/RDB) so a restart restores state.
- Brief staff on the re-login path and keep the magic-link/login handy.

---

## 🟠 E5 — The ceremony screen can get stuck on a stale slide at the worst moment

`display_ceremony.html` uses its **own** WebSocket client (not the hardened
`display_socket.js`). It has no ping/pong heartbeat, and crucially it does **not reload on
reconnect**. So:
- A half-open socket (NAT/Wi-Fi timeout) is never detected → reveal updates silently stop.
- If the screen blips while the operator fires the final `action=publish` (which broadcasts
  `phase: idle`), the screen **misses that event** and stays frozen on the ceremony slide
  until someone manually reloads it — exactly the screen you said is hard to refresh, at the
  highest-stakes moment.

Fix: drive the ceremony screen through `connectDisplaySocket` (heartbeat + reload-on-
reconnect already solve both), or add the same heartbeat and a reload on every reopen.
`index()` already re-renders the correct ceremony/idle state server-side, so a reload
always resyncs.

## 🟠 E6 — Worker recycling drops every WebSocket on that worker

`gunicorn.conf.py max_requests = 1000` recycles each worker after 1000 requests. Under
UvicornWorker that **kills all live WebSocket connections** held by the worker, forcing the
projector screens and viewer pages to reconnect — and `display_socket.js` reloads the page
on reconnect. With constant homepage/poll traffic, workers cycle regularly, causing periodic
reload flicker across all screens for no real benefit (memory creep from a Django ASGI app
is minor).

Fix: set `max_requests = 0` (disable) for the ASGI deployment, or raise it to a large value
so screens aren't churned. If memory creep is a real concern, run a separate dedicated
worker pool for `/ws/` vs HTTP.

## 🟠 E7 — Per-player / per-team modals are uncached and heavy, and hundreds will open them

`details_player` and `details_team` (`public_modals.py`) are the "see my scores/stats"
endpoints players will hammer. `scores_per_player_json` inside them is cached, but
`player_extra_stats`, `player_rounds_json`, and especially `details_team` (which recomputes
the full team rank history with nested loops over the entire leaderboard per round) run
fresh DB queries on every open, with no cache and no nginx microcache (these aren't `/`).

Fix: wrap these in a short per-key cache (e.g. `cache.get_or_set` keyed by
`(subdomain, id, last_published_round)` with a 20–60s TTL, busted by `invalidate_leaderboard`).
Verify with a quick concurrent load test (see runbook below).

## 🟠 E8 — Display standings 500 if there are fewer than 12 players

`display.py:147` does `scores_json[11]["visible"]` and several places index `scores_json[0]`.
With < 12 players this is an `IndexError` → 500 on a projector screen. Not a risk at the
championship's real size, but it *will* bite during pre-event setup/testing with a handful of
dummy players, and a 500 on an unrefreshable screen is exactly what you want to avoid.

Fix: guard the index (`len(scores_json) > 11 and scores_json[11]["visible"]`).

---

## 🟡 Housekeeping / lower-risk

- **`counter_start` has `print()` debug statements** (`admin_views.py:470-481`) — noisy logs.
- **Dev tenant hacks** (`helpers.get_tenant`): `subdomain == "192" → devvarberg`, and
  auto-creating a Tenant for any authenticated staff hitting an unknown subdomain — a typo
  silently creates a tenant (review.md S6). Env-gate or remove for the event.
- **`ALLOWED_HOSTS`** includes `192.168.0.116,localhost` — harmless but confirm
  `oemc2026.mahj.ovh` is covered by the leading `.mahj.ovh` (it is).
- **OCR schema vs prompt mismatch** for `Winner` nullability (review.md S7) — can reject a
  legitimately-empty hand.
- **`message.content[0].text`** assumes text-first response (review.md S8).
- Unpinned deps, esp. `anthropic` (review.md I2) — a same-commit rebuild during the event
  could pull a new SDK and break `output_config`/scanning. Pin before the event; don't
  `--build` during it.

---

## Pre-event runbook / operational checklist

1. **Don't rebuild or `git pull` during the event.** Pin deps now; deploy once; freeze.
2. **Load test before the doors open**: hammer `/`, `/details_player_<id>`,
   `/details_team_<t>` concurrently (e.g. `hey`/`ab` at 100–300 concurrent) and watch worker
   saturation; fire 5–10 simultaneous `/scan` uploads and confirm the homepage + a projector
   screen stay responsive. This single test would surface E1, E2, E7 directly.
3. **Increase workers** if the box has the cores (gunicorn `workers` and the sync thread pool
   are your real concurrency ceiling for blocking calls).
4. **DB backups**: snapshot the `postgres_data` volume on a schedule during the event
   (e.g. `pg_dump` hourly to off-box) so a corruption is recoverable mid-tournament.
5. **Screen recovery**: confirm every projector screen auto-recovers after a server restart
   (display_socket.js reload-on-reconnect handles this — but E5 ceremony screen and E6
   churn are the exceptions to fix).
6. **Have a printed login link** for scorers/operator in case of E4 (Redis restart → logout).
