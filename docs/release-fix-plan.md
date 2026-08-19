# Release fix plan

Derived from [code-review-2026-08-19.md](code-review-2026-08-19.md). Goal: clear every open finding before the public release, grouped into **8 sessions** where each session is one coherent unit of work — same code area, same fix pattern, same test surface — so it can be done, tested, and committed in one sitting.

**Working rules for every session**
- Branch: `refactor` (this is the public-release refactor; commit there). Commit per session after its tests pass — **split into several smaller commits within a session where that's more natural** (e.g. one per finding or per coherent sub-change).
- **Scope: the full plan, including the structural refactors** (`options()` decomposition, `stats.py` tally extraction, etc.).
- End each session by running the full suite (`.venv/bin/python -m pytest -q`, ~3 min, currently **481 passing**) — it must stay green.
- Add the tests listed for the session *in* that session; a fix without a test doesn't count as done.
- Mark the covered findings **✅ DONE** in `code-review-2026-08-19.md` as you finish them, and tick the checklist boxes here.
- Keep each session's diff self-contained so it's independently reviewable/revertable.

**Recommended order:** S1 → S2 → S3 → S4 → S5 → S6 → S7 → S8. S1 is the release-critical leak; S2 and S3 share templates (do S2 first so S3 escapes the final markup); S4 is isolated and high-value; S5–S8 are independent and can be reordered freely.

**Already done** (safe mechanical batch, 2026-08-19): dead imports, dead `_last_published_round`, `Schedule`/`Screen` `__str__`, Turkey in `EUROPE`, WS regex, two docstrings.

---

## Session 1 — Close the visibility leaks (RELEASE-CRITICAL)

**Theme:** every anonymous surface must honour the same publish/withhold masking the main HTTP pages enforce. This is the single most important session.

**Findings:** F1 (critical), F2, F3, ceremony stat-slide spoiler. **✅ SESSION COMPLETE** — full suite 493 passing (+12 tests).

- [x] **F1 — gate `/print_scores`** — now passes `full_view=is_tenant_admin(request)`, mirroring the public desktop (public → masked, admin → full). Left the name/seating/schedule print views public (already-public data). Test: `test_print_views.py::test_print_scores_masks_rounds_for_the_public`.
- [x] **F2 — authenticate the scorers WebSocket** — extracted `scorer_socket_allowed(user, subdomain)` policy; `ScorersConsumer.connect()` wraps it in `database_sync_to_async` and closes on failure. Tests: `test_consumers.py::TestScorerSocketPolicy` + anon-reject / superuser-accept integration.
- [x] **F3 — stop `scan_seats` leaking scores** — dropped `mp`/`tp` from the payload; kept `wind`/`player` labels + flags. Test: `test_scan.py::test_scan_seats_does_not_leak_scores_to_anonymous`.
- [x] **Stat-slide spoiler** — `_slide_payload` strips `winners`/`value`/`mp`/`round_label` when `step < 1`. Test: `test_ceremony.py::test_step_zero_slide_withholds_the_winner`.

**Files:** `print_views.py`, `consumers.py`, `scan.py`, `ceremony.py`.

**Tests:** anonymous GET `/print_scores` during a withheld-final window returns masked standings (extend `test_print_views.py`); anonymous WS connect to `ws/scorers/<sub>/` is rejected and a scorer/admin is accepted (`test_consumers.py`); anonymous `scan_seats` response contains no `mp`/`tp` key (`test_scan.py` / `test_security.py`); ceremony `phase=stat, step=0` payload carries no winners (`test_ceremony.py`).

**Risk:** low-moderate. The WS auth is the fiddly one (async DB access); everything else is a guard.

---

## Session 2 — Stop state-changing GETs (CSRF hardening + admin allowlist)

**Theme:** no mutation should be reachable by GET. This is the highest-UI-risk session — it touches views *and* their templates together — so change one endpoint at a time and click through the admin after each.

**Findings:** F6, `_apply_set_tournament` allowlist, `phase` whitelist, `add_mode` GET crash, `?logout=1` (×2), plus the admin-side robustness 500s that live in the same code. **✅ SESSION COMPLETE** — full suite 502 passing (+9 tests).

- [x] **F6 — require POST on every mutating action** — method guards (405 on GET) on: `ceremony_control`, the display screen/mode dispatch (`add_screen`/`remove_screen`/`identify_screens`/`add_mode`/`set_all_views`/`rm_mode`/`set_mode`/`set_tournament`), settings `set_tournament`/`save_schedule`, and `update_screen_view`/`update_screen_name`. Params stay in the query string; CSRF is enforced by method. Templates converted: `admin_display.html` (new `postAdmin()` form-submit helper for add/remove screen + rm_mode), `admin_ceremony.html` (`send()` → POST + `X-CSRFToken`), `admin.html` (logout → POST form), `desktop.html` (logout → cookie-token POST form).
- [x] **Whitelist `phase`** — `VALID_PHASES` in `ceremony.py`; unknown phase → 400, not stored.
- [x] **`add_mode` GET crash** — gone (POST-only).
- [x] **`?logout=1` → POST** — admin (`options`) and public (`desktop`). The public page is role-bucket-cached, so its logout form reads the CSRF token from the cookie at click time; `desktop` mints that cookie for authenticated viewers only (via `get_token`), keeping anonymous `/` free of `Set-Cookie` so nginx still microcaches it.
- [x] **`_apply_set_tournament` allowlist** — `DISPLAY_SETTINGS_FIELDS` / `TOURNAMENT_SETTINGS_FIELDS`; the denylist is gone, so a display operator can't reach structural/identity fields and no page can touch `counter`.
- [x] **Admin 500-guards** — `json.loads` guarded in `admin_player_draw_assign`/`player_editor_save`; `remove_screen` guards an empty queryset; `rm_mode`/`set_mode` use `_screen_mode_or_404` (404 on stale/non-numeric id).
- [~] **Structural payoff (deferred to S8)** — kept the surgical method-guards inside `options()` rather than extracting the mutations into their own URLs; the security outcome is identical and it avoids a large, browser-untestable template rewrite. The `options()` decomposition remains an S8 cleanup item.

**Tests:** GET→405 on each converted endpoint; add/remove screen + logout (admin & public) via POST; display-op `set_tournament` can't change `nb_rounds` (allowlist); unknown `phase` → 400; `rm_mode` bad/non-numeric id → 404; remove-with-no-screens is a no-op. In `test_ceremony.py`, `test_display_admin.py`.

**⚠️ Manual click-through still recommended** (not covered by tests — the browser JS/form paths): display page → add/remove screen, apply mode, set all views, identify screens, rename screen, edit a display setting; ceremony console → start/reveal/back, blank, publish & end; settings → save a field + save schedule; both logout links (admin sidebar + public `⋮` menu).

**Risk:** HIGH for UI regressions — verify the click-through above against a running instance.

---

## Session 3 — XSS escaping + frontend de-duplication

**Theme:** player/team names are user-controlled; every injection point must escape. While in these exact templates, fold in the client-side duplication the review flagged.

**Findings:** F4, F5, `scan.html` innerHTML, CSV-escaping, team-draw resume-overlay bug, client-side duplication (MCR tp-ranking, `getCookie`, preview-grid). **✅ SESSION COMPLETE** — full suite 511 passing (+5 tests).

- [x] **F4 — five `json.dumps`+`|safe` injections** — all five now `{{ …|json_script:"id" }}` + `JSON.parse(textContent)`: `display_ceremony.html` (public), `admin_player_draw.html` (×2), `admin_team_draw.html` (×2). The views pass the objects instead of pre-dumped strings (`views/display.py`, `admin_views.py` draw-view contexts); `json` is no longer imported in `display.py`. `saved_draw` goes over as `[]` rather than the old `"null"` string — the page's `SAVED_DRAW.length > 0` check reads both the same way.
- [x] **F5 — draw-page `innerHTML` interpolation** — every name/flag site in both draw pages goes through `escapeHtml()`. The `onclick="confirmTeam('${t.name}…')"` breakout is gone: the row carries `data-team-index` (the team's index in `TEAMS`) on a delegated listener. Teams have no DB id, and the event log persisted in `localStorage` is keyed by `team_name`, so the index is the page's stable handle — the name never re-enters markup.
- [x] **`scan.html`** — the error card's message span is filled with `textContent` (mirrors `setPendingText` just above it).
- [x] **CSV export escaping** — one shared `csvField()` quotes all three export sites, doubling embedded `"`; since every field is quoted, embedded newlines and commas are covered too.
- [x] **team-draw resume overlay** — `loadState()`'s resume branch toggles `#pause-overlay` with the restored `paused` flag.
- [x] **De-dup while here** — `static/js/mcr_table_points.js` (`tablePointsFromMinipoints`) replaces the three divergent `get_tp` copies; `static/js/browser_utils.js` (`escapeHtml` / `csvField` / `csrfCookie`) replaces the two `getCookie` copies, `admin_seating.html`'s `csrf()` and `display_ceremony.html`'s local `esc()`; `_screen_previews.html` + `static/js/screen_previews.js` replace the preview-grid/enlarge-modal block duplicated between `admin_display.html` and `admin_ceremony.html` (each page keeps its own heading toolbar — their buttons differ).

**Files:** the templates above, three new `static/js/` files, one new template partial, `views/display.py`, `views/admin_views.py`.

**Tests:** `test_player_draw.py` (name holding `</script>` is escaped and still delivered; the escaping helper is loaded), `test_team_draw.py` (same for a team name with `"`/`<`; the row carries an index, not a name), `test_ceremony.py::TestScreenTakeover` (same on the **public** projector page). `json_script_payload()` in `conftest.py` reads back what `|json_script` wrote, so each test asserts both halves: nothing verbatim in the HTML, real name still in the payload.

**⚠️ Manual click-through still recommended** (the JS paths tests can't reach): player draw → search, assign, undo, Export CSV; team draw → search a team, enter four roles, advance, save, Export CSV, and resume a paused draw; a score grid → type minipoints and check the table-point cells fill; display page and ceremony console → show/hide previews, enlarge one, Escape to close, plus Identify screens on the display page.

**Risk:** moderate. `json_script` changes the read-side JS (`JSON.parse(textContent)`) — verify each consumer. Overlaps templates with S2, so do S2 first.

---

## Session 4 — Riichi path + score-entry integrity

**Theme:** all in `score_entry.py` and the score grid; one mental model (the save/validate/publish loop). The Riichi path has clearly never been run — add the missing test coverage as the centrepiece.

**Findings:** F7, F8, publish/edit TOCTOU, first-seat-only lock, `_prune_to_played_hands` version bump, invalid-entry coercion + NULL-MP, score-entry 500-guards, `WINDS` dedup, `scores_per_hand` phantom sheet. **✅ SESSION COMPLETE** — full suite 528 passing (+17 tests).

- [x] **F7 — Riichi save `KeyError: 'tp'`** — the Riichi grid sends no `tp` key at all, so `number_or_none(entry, 'tp')` reads a missing cell as NULL rather than raising.
- [x] **F8 — Riichi rounds unpublishable** — the `tablepoints`-non-null half of the completeness check is now MCR-only.
- [x] **F8c (NEW, found by the round-trip test) — a published Riichi round still didn't count.** Four places called a seat "scored" only with non-NULL `tablepoints`, so Riichi standings sat at zero, `_last_complete_round` reported nothing complete, and the placement cards were empty. One shared rule now: `seat_is_scored()` / `unscored_seats_q()` in `scoring/_common.py`. See the note under F8 in the review. The `riichi_tournament` fixture had hidden all of it by inheriting pre-filled `tablepoints` from the MCR seed.
- [x] **Publish/edit TOCTOU** — check and write are one transaction with the row's seats under `select_for_update()`, and `set_round_published` takes the same locks around its completeness check, so the two paths serialize. **Deviation from the plan:** locking the `PublishedRound` row as written would not have closed the race — an unpublished round has no row to lock, and a row lock cannot block the insert that publishing performs. Locking the seats both paths already touch does.
- [x] **First-seat-only lock** — one payload is one `(round_nb, table_nb)` or a 400.
- [x] **Prune version bump** — `version=F('version')+1` on the blank→draw coerce.
- [x] **Invalid-entry handling** — `_parse_typed_hand` validates the cells a scorer typed (points parse, `by`/`from` within 0-4) and rejects rather than coercing; `update_hand_points` echoes what was stored, since the encoding is lossy. `_parse_hand` stays tolerant for the OCR prefill, where a bad model guess should leave a blank cell rather than fail the sheet. Blank cells remain legitimate everywhere — that's how an unplayed row and a self-draw are entered. **Left alone:** the penalty field's coerce-to-0 (`update_seat_penalty`), which the review didn't flag and an existing test pins; it's a display-only sheet-balance figure.
- [x] **Score-entry 500-guards** — `int_param` on the id/version/round/table params, `number_or_none` on the score cells; `create_hand_points` coerces and validates all 16 cells *before* the transaction, so a bad request can't write a sheet and then 500.
- [x] **`scores_per_hand` phantom sheet** — 404 when the `(round, table)` has no seats.
- [x] **`WINDS` dedup** — `WIND_LETTERS` joins `WINDS` in `scoring/_common.py`; the `score_entry.py` and `scan.py` copies are gone.

**Files:** `score_entry.py`, `views/helpers.py` (the two coercion helpers), `scoring/_common.py`, `scoring/standings.py`, `scoring/visibility.py`, `scoring/stats.py`, `scoring/__init__.py`. The score-grid templates needed no change — the guards are all server-side.

**Tests:** `TestRiichiRoundTrip` (save with no `tp`, publish, scores reach the standings, final round still withheld, MCR still requires table points, Riichi round counts as complete), `TestBulkPayloadIntegrity` (mixed round/table rejected, unparseable cell rejected, blank cell still clears), `TestPruneBumpsVersion` (stale post-prune save gets 409), `TestPhantomScoreSheet`, plus malformed-cell/id guards on `update_hand_points`.

**Note on the 400 body:** the coercion helpers raise `BadRequest`, so the offending field name reaches the server log rather than the response body — Django renders a plain 400, and the score grid only shows an error pip. Matching `json_body`'s contract was worth more than a per-endpoint JSON error, which would have meant scattering try/except back through the views. A `handler400` that renders JSON for XHR would put the field in the body for every endpoint at once, if that's ever wanted.

**Risk:** moderate; well-covered area, but the atomic/lock change needs care under the existing optimistic-locking tests.

---

## Session 5 — Scan pipeline hardening

**Theme:** the anonymous OCR flow currently trusts callers with far more than a photo. All in `scan.py` + the queue/worker modules.

**Findings:** F13, anonymous-upload metering, worker resilience (scan + restore), `scan.py` 500-guards.

- [ ] **F13 — `scan_prefill` trusts client scores + self-validates** (`scan.py:371-440`) — have the client send `job_id`; read the scores server-side from `scan_queue.get_result(job_id)`; ignore `validate=true` from anonymous callers (require a role to validate).
- [ ] **Anonymous upload metering** (`scan.py:129-153`) — cap `file.size` in the view, verify the payload decodes as an image before staging, add a simple per-IP/tenant rate limit, and sweep stale job files (they're only cleaned on success today).
- [ ] **Worker resilience** — move `set_result` inside a guarded/retry block in `scan_worker.py:69` and `restore_worker.py:92-95` so a Redis blip at result-write time can't kill the loop or orphan the job. Refresh the `pending` marker's TTL on dequeue so a long backlog can't expire it (`scan_queue.py:22`).
- [ ] **`scan.py` 500-guards** — guard `int()` on raw GET params and client body keys (`scan.py:321-332,405`).

**Files:** `scan.py`, `scan_queue.py`, `management/commands/scan_worker.py`, `management/commands/restore_worker.py`.

**Tests:** anonymous `scan_prefill` cannot set `validated=True`; a body with hand values but a bogus `job_id` is rejected; oversized/non-image upload rejected (`test_scan.py`). Worker retry can be unit-tested by faking a `RedisError` on the first `set_result`.

**Risk:** moderate. The `job_id` contract change touches `scan.html`'s fetch — verify end to end.

---

## Session 6 — Multi-tenancy & isolation

**Theme:** tenancy wasn't carried into the filesystem, credential, and HTTP layers. Includes one migration (auto-applies on deploy — never ask the operator to migrate).

**Findings:** F9, F10, F12, `Tenant.subdomain` uniqueness, `player_rounds_rows` tenant filter, free-text `EMA_ID`, import atomicity.

- [ ] **F9 — login-link minting escalation** (`user_admin.py:218-237`) — apply the same containment guard `revoke`/`delete` already use: `if not request.user.is_superuser and not _memberships_contained(user, tenant): 403`.
- [ ] **F10 — shared import temp file race** (`admin_views.py:277-290`) — pass the uploaded file object straight to `load_workbook(attached_file)` (no disk hop), or use a per-request `tempfile.NamedTemporaryFile`.
- [ ] **Import atomicity** (`admin_views.py:513-527`) — wrap the multi-step wipe+import body in `transaction.atomic()` so a mid-import crash can't leave a half-imported tournament (same code as F10).
- [ ] **F12 — `USE_X_FORWARDED_HOST` host spoofing** (`apps/settings/prod.py:23` + `nginx/mahjong.conf.template`) — remove `USE_X_FORWARDED_HOST` (nginx already passes the correct `Host`), or add `proxy_set_header X-Forwarded-Host $host;` to every proxy block. Verify tenant resolution + microcache key stay consistent.
- [ ] **`Tenant.subdomain` DB uniqueness** (`models.py:4-6`) — add `UniqueConstraint(fields=['subdomain'])` + migration.
- [ ] **`player_rounds_rows` tenant filter** (`views/scoring.py:38-40`) — add `tenant=tenant` to the `Player` lookup (latent cross-tenant hole).
- [ ] **Free-text `EMA_ID`** (`admin_views.py:940-948`) — validate/normalize in `player_editor_save` exactly as the importer does (digits → `f"{int(v):08d}"`, else 400) so it can't crash the template export at `:589`.

**Files:** `user_admin.py`, `admin_views.py`, `apps/settings/prod.py`, `nginx/mahjong.conf.template`, `models.py` (+ migration), `views/scoring.py`.

**Tests:** tenant-A admin cannot mint a link for a user whose memberships aren't contained in A (`test_membership.py`/`test_security.py`); two imports don't cross tenants (harder — at least assert the file object is read directly, no shared path); duplicate subdomain rejected; non-numeric `EMA_ID` rejected at save and export survives. F12 is config — verify by test that a spoofed `X-Forwarded-Host` no longer changes `get_host()` (or document the nginx change if not unit-testable).

**Risk:** moderate. F12 is a settings change — confirm it doesn't break the legitimate dotted-subdomain routing (`test_dotted_subdomain.py`).

---

## Session 7 — Standalone, backup & ops hardening

**Theme:** disaster-recovery and deployment surface. Two of these sit exactly in the data-loss path.

**Findings:** F11, sqlite 0-byte snapshot, restore WAL ordering, standalone SECRET_KEY note, SFTP host key, `publish_target_test` probe, CI workflow, gunicorn umask comment, auth cookie, ipify call.

- [ ] **F11 — standalone `admin/admin` on 0.0.0.0** (`standalone/run.py:133,37`, `apps/settings/standalone.py:35`) — generate a random initial password and print it once (or force a change on first login); consider binding admin to loopback and exposing only display screens on the LAN.
- [ ] **sqlite 0-byte snapshot passes integrity** (`standalone_backup.py:77-83`) — back up to `dest.with_suffix('.tmp')` and `os.replace` into place only after `backup()` succeeds; unlink temp on failure.
- [ ] **restore deletes WAL before copy** (`standalone_backup.py:137-146`) — reorder: copy snapshot to tmp first, then delete `-wal`/`-shm`, then `os.replace`.
- [ ] **standalone SECRET_KEY note** (`apps/settings/standalone.py:28`) — document (and ideally warn on) that an ephemeral key makes stored publish secrets undecryptable; the launcher should persist the key on first run.
- [ ] **SFTP auto-accept host keys** (`publish/sftp_upload.py:98`) — default to `RejectPolicy`; require the operator to paste the `host_key` when configuring a target (the pin path already exists), or persist first-seen and reject changes.
- [ ] **`publish_target_test` SSRF-ish probe** (`admin_views.py:1133`) — acceptable for trusted staff; if hardening, log/rate-limit the probes.
- [ ] **CI workflow** (`.github/workflows/release.yml:14,105`) — scope `contents: write` to the release job only; pin `softprops/action-gh-release@v2` to a full commit SHA.
- [ ] **gunicorn umask comment** (`gunicorn.conf.py:2`) — add a comment noting the world-writable socket relies on container isolation (no code change needed).
- [ ] **auth cookie `secure` flag** (`apps/middleware.py:21`) — cosmetic; make `secure` conditional on the request scheme so the cache-bypass cookie works in dev/standalone (or document why it's prod-only).
- [ ] **ipify call** (`admin_display.html:135`) — make the third-party IP lookup click-to-run instead of on every page view.

**Files:** `standalone/run.py`, `apps/settings/standalone.py`, `standalone_backup.py`, `publish/sftp_upload.py`, `admin_views.py`, `release.yml`, `gunicorn.conf.py`, `apps/middleware.py`, `admin_display.html`.

**Tests:** `test_standalone_backup.py` — a failed snapshot leaves no listable/restorable file; restore copy-fail path doesn't destroy the live DB. SFTP policy default is `Reject` without a pinned key (`test_static_export.py`/publish tests).

**Risk:** low-moderate; backup tests already exist to build on.

---

## Session 8 — Correctness one-offs & code-quality cleanup

**Theme:** independent correctness bugs plus the remaining dead-code/duplication. Lowest risk; can be split across sittings. Fix each correctness item with a regression test; do the extractions last.

**Correctness:**
- [ ] **`desktop` positional round rebuild** (`views/public.py:108-116,226-228`) — use `sc['round_nb']` (and `player_table.get((pid, sc['round_nb']))`) instead of the positional index so a player who missed a round doesn't shift every later score/column; read xlsx per-round columns by round. Resolve the `standings.py:26` "seats always filled" assertion vs the `team_standings` bye-handling contradiction one way.
- [ ] **cross-table grid sizing** (`scoring/stats.py:22-30`, `print_views.py:37,53`) — size from `max(seat.table_nb)`/`max(seat.round_nb)` like `tournament_seating`; skip empty cells in `cross_positions` so public setup states don't 500.
- [ ] **team modal `None` kills script** (`modal_details_team.html:149`, `public_modals.py:111-113`) — pass `team_history_pos` (and the player variant) through `json_script` so a missing-round `None` renders as JSON `null`.
- [ ] **`details_player` 500s** (`public_modals.py:35-37`) — `get_object_or_404` and guard the `[…][0]` standings lookup.
- [ ] **draw-assign race → 409 not 500** (`admin_views.py:898-912`) — catch `IntegrityError` and return the designed 409 payload.
- [ ] **`algebraic_feasible` false-negative badge** (`seating.py:404-424`) — implement as `len(find_shifts(T, R, seed=0)) >= R`, or document the badge as conservative.
- [ ] **`measure()` crash swallowed** (`seating.py:472-481`) — derive player/round sets from the chart rows (defaultdicts) and drop the caller's blanket `except` (`admin_views.py:1588-1596`).
- [ ] **`detailed_scores` cache spam** (`public_modals.py:147-160`) — skip caching / 404 when `(round,table)` is outside the known seating range.

**Cleanup (dead code / duplication / readability):**
- [ ] Delete or mark test-only: `all_player_rounds` (`stats.py:450-472`).
- [ ] Remove dead `getTotalTime()` + handler (`admin_display.html:291-293,373`) and the unused `columns` param on `_score_columns`/`_paginate` (`display.py:200-202`).
- [ ] Extract `classify_hand(hand, wind)` + one placement helper to collapse the tally loop duplicated ×4 and placement ×2 in `stats.py` (194-225, 331-352, 538-559, 643-663; 227-237, 686-716) — this is the divergence-bug risk, worth doing carefully with the golden tests as guard.
- [ ] Extract one `_cached(prefix, request, full_view, compute)` wrapper for the six repeated cache-or-compute functions in `views/scoring.py`.
- [ ] Extract the reauth-gate fragment pasted ×3 (`admin_views.py:1689,1732,1756`) and unify the validated/filled table-key computation (`admin_views.py:183-189` vs `1631-1641`).
- [ ] Replace native `alert()` with `window.alertAction` inside admin-shell fragments (`admin_scores_per_table.html` ×2, `admin_users.html` ×7, `admin_publisher_overview.html` ×2, `admin_tenants.html` ×2). (Standalone pages keep native dialogs — per convention.)
- [ ] Back off the every-second `new Audio()` sound-unlock probe on unattended projectors (`display_counter.html:389-404`) — retry on `pointerdown`/`keydown` or after N tries.
- [ ] Readability: align `history_pos` shapes (drop the magic client-side `.slice(2)`; `standings.py:53-74` + `modal_details_player.html:317`, update golden files) and correct the `views/scoring.py:9-11` cache comment to state the real invalidation contract.

**Files:** `public.py`, `stats.py`, `standings.py`, `seating.py`, `public_modals.py`, `scoring.py`, `admin_views.py`, `display.py`, several templates, golden snapshots.

**Tests:** regression test per correctness item (sparse-score standings, oversized-table grid, team with a missing round, unknown player id, concurrent draw assign). The `stats.py` and `history_pos` refactors are guarded by the existing golden tests — expect to regenerate snapshots and eyeball the diff.

**Risk:** low individually; the `stats.py` extraction and golden-snapshot regen need attention so a refactor doesn't silently change a number.

---

## Coverage checklist (every open finding maps to a session)

| Finding | Session |
|---|---|
| F1 `/print_scores` public | S1 |
| F2 scorers WebSocket anon | S1 |
| F3 `scan_seats` leaks mp/tp | S1 |
| Ceremony stat-slide spoiler | S1 |
| F6 GET mutations (all) | S2 |
| `_apply_set_tournament` denylist | S2 |
| `phase` unvalidated / `add_mode` crash | S2 |
| `?logout=1` GET (×2) | S2 |
| admin `json.loads` / `ScreenMode.get` / `.last()` 500s | S2 |
| ✅ F4 `json.dumps\|safe` breakout (×5) | S3 |
| ✅ F5 draw-page `innerHTML` XSS | S3 |
| ✅ `scan.html` innerHTML error | S3 |
| ✅ CSV export escaping | S3 |
| ✅ team-draw resume overlay | S3 |
| ✅ client tp-ranking / getCookie / preview-grid dup | S3 |
| ✅ F7 Riichi save 500 | S4 |
| ✅ F8 Riichi unpublishable | S4 |
| ✅ publish/edit TOCTOU | S4 |
| ✅ first-seat-only lock | S4 |
| ✅ prune version bump | S4 |
| ✅ invalid-entry coercion / NULL MP | S4 |
| ✅ score-entry 500-guards | S4 |
| ✅ `scores_per_hand` phantom sheet | S4 |
| ✅ `WINDS` dedup | S4 |
| ✅ F8c Riichi standings/completeness (found in S4) | S4 |
| F13 `scan_prefill` client scores | S5 |
| anonymous upload metering | S5 |
| scan/restore worker resilience + TTL | S5 |
| `scan.py` 500-guards | S5 |
| F9 link-minting escalation | S6 |
| F10 import temp-file race | S6 |
| import atomicity | S6 |
| F12 `USE_X_FORWARDED_HOST` | S6 |
| `Tenant.subdomain` uniqueness | S6 |
| `player_rounds_rows` tenant filter | S6 |
| free-text `EMA_ID` | S6 |
| F11 standalone `admin/admin` | S7 |
| sqlite 0-byte snapshot | S7 |
| restore WAL ordering | S7 |
| standalone SECRET_KEY note | S7 |
| SFTP host-key policy | S7 |
| `publish_target_test` probe | S7 |
| CI workflow perms/pin | S7 |
| gunicorn umask comment | S7 |
| auth cookie secure flag | S7 |
| ipify call | S7 |
| desktop positional rebuild | S8 |
| cross-table grid sizing | S8 |
| team modal `None` | S8 |
| `details_player` 500s | S8 |
| draw-assign 409 race | S8 |
| `algebraic_feasible` badge | S8 |
| `measure()` crash | S8 |
| `detailed_scores` cache spam | S8 |
| `all_player_rounds` dead | S8 |
| `getTotalTime` / `_score_columns` dead | S8 |
| `stats.py` tally/placement dup | S8 |
| `views/scoring.py` cache-wrapper dup | S8 |
| reauth fragment / table-key dup | S8 |
| native `alert()` consistency | S8 |
| audio-probe backoff | S8 |
| `history_pos` shape / cache comment | S8 |
| `options()` mega-view decomposition | S2 (partial) / S8 |
