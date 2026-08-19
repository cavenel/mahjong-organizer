# Deep code review — 2026-08-19

**Repo:** `django_oemc2022` · **branch:** `refactor` · reviewed over the current working tree only (no git history consulted). Full test suite run during review: **481 passed**.

Method: six specialized reviewers ran in parallel over disjoint slices (models/infra, admin views, score-entry views, public views + scoring package, deployment/settings/publishing, templates + JS), each instructed to verify findings against actual callers/consumers before reporting. Findings merged and re-ranked; duplicates confirmed independently by multiple reviewers. Line numbers refer to the working tree as of 2026-08-19.

## Verdict

The codebase is in good shape for a public release, but the secrecy model has holes at its edges. The core is well engineered — disciplined tenant scoping on effectively every query, a consistent per-tenant Membership authorization layer, careful optimistic locking in score entry, clean secrets handling, a solid restore pipeline, a substantial security test suite. The problems cluster in four places:

- **anonymous surfaces that bypass the publish/withhold visibility rules** (a print view, the scorers WebSocket, the scan API)
- **state-changing GET endpoints** that sidestep CSRF protection
- **two pages that predate the app's escaping discipline** (XSS via player/team names)
- **a never-exercised Riichi rules path that is functionally broken**

All fixes are local; none require redesign.

**Counts:** 1 critical · 11 high · 15 medium · 21 low.

Severities are calibrated for the public multi-tenant deployment. Some highs (tenant-escalation, host-header spoofing) are moot in single-tenant standalone mode; some (standalone default credentials) only exist there.

## Progress

Items marked **✅ DONE** below have been fixed and verified (full suite still 481 passing). Completed 2026-08-19, safe mechanical batch:

- Dead `User` / `stat_all_rounds` / `stat_rounds` imports + redundant `invalidate_leaderboard` re-import removed
- Dead `_last_published_round` (def + export) deleted
- `Schedule.__str__` and `Screen.__str__` NULL-crash / duplicate-field bugs fixed
- Turkey (`'tr'`) added to `EUROPE` — Best European award now includes Turkish players
- WebSocket subdomain regex tightened to ASCII + length bound
- Two stale docstrings (`_assign_winds`, `leaderboard_gen`) corrected

## Fix before the public release

1. **Gate `/print_scores`** — it publicly renders full unmasked standings (F1).
2. **Authenticate the scorers WebSocket** — it streams live unpublished scores to anyone (F2).
3. **Stop leaking scores through the scan API** and stop trusting client-authored score writes (F3, F13).
4. **✅ DONE — Escape the five `json.dumps|safe` injections and the draw pages' `innerHTML`** — stored XSS via player/team names, one on a public page (F4, F5).
5. **Make every mutating endpoint POST-only** — ceremony publish, screen/mode CRUD, `set_tournament`, logout (F6).
6. **✅ DONE — Fix the Riichi path** — score saves 500 and rounds can never be published (F7, F8). The round-trip test also turned up a third Riichi blocker the review missed — see the note under F8.
7. **✅ DONE — Close the two multi-tenant gaps** — login-link minting escalation and the shared import temp file (F9, F10).
8. **✅ DONE — Killed the standalone `admin/admin` default** on a 0.0.0.0 bind (F11). **✅ DONE** — removed `USE_X_FORWARDED_HOST` (F12).

---

## Information leaks — the visibility model bypassed

The app carefully masks unpublished rounds and the ceremony-withheld final everywhere on the HTTP pages, except here. Two reviewers independently confirmed F2.

### ✅ DONE · F1 · CRITICAL · security/visibility bypass — `/print_scores` is public and renders full standings
`mahj/views/print_views.py:77-88` · routed at `apps/urls.py:88`

`print_scores` has no auth decorator (contrast `table_posters` at line 169) and calls `scores_per_player_rows(request, full_view=True)`, which bypasses publish gating and the ceremony-withheld window entirely. During the podium-suspense window, anyone requesting `/print_scores` reads the complete final standings.

**Fix:** Add `@tenant_admin_required`, or pass `full_view=is_tenant_admin(request)`. Consider gating the other undecorated print endpoints (`cross_positions`, `player_cards`, `player_names`, `team_names`, `print_schedule`) for consistency — they only expose data the public desktop already shows, but it costs nothing.

### ✅ DONE · F2 · HIGH · security/visibility bypass — Scorers WebSocket accepts anonymous connections and streams live scores
`mahj/consumers.py:52-56` · `mahj/routing.py:11` · fed by `mahj/views/score_entry.py:417`

`ScorersConsumer.connect()` joins `scorers_{subdomain}` and calls `accept()` without ever reading `scope['user']` (which `AuthMiddlewareStack` populates) or checking Membership. Anyone can open `wss://…/ws/scorers/<subdomain>/` — for any tenant — and watch per-seat minipoints/tablepoints in real time as scorers type them, including the withheld final round. Publish state and validation events leak on the same channel. This undoes the entire publish-gating model.

**Fix:** In `connect()`, resolve the user's Membership for the URL's tenant via `database_sync_to_async` and `close()` unless they hold scorer/publisher/tenant-admin. `TenantConsumer` (public displays) is fine as-is.

### ✅ DONE · F3 · HIGH · security/visibility bypass — `scan_seats` returns unpublished mp/tp to anonymous callers
`mahj/views/scan.py:312-350`

The unauthenticated endpoint returns live `mp`/`tp` for any (round, table) with no published-round or withheld masking. Anyone can iterate `scan_seats?round_nb=N&table_nb=T` and reconstruct the hidden final round. The scan UI only needs seat names and filled/validated flags — it never displays the scores.

**Fix:** Drop `mp`/`tp` from the payload (nothing in `scan.html` uses them), or mask them unless the caller holds the scorer role.

### ✅ DONE · Low · security/spoiler — Ceremony stat slides ship winners before the reveal
`mahj/views/ceremony.py:164-168`

For `phase='stat'`, the full slide (winners and values) is sent regardless of `step`; the title-then-reveal staging is purely client-side, so a spectator can read the winner from the WebSocket frame seconds early.

**Fix:** Omit `winners`/`value` when `step == 0`.

---

## Cross-site scripting

Player and team names are user-controlled (Excel import, player editor). Most of the codebase escapes correctly via `json_script` and an `esc()` helper — these pages predate that discipline.

### ✅ DONE · F4 · HIGH · security/stored XSS — `json.dumps` + `|safe` inside `<script>`: `</script>`-breakout via names
`display_ceremony.html:44` (public page, payload from `views/display.py:64`) · `admin_player_draw.html:38-43` · `admin_team_draw.html:32-37`

Python's `json.dumps` escapes neither `<` nor `/`, so a player named `</script><script>…</script>` terminates the script block and injects live script. The ceremony display is a **public** page. The rest of the codebase already does this right with `|json_script` (`desktop.html:145`, `admin_seating.html:18`, `admin_settings.html:92`, …).

**Fix:** Switch the five injections to `{{ …|json_script:"id" }}` + `JSON.parse(textContent)`, or at minimum `json.dumps(…).replace('<', '\\u003c')`.

### ✅ DONE · F5 · HIGH · security/DOM XSS — Draw pages interpolate names unescaped into `innerHTML`
`admin_player_draw.html:138-143, 231, 397, 403, 453, 480, 510-514` · `admin_team_draw.html:242, 356, 360, 463, 514, 525, 566, 570, 798, 802`

Template literals build HTML from `p.full_name` / `t.name` with no escaping. Worst case: `onclick="confirmTeam('${t.name.replace(/'/g, "\\'")}')"` escapes only single quotes — a name containing `"` breaks out of the attribute entirely. The server error string interpolated at `:453` also embeds a player name (`admin_views.py:906`). `display_ceremony.html:46-50` already defines the correct `esc()` helper — these pages just don't use it. Fixing this also fixes legitimate names with apostrophes or `<` rendering wrong.

**Fix:** Apply the existing `esc()` helper (or `textContent`-based rendering) at every interpolation site; move the `onclick` to an event listener carrying the id, not the name.

### ✅ DONE · Low · security — `scan.html` renders server error text via `innerHTML`
`mahj/templates/mahj/scan.html:213` · error source `views/scan.py:182-183`

`div.innerHTML` with `${msg}` where `msg` can be OCR-pipeline error text.

**Fix:** Use `textContent` for the message span.

---

## State-changing GET endpoints

Django's CSRF protection never applies to GET, and SameSite=Lax cookies still ride top-level navigations — so every one of these is a working CSRF vector against a logged-in operator. Four reviewers hit this pattern independently.

### ✅ DONE · F6 · HIGH · security/CSRF — Mutations tunneled through GET across the admin surface
`views/ceremony.py:203-248` · `views/admin_views.py:1226-1277, 1394-1459` · `views/display.py:313-335` · `admin_display.html:49, 438, 444` · `admin_ceremony.html:290`

The worst single instance: `ceremony_control?action=publish` publishes **all rounds including the withheld final** and fires the static export on a plain GET — one crafted link clicked by a logged-in display op reveals the results mid-ceremony. The same pattern covers `add_screen`/`remove_screen`/`rm_mode`/`set_mode`/`set_all_views`, `action=set_tournament`, and `update_screen_view`/`update_screen_name`. Even the POSTed ones read every parameter from `request.GET` with no method check, so GET works identically. Bonus bug: `add_mode` via bare GET crashes with an IntegrityError. Related nuisances: `?logout=1` logs the operator out on GET in two places (`admin_views.py:1325`, `public.py:29`), and `ceremony_control` stores `phase` unvalidated — any unknown string hijacks every display screen with an empty slide (`ceremony.py:236`).

**Fix:** Require POST (405 otherwise) on every mutating action and move parameters into the body; convert the template anchors to POST fetches/forms (the mode-apply calls already POST — the views just don't enforce it). Whitelist `phase` against `('idle','blank','teams','players','stat')`. Structurally, splitting these actions out of the `options()` mega-view into their own POST-only URLs (like the rest of `apps/urls.py`) fixes the whole class.

### ✅ DONE · Medium · security/privilege — `_apply_set_tournament` is a denylist, not an allowlist
`mahj/views/admin_views.py:1226-1253`

Only `counter` (and `total_time` for non-admins) are protected; any other `TournamentSettings` field is settable via `?tournament-<field>=`. A *display operator* (the role admitted to `page=display`) can rewrite `nb_rounds`, `has_teams`, `rules`, `public_url` — structural fields far beyond screen-layout tuning. Combines badly with the GET-mutation finding above.

**Fix:** Invert to an explicit per-page allowlist (display page: layout fields; settings page: identity/format fields).

---

## Multi-tenancy gaps

Tenant scoping on ORM queries is disciplined throughout — these are the places where tenancy wasn't carried into the filesystem, credential, or HTTP layer.

### ✅ DONE · F9 · HIGH · security/escalation — Login-link minting lets a tenant admin escalate into another tenant
`mahj/views/user_admin.py:218-237`

A sesame login link is a full credential for the *account*, and it's handed to the *minter* in the JSON response. An admin of tenant A can mint a link for a user shared with tenant B, then log in as them on B's subdomain with whatever roles they hold there. `user_revoke_links` (line 257) and `user_delete` (line 286) already enforce `_memberships_contained` for exactly this reason; minting skips it.

**Fix:** Apply the same containment check: `if not request.user.is_superuser and not _memberships_contained(user, tenant): 403`.

### ✅ DONE · F10 · HIGH · bug/data corruption — Template import uses one shared temp file, cross-tenant race
`mahj/views/admin_views.py:277-290`

Every import from every tenant writes to the fixed path `BASE_DIR/tmp/template.xlsx`. Two concurrent imports race on remove/save/load: tenant A can end up importing tenant B's player list — silent cross-tenant data leakage — and A's own players are already deleted (line 284) before the wrong file loads.

**Fix:** Skip the disk hop: `load_workbook(attached_file)` accepts the uploaded file object directly. (Or `tempfile.NamedTemporaryFile` per request.)

### ✅ DONE · F12 · HIGH · security/host spoofing — `USE_X_FORWARDED_HOST` with nginx not stripping the header
`apps/settings/prod.py:23` · `nginx/mahjong.conf.template`

nginx forwards client-supplied `X-Forwarded-Host` untouched, so a request to `a.domain` carrying `X-Forwarded-Host: b.domain` makes `get_host()` — the input to tenant resolution in `helpers.py:186` — resolve tenant **b** while nginx's microcache keys on `$host` = a. That enables cross-tenant content confusion and cache poisoning (both hosts pass `ALLOWED_HOSTS` under the shared base domain), and Django-built absolute URLs follow the spoofed host.

**Fix:** Remove `USE_X_FORWARDED_HOST` (nginx already passes the correct `Host`), or add `proxy_set_header X-Forwarded-Host $host;` to every proxy block.

> **Confirmed during S6:** this is a working cross-tenant *authorization* bypass, not
> only a host-spoof. `test_the_vector_is_real_which_is_why_the_setting_is_off`
> (`test_membership.py`) flips the setting back on and a scorer of tenant B gets a
> 200 on tenant A's host purely from the header. **Fixed** by dropping the setting;
> no code change can defend it while Django prefers a client-supplied header.

### ✅ DONE · Low · security/hardening — `Tenant.subdomain` has no database uniqueness
`mahj/models.py:4-6` · resolution via `filter(…).first()` at `views/helpers.py:204`

Duplicate subdomains would silently merge/split tenant data; the `exists()` guards in `user_admin.py` are racy and cover only one writer. All authorization keys off the tenant row, so this is an isolation backstop worth having.

**Fix:** `UniqueConstraint(fields=['subdomain'])` — the migration auto-applies on deploy.

### ✅ DONE · Low · security/latent — `player_rounds_rows` fetches a Player with no tenant filter
`mahj/views/scoring.py:38-40`

`Player.objects.get(id=player_id)` — currently shielded because its only caller fetches the tenant-scoped player first, but it's a cross-tenant hole waiting for a second caller.

**Fix:** Add `tenant=tenant` to the lookup.

---

## The Riichi path is broken

The repo ships a Riichi report template and a rules toggle, and the test fixtures include a Riichi variant — but the score-entry flow for non-MCR events has clearly never been exercised end-to-end.

### ✅ DONE · F7 · HIGH · bug — Every Riichi score save returns 500 (`KeyError: 'tp'`)
`mahj/views/score_entry.py:390` · grid JS in `admin_scores_per_table.html:196-199, 289-297`

The TP input is only rendered when `rules == "MCR"`; for anything else the JS builds `tp: undefined`, which `JSON.stringify` drops, so the `tp` key is absent — and `float(entry['tp'])` raises `KeyError`, which the `except (TypeError, ValueError)` one line below does not catch. Every minipoint save in a Riichi event 500s; the UI pip just turns red. Tests always send `tp` explicitly, so this is untested.

**Fix:** `entry.get('tp')` (and `entry.get('mp')`).

### ✅ DONE · F8 · HIGH · bug — Riichi rounds can never be published
`mahj/views/score_entry.py:457`

Publishing requires `tablepoints` non-null on all seats, but nothing ever writes tablepoints outside MCR (the input isn't rendered), so `set_round_published` returns "round is incomplete" forever. Non-MCR standings rank on minipoints alone, so TP genuinely isn't needed there.

**Fix:** Gate the `tablepoints=None` completeness check on `tournament.rules == "MCR"`. And add a Riichi round-trip test.

> **Added 2026-08-19 during S4 — F8c · HIGH · bug — a published Riichi round still doesn't count.**
> Fixing F8 makes a Riichi round publishable, but not visible: four places decided a
> seat was "scored" by requiring non-NULL `tablepoints`, which Riichi never writes.
> `player_standings` (`standings.py:29`) held `round_max` at 0, so every Riichi total
> stayed zero; `_last_complete_round` (`visibility.py:25`) reported no complete rounds
> whatever was entered; `player_extra_stats` (`stats.py:505`) and `team_extra_stats`
> (`stats.py:614`) selected no seats, emptying the placement cards. The suite was green
> because the `riichi_tournament` fixture inherits pre-filled `tablepoints` from the MCR
> seed, so it never had the shape a real Riichi tournament has.
> **Fixed** with one shared rule — `seat_is_scored()` / `unscored_seats_q()` in
> `scoring/_common.py`: minipoints always required, table points only under MCR
> (matching how `stats.py` already picks its rank field).

---

## Score-entry integrity

The optimistic-locking core (version + F()+1, 409 recovery in the JS) is correctly built — these are the seams around it.


### ✅ DONE · Medium · bug/race — Publish/edit TOCTOU, no lock spans check and write
`mahj/views/score_entry.py:406-414, 454-477`

`update_seats_bulk` checks `_round_is_published` then writes in a separate transaction; `set_round_published` checks completeness then creates the `PublishedRound`. Interleaved, a scorer's edit can land *after* the publish — the published round then differs from what was exported, with no 409 and no cache bust.

**Fix:** Wrap check + write in one transaction with `select_for_update()` on the `PublishedRound` row.

### ✅ DONE · F8b (Medium) · security/bug — Publish lock checked only against the first seat in the payload
`mahj/views/score_entry.py:398-417`

`round_nb`/`table_nb` come from `to_update[0]`, so a payload mixing one unpublished-round seat with published-round seats bypasses the lock and silently edits published scores — the exact thing the lock exists to stop a plain scorer from doing.

**Fix:** Reject payloads whose seats don't all share one `(round_nb, table_nb)`, then check the lock on that pair.

### ✅ DONE · Medium · bug/race — `_prune_to_played_hands` skips the version bump
`mahj/views/score_entry.py:282-285`

Coercing blank rows to draws via `.update(win_by=0, …)` doesn't bump `version`, unlike every other write path. A second device holding the old version can then overwrite the coerced draw on a *validated* sheet with no 409 — silently un-drawing the hand.

**Fix:** Add `version=F('version') + 1` to the coerce update; consider running prune + validate in one transaction.

### ✅ DONE · Medium · bug/data quality — Invalid hand entries silently coerced instead of rejected
`mahj/views/score_entry.py:50-78` (`_parse_hand`)

An out-of-range discarder (`From = 5`, the classic typo for 4) is coerced to `win_from=None` — recorded as a *self-draw*, which pays out differently than the discard win the scorer meant. Impossible MCR values (`Value < 8`) are accepted; decimal input zeroes the hand into "unplayed". The client tints these cells red but still saves them, and the sheet can be validated with the coerced values. Related: a non-integer minipoint (`12.5`) is saved as `NULL` while the client-side check reports success (`score_entry.py:385-388`) — publish later fails "incomplete" with no visible culprit.

**Fix:** Reject (400 with the offending field) instead of coercing when `by`/`from` is outside 0-4 or points fail to parse on non-blank input; echo stored values in the save response so the UI reflects reality.

---

## Scan pipeline

The OCR scan flow is intentionally anonymous (players photograph their own sheets) — but it currently trusts callers with much more than a photo.

### F13 · Medium · security — `scan_prefill` accepts arbitrary client-authored scores and can self-validate
`mahj/views/scan.py:371-440`

The hand values written come from the request body, not from the server-stored OCR result, and `validate` defaults to true when present — so an anonymous caller needs no photo at all to fill any empty table *and mark its sheet validated*, skipping scorer review and feeding the stats. The only guard is that the table must be empty.

**Fix:** Have the client send `job_id` and read the scores server-side from `scan_queue.get_result(job_id)`; ignore `validate` from anonymous callers.

### Medium · security/cost — Anonymous scan upload is an unmetered paid-API and disk sink
`mahj/views/scan.py:129-153`

Every anonymous POST stages up to 20 MB (nginx cap; `file.read()` loads it fully into RAM first) and enqueues one Claude vision call — no rate limit, no per-tenant quota, no image validation. Failed-job files are only cleaned on success paths, so the `captures/scan_jobs` volume fills.

**Fix:** Cap `file.size` in the view, verify the payload decodes as an image before staging, add a simple per-IP rate limit, and sweep stale job files.

### Medium · robustness — `scan_worker` dies and orphans the job if Redis blips at result-write time
`mahj/management/commands/scan_worker.py:69` · same shape in `restore_worker.py:92-95`

`set_result` sits outside the try/except that the "never let one bad job kill the loop" comment annotates — a `RedisError` there kills the process; compose restarts it, but the completed OCR result is lost and the client polls a stale `pending` until the 600 s TTL. In the restore worker the equivalent gap leaves the operator's page hanging on `pending` (the pool itself is safely resumed by the `finally`).

**Fix:** Move `set_result` inside a guarded block with a short retry; log on final failure. Also: the `pending` marker's 600 s TTL can expire while a job is still queued under backlog (`scan_queue.py:22`) — have the worker refresh it on dequeue.

---

## Backup, restore & standalone

The Postgres restore pipeline is genuinely well-guarded (double validation, typed confirmation, identifier quoting). The sqlite standalone path has two ordering bugs sitting exactly where a latent bug hurts most.

### ✅ DONE · F11 · HIGH · security — Standalone ships `admin/admin` on a 0.0.0.0 bind
`standalone/run.py:133, 37` · `apps/settings/standalone.py:35`

Bootstrap creates superuser `admin`/`admin`, binds `0.0.0.0`, and sets `ALLOWED_HOSTS=['*']` — anyone on the venue LAN can log into the full admin until the operator manually rotates a password the code only *prints a notice* about. For a non-technical operator that default will routinely survive the whole event.

**Fix:** Generate a random initial password and print it once (or force a change on first login). Consider binding the admin surface to loopback and exposing only display screens on the LAN.

### ✅ DONE · Medium · bug/data loss — A failed sqlite snapshot leaves a 0-byte file that passes the integrity check
`mahj/standalone_backup.py:77-83`

`sqlite3.connect(dest)` creates the file immediately; if `src.backup(dst)` then fails, the empty file remains — and `PRAGMA quick_check` on a 0-byte file returns `ok` (verified empirically), so it's listed as the newest snapshot and accepted for restore. Restoring it erases the live database.

**Fix:** Back up to `dest.with_suffix('.tmp')` and `os.replace` into place only after `backup()` succeeds; unlink the temp on failure.

### ✅ DONE · Medium · bug/data loss — Restore deletes the live WAL before the snapshot copy
`mahj/standalone_backup.py:137-146`

`apply_pending_restore` unlinks `-wal`/`-shm` *before* `shutil.copyfile(snap, tmp)`. If the copy fails — disk-full is realistic when duplicating the whole DB — the old DB stays live but its un-checkpointed commits are gone, and the safety snapshot is explicitly best-effort. The comment claims crash-safety via atomic `os.replace`, but the sidecar deletion happens outside that protection.

**Fix:** Reorder: copy snapshot to tmp first, then delete sidecars, then `os.replace`.

---

## Deployment & publishing

### ✅ DONE · Medium · security — SFTP publish auto-accepts unknown host keys
`mahj/publish/sftp_upload.py:98`

Without a pinned `host_key`, `_connect` uses `AutoAddPolicy()` and never persists the accepted key — every publish is trust-on-first-use with no continuity, i.e. accept-any. An on-path attacker can MITM the session and capture the target's password or private key.

**Fix:** Default to `RejectPolicy` and require the host-key line when configuring a target (the pin path already exists), or persist first-seen and reject changes.

- **✅ DONE (logged, not blocked) · Low · SSRF-ish probe** — `publish_target_test` (`admin_views.py:1133`) lets a tenant admin open an SSH handshake to any host:port on the private network. Bounded by auth; log/rate-limit if hardening. Each attempt now logs the user, tenant and target — the capability is inherent to a "test this target" button and the role is trusted, so it stays, but it's attributable.
- **✅ DONE · Low · CI** — `release.yml:14,105`: `contents: write` granted workflow-wide (the build job doesn't need it) and `softprops/action-gh-release@v2` pinned to a moving tag. Scope the permission to the release job; pin the action to a SHA.
- **✅ DONE · Low · ops** — `gunicorn.conf.py:2`: `umask = 0` makes the unix socket world-writable; safe only because the socket volume is shared solely with nginx — worth a comment stating that reliance.
- **Low · dead cookie** — `apps/middleware.py:21`: the `auth` cache-bypass cookie is set `secure=True` unconditionally, so it silently never persists over plain HTTP (dev, standalone). Cosmetic today.
- **✅ DONE · Low · key loss** — `apps/settings/standalone.py:28`: an ephemeral `SECRET_KEY` fallback silently makes stored publish secrets undecryptable (Fernet key derives from it). Documented for sessions, not for publish secrets.
- **✅ DONE · Low · third-party call** — `admin_display.html:135`: every admin-display view fetches `api.ipify.org`; make it click-to-look-up.

---

## Correctness — assorted verified bugs

### ✅ DONE · Medium · bug — `desktop` rebuilds score rows positionally, defeating round-keyed team folding
`mahj/views/public.py:108-116, 226-228` vs `scoring/standings.py:109-120, 232`

`team_standings` deliberately folds by `sc['round_nb']` because a player who missed a round has a shorter score list — but `desktop` rebuilds each row with `'round_nb': r_idx + 1` (positional index) before passing it in, and `stats_xlsx` indexes per-round columns the same way. Any sparse score list shifts every later score into the wrong round column and mis-folds the team totals — exactly the case the standings comment guards against. (Latent today if seats are always filled, but it silently disarms a documented guard; note `standings.py:26` asserts the opposite invariant — resolve that contradiction one way.)

**Fix:** Use `sc['round_nb']` when rebuilding, and read scores by round in the xlsx accessors.

### ✅ DONE · Medium · robustness — Cross-table grid sized from `player_count // 4`, public 500s on mismatch
`mahj/scoring/stats.py:22-30` · `views/print_views.py:37, 53`

`grid[p.table_nb - 1]` raises `IndexError` whenever a seat's table number exceeds players÷4 (10-table seating uploaded while 39 players registered); conversely extra empty cells make `cross_positions` — publicly routed — do `cell["seat"]` → `KeyError`. Ordinary setup states produce public 500s.

**Fix:** Derive dimensions from `max(seat.table_nb)`/`max(seat.round_nb)` like `tournament_seating` does; skip empty cells in the template.

### ✅ DONE · Medium · bug — Turkey still can't win "Best European"
`mahj/views/ceremony.py:32-37` vs `scoring/_common.py:60-64`

The `'turkey': 'tr'` flag alias exists (per its comment) precisely so Turkish players aren't excluded from Best European — but `'tr'` is missing from the `EUROPE` frozenset, so the award still skips them.

**Fix:** Add `'tr'` (and decide on `ge`/`am`/`az`/`fo`/`gi` while there).

### ✅ DONE · Medium · bug — Team modal: a Python `None` in the chart data kills the script block
`modal_details_team.html:149` · `views/public_modals.py:111-113`

**✅ DONE (S8).** `const historyData = {{ team_history_pos }};` injects a Python list repr; when a team is missing from a round's ranking the list contains `None`, which is a JS `ReferenceError` — the chart *and* `goBack()` below it die, so the Back button stops working. The player modal uses the same fragile pattern (ints-only today).

**Fix:** Pass both through `json_script` — JSON renders `null`, which Chart.js handles.

### ✅ DONE · Medium · bug — Free-text `EMA_ID` from the player editor crashes the template export
`admin_views.py:940-948` (no validation) → `:589` (crash) — **✅ DONE**

The editor accepts any string; export does `int(player.EMA_ID)` → 500 on "N/A". The importer already enforces digits-or-blank with zero-padding — the editor path bypasses it, so editor-entered "1234" also won't match the canonical "00001234".

**Fix:** Validate/normalize in `player_editor_save` the same way the importer does.

- **✅ DONE · Low · race** — `admin_views.py:898-912`: when a drawn number has no current holder, `select_for_update` locks nothing; two desks assigning simultaneously → the second gets an unhandled `IntegrityError` 500 instead of the designed 409. Catch it and return the 409 payload.
- **✅ DONE · Low · UI badge** — `seating.py:404-424`: `algebraic_feasible` is first-fit while `find_shifts` tries 300 random orderings, so the seating page can claim "rematch-free impossible" when `generate()` would succeed. Implement it as `len(find_shifts(T, R, seed=0)) >= R`. (The seating algebra itself was verified correct — forbidden shifts, distinct residues, teammate guarantee all check out.)
- **✅ DONE · Low · crash-swallow** — `seating.py:472-481`: `measure()` assumes contiguous draw numbers/rounds from 1; a hand-edited imported chart makes it raise, and the caller's blanket `except` silently hides the quality panel. Derive the sets from the rows; drop the blanket except.
- **✅ DONE · Low · UI state** — `admin_team_draw.html:186-197`: resuming a saved draw restores `paused` but never shows the pause overlay (the player-draw page does) — the page looks frozen, keys swallowed.
- **✅ DONE · Low · CSV** — `admin_player_draw.html:536`, `admin_team_draw.html:633, 787`: names are quoted without doubling embedded `"`; such a name corrupts the exported row meant to be re-imported.
- **✅ DONE · Low · cosmetics** — `models.py:415`: `Schedule.__str__` prints `time` twice (meant `name`) and crashes on NULL; same NULL-concat in `Screen.__str__` (`models.py:273`).
- **✅ DONE · Low · WS routing** — `routing.py:8-11`: the URL regex admits Unicode and unbounded length, but Channels group names must match `[a-zA-Z0-9_.-]{1,100}`; a crafted URL raises in `group_add`. Tighten to `[a-zA-Z0-9_.-]{1,80}`.
- **✅ DONE · Low · stats suppression** — `score_entry.py:136-138`: visiting `scores_per_hand_3_999` (scorer-gated GET) creates a phantom unvalidated sheet that marks round 3 "open" in the stats, with no UI listing it. 404 when no Seats exist for the pair.
- **✅ DONE · Low · cache spam** — `public_modals.py:147-160`: `detailed_scores` caches a placeholder per arbitrary (round, table) pair; bound it to the known seating range.

---

## Robustness — 500s where a 400/404 belongs

A recurring pattern: `int()` / `json.loads()` / `[...]` on raw client input with no guard. Each is a one-line fix; listed together as one cleanup batch.

- ✅ DONE (S8) — `public_modals.py:35-37` — `details_player`: unknown id → `DoesNotExist` 500 on a public, enumerable endpoint (the team modal correctly 404s); also `[…][0]` IndexError for a player absent from standings. Use `get_object_or_404` + a guard.
- ✅ DONE (S4) — `score_entry.py:377, 385-390` — `int(e['id'])`, `entry['mp']`/`entry['tp']`: KeyError/ValueError uncaught; a grid cell with a missing Seat renders `data-id=""` so a legitimate save 500s.
- ✅ DONE (S4) — `score_entry.py:217` — `int(round_nb)` runs *after* the transaction commits: a malformed `create_hand_points` request writes 16 hands, then 500s.
- ✅ DONE (S4) — `score_entry.py:227, 296-297, 327-328` — raw `int(POST[...])` on version/params → 500 instead of 400.
- `scan.py:321-332, 405` — `int()` on raw GET params and client body keys → 500.
- ✅ DONE (S2) — `admin_views.py` bare `json.loads(request.body)` in `admin_player_draw_assign`/`player_editor_save` now returns 400 on malformed input.
- ✅ DONE (S2) — `remove_screen` guards an empty queryset; `rm_mode`/`set_mode` use `_screen_mode_or_404` (stale/non-numeric id → 404, not 500).

---

## Dead code & duplication

- **✅ DONE · Dead** — `scoring/visibility.py:33-36`: `_last_published_round` exported but has zero callers (`publish_state` superseded it).
- **✅ DONE (deleted) · Test-only** — `scoring/stats.py:450-472`: `all_player_rounds` is called only by its own golden test; production uses `all_slot_rounds`. Delete or mark test-only.
- **✅ DONE · Dead imports** — `admin_views.py:16` (`User`), `:40-41` (`stat_all_rounds`, `stat_rounds`); `:503` re-imports `invalidate_leaderboard` already imported at line 25.
- **❌ NOT A FINDING · Dead JS** — `admin_display.html:291-293, 373`: `getTotalTime()` and its change handler target an input that only exists on the settings page fragment; the fallback always wins. **Checked in S8:** every function in that file is referenced, `getTotalTime` twice (from `updateDisplay` and `updateButtons`). The fallback winning makes it *degrade*, not dead — nothing removed.
- **✅ DONE · Dead parameter** — `views/display.py:200-202`: `_score_columns`'s `columns` argument is unused and threaded through `_paginate`.
- **✅ DONE · Duplication (server)** — the hand-tally classification loop exists four times in `stats.py` (194-225, 331-352, 538-559, 643-663) with subtly different keying — a divergence bug waiting to happen; placement counting exists twice (227-237 vs 686-716); `player_extra_stats`/`team_extra_stats` share ~35 verbatim lines. Extract `classify_hand(hand, wind)` + one placement helper.
- **✅ DONE · Duplication (server)** — `views/scoring.py`: six functions repeat the identical cache-or-compute wrapper; one `_cached(prefix, request, full_view, compute)` collapses ~80 lines to ~20. Also duplicated: ✅ DONE (S4) `WINDS` lists in `score_entry.py:22` / `scan.py:309` / `scoring/_common.py:14`; the validated/filled table-key computation (`admin_views.py:183-189` vs `1631-1641`); the reauth-gate fragment pasted three times (`1689, 1732, 1756`).
- **✅ DONE · Duplication (client)** — MCR table-point ranking (`get_tp` + `sortWithIndeces`) implemented three times with divergent styles (`admin_scores_per_table.html:236`, `admin_scores_per_hand.html:196` — this copy leaks globals — and `modal_detailed_scores.html:137`). This is scoring logic that must agree with the server; extract to one static JS file. Smaller repeats: `getCookie('csrftoken')` ×3, the preview-grid + enlarge-modal block duplicated verbatim between `admin_display.html` and `admin_ceremony.html`.
- **Structural** — `admin_views.py:1322-1806`: `options()` is a ~480-line page dispatcher with 13 hand-rolled role gates; a `{page: (required_role, renderer)}` table makes missing gates impossible to overlook, and splitting the tunneled mutations into POST-only URLs fixes F6 structurally.
- **✅ DONE · Consistency** — native `alert()` inside admin-shell fragments where `window.alertAction` exists (the shell's own comment explains blocking dialogs stall WS timers): `admin_scores_per_table.html` ×2, `admin_users.html` ×7, `admin_publisher_overview.html` ×2, `admin_tenants.html` ×2.
- **✅ DONE · Readability (docstrings)** — two docstrings described absent code: `seating.py:273` promised "local-search passes" (greedy-only), `signals.py:23` named cache keys that are now `modal_*`. Both corrected.
- **✅ DONE · Readability (remaining)** — `standings.py:53-74`: `history_pos` carries two junk leading entries stripped by a magic `.slice(2)` client-side while the team variant has none — align the shapes. `views/scoring.py:9-11`: the cache comment says signals invalidate on real writes, but Seat/Hand writes use `.update()`/`bulk_update` (no signals) — admin full-view caches can lag live entry by up to 300 s; make the comment state the actual contract.
- **Battery drain** — `display_counter.html:389-404`: the sound-unlock probe constructs a `new Audio()` every second forever on unattended projectors; back off or retry on `pointerdown`.
- **Missing atomic** — `admin_views.py:513-527`: the ~10-step template import handles Python errors with a manual wipe but not a mid-import process kill; wrapping the body in `transaction.atomic()` costs one line.

---

## Verified clean

Things the reviewers explicitly checked and cleared — listed so they don't get re-audited.

- **Authorization architecture** — every mutating URL carries the right Membership decorator; decorator ordering is correct; reauth ("sudo mode") gating on destructive endpoints is layered properly; no `csrf_exempt` anywhere; no IDOR found — tenant scoping is disciplined across dozens of queries.
- **Secrets** — `.env`, backup env, and OVH creds correctly untracked; prod fails loud on a missing `SECRET_KEY`; publish secrets encrypted (Fernet over HKDF) and write-only in the admin; no credentials in any tracked file.
- **Prod hardening** — secure/HSTS/redirect/cookie flags all set; nginx hardcodes `X-Forwarded-Proto` (not spoofable); unknown hosts get 444; container runs non-root with a clean multi-stage build; restore scripts are quoted, `set -euo pipefail`, typed-confirmation-gated, injection-free.
- **Scoring math** — no N+1 patterns that scale with player count; zero-division guarded throughout; tie handling and MCR-vs-minipoint ranking internally consistent; penalty correctly never re-added.
- **Seating algebra** — forbidden-shift set, pairwise-distinct residues, offset cancellation, teammate guarantee, and the greedy backtracker's bookkeeping all verified correct.
- **Score-entry core** — optimistic locking with 409 recovery correctly implemented on all three hand-write paths; CSRF present on every POST including beacon flushes.
- **Frontend** — no dead templates or JS files; every `fetch()` URL resolves; no Position→Seat rename leftovers in any payload contract; no multi-line `{# #}` comments; the WebSocket client layer (heartbeat watchdog, jittered backoff, reconnect-resync) is well engineered.
- **Cache invalidation** — all six leaderboard key families match their setters exactly; the counter hot path's signal-skip is deliberate and correct.
- **Tests** — full suite passes (481); the security suite covers role gating, CSRF, and GET-vs-POST per endpoint. Main gap: nothing exercises the Riichi score-entry round trip (see F7/F8) or omits `tp` from save payloads. ✅ DONE (S4) — `TestRiichiRoundTrip` closes this, and immediately found the extra finding recorded under F8.
