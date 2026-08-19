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

**Findings:** F6, `_apply_set_tournament` allowlist, `phase` whitelist, `add_mode` GET crash, `?logout=1` (×2), plus the admin-side robustness 500s that live in the same code (`json.loads` guards, `ScreenMode.get`/`.last()` guards).

- [ ] **F6 — require POST on every mutating action** and move params into the body: `ceremony_control?action=publish` (`ceremony.py:203-248`), screen/mode CRUD + `set_all_views`/`set_mode`/`rm_mode` (`admin_views.py:1394-1459`), `set_tournament` (`admin_views.py:1226-1277`), `update_screen_view`/`update_screen_name` (`display.py:313-335`). Return 405 otherwise. Convert the template anchors/fetches: `admin_display.html:49,438,444`, `admin_ceremony.html:290`, `admin.html:202` (logout).
- [ ] **Whitelist `phase`** against `('idle','blank','teams','players','stat')` (`ceremony.py:236`); reject others so an unknown value can't blank every display.
- [ ] **`add_mode` GET crash** — folds out naturally once the action is POST-only with a validated body.
- [ ] **`?logout=1` → POST** (`admin_views.py:1325`, `public.py:29`) using Django's POST logout.
- [ ] **`_apply_set_tournament` allowlist** (`admin_views.py:1226-1253`). Replace the denylist with an explicit per-page field allowlist (display page: layout fields only; settings page: identity/format fields) so a display operator can't rewrite `nb_rounds`/`rules`/`has_teams`/`public_url`.
- [ ] **Admin 500-guards in the same code** — wrap `json.loads(request.body)` (`admin_views.py:878,931`) in the try/except pattern already used by `admin_team_draw_save`; use `get_object_or_404`/`.filter().first()` for `ScreenMode`/`Screen` and guard `.last().delete()` on empty querysets (`admin_views.py:1406,1424,1442`).
- [ ] **Optional structural payoff** — begin decomposing `options()` (`admin_views.py:1322-1806`) by lifting these mutations into their own POST-only URLs in `apps/urls.py`; a `{page: (required_role, renderer)}` dispatch table makes a missing role-gate impossible to overlook. Do only as much as safely fits.

**Files:** `ceremony.py`, `admin_views.py`, `display.py`, `public.py`, `apps/urls.py`, templates (`admin_display.html`, `admin_ceremony.html`, `admin.html`).

**Tests:** each converted endpoint returns 405 on GET and works on POST with CSRF (extend `test_security.py`, which already tests GET-vs-POST per endpoint); a display operator POSTing `set_tournament` with a non-allowlisted field is rejected; unknown `phase` rejected; stale/garbage `ScreenMode` id returns 4xx not 500.

**Risk:** HIGH for UI regressions — the templates drive these via `<a href>`/`$.post`. Manual click-through of the display/ceremony/settings admin pages required in addition to tests.

---

## Session 3 — XSS escaping + frontend de-duplication

**Theme:** player/team names are user-controlled; every injection point must escape. While in these exact templates, fold in the client-side duplication the review flagged.

**Findings:** F4, F5, `scan.html` innerHTML, CSV-escaping, team-draw resume-overlay bug, client-side duplication (MCR tp-ranking, `getCookie`, preview-grid).

- [ ] **F4 — five `json.dumps`+`|safe` injections** → `{{ …|json_script:"id" }}` + `JSON.parse(textContent)`: `display_ceremony.html:44` (public!), `admin_player_draw.html:38-43`, `admin_team_draw.html:32-37`. Update the corresponding view payload construction (`views/display.py:64`, `admin_views.py:772-774,859-860`) only if the key names change.
- [ ] **F5 — draw-page `innerHTML` interpolation** → apply the existing `esc()` helper (defined in `display_ceremony.html:46-50`) or `textContent` at every site: `admin_player_draw.html:138-143,231,397,403,453,480,510-514`; `admin_team_draw.html:242,356,360,463,514,525,566,570,798,802`. Move `onclick="confirmTeam('${t.name}…')"` to an event listener keyed on team **id**, not name.
- [ ] **`scan.html:213`** — render server error text with `textContent`, not `innerHTML`.
- [ ] **CSV export escaping** (`admin_player_draw.html:536`, `admin_team_draw.html:633,787`) — double embedded `"` and quote newlines so a name with a quote doesn't corrupt the re-importable row.
- [ ] **team-draw resume overlay** (`admin_team_draw.html:186-197`) — on resume with `paused=true`, unhide `#pause-overlay` (mirror `admin_player_draw.html:588`).
- [ ] **De-dup while here** — extract the MCR table-point ranking (`get_tp` + `sortWithIndeces`, duplicated in `admin_scores_per_table.html:236`, `admin_scores_per_hand.html:196` (this copy also leaks globals), `modal_detailed_scores.html:137`) into one file under `static/js/`; extract `getCookie('csrftoken')` (×3) and the preview-grid/enlarge-modal block duplicated between `admin_display.html` and `admin_ceremony.html`.

**Files:** the templates above + one or two new `static/js/` files + minor view payload tweaks.

**Tests:** a template render / integration test that a player named `</script><script>…` and a team name with `"`/`<` appear escaped in the response HTML (extend `test_display_admin.py` / `test_player_draw.py` / `test_team_draw.py`). Note per memory: Tailwind rebuilds each Docker build, so new classes are fine; but new `static/js/` files must be referenced via `{% static %}`.

**Risk:** moderate. `json_script` changes the read-side JS (`JSON.parse(textContent)`) — verify each consumer. Overlaps templates with S2, so do S2 first.

---

## Session 4 — Riichi path + score-entry integrity

**Theme:** all in `score_entry.py` and the score grid; one mental model (the save/validate/publish loop). The Riichi path has clearly never been run — add the missing test coverage as the centrepiece.

**Findings:** F7, F8, publish/edit TOCTOU, first-seat-only lock, `_prune_to_played_hands` version bump, invalid-entry coercion + NULL-MP, score-entry 500-guards, `WINDS` dedup, `scores_per_hand` phantom sheet.

- [ ] **F7 — Riichi save `KeyError: 'tp'`** (`score_entry.py:390`) → `entry.get('tp')` and `entry.get('mp')`.
- [ ] **F8 — Riichi rounds unpublishable** (`score_entry.py:457`) → gate the `tablepoints`-non-null completeness check on `tournament.rules == "MCR"`.
- [ ] **Publish/edit TOCTOU** (`score_entry.py:406-414,454-477`) — wrap check+write in one `transaction.atomic()` with `select_for_update()` on the `PublishedRound` row.
- [ ] **First-seat-only lock** (`score_entry.py:398-417`) — reject payloads whose seats don't all share one `(round_nb, table_nb)`, then check the publish lock on that pair.
- [ ] **Prune version bump** (`score_entry.py:282-285`) — add `version=F('version')+1` to the blank→draw coerce update so a stale second device gets a 409.
- [ ] **Invalid-entry handling** (`score_entry.py:50-78`) — reject (400 with the offending field) instead of silently coercing when `by`/`from` is outside 0-4 or points fail to parse on a non-blank cell; echo stored values in the save response. Same for non-integer MP silently saved as NULL (`score_entry.py:385-388`).
- [ ] **Score-entry 500-guards** — guard `int(e['id'])` / `entry['mp']` / `entry['tp']` (`:377,385-390`); move `int(round_nb)` before the transaction in `create_hand_points` (`:217`); guard `int(POST[...])` on version/params (`:227,296-297,327-328`).
- [ ] **`scores_per_hand` phantom sheet** (`score_entry.py:136-138`) — 404 when no Seats exist for the requested `(round,table)` so a crafted URL can't mark a round "open" in the stats.
- [ ] **`WINDS` dedup** — single source for `['E','S','W','N']` (currently `score_entry.py:22`, `scan.py:309`, and a long-name variant in `scoring/_common.py:14`).

**Files:** `score_entry.py`, score-grid templates (`admin_scores_per_table.html`, `admin_scores_per_hand.html`), `scoring/_common.py`.

**Tests:** **a full Riichi round-trip** (enter minipoints with no `tp`, complete, publish, verify standings) using the `riichi_tournament` fixture — this is the gap that let F7/F8 ship; malformed-cell rejection; a mixed-round payload is rejected; a concurrent stale-version save after prune gets 409.

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
| F4 `json.dumps\|safe` breakout (×5) | S3 |
| F5 draw-page `innerHTML` XSS | S3 |
| `scan.html` innerHTML error | S3 |
| CSV export escaping | S3 |
| team-draw resume overlay | S3 |
| client tp-ranking / getCookie / preview-grid dup | S3 |
| F7 Riichi save 500 | S4 |
| F8 Riichi unpublishable | S4 |
| publish/edit TOCTOU | S4 |
| first-seat-only lock | S4 |
| prune version bump | S4 |
| invalid-entry coercion / NULL MP | S4 |
| score-entry 500-guards | S4 |
| `scores_per_hand` phantom sheet | S4 |
| `WINDS` dedup | S4 |
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
