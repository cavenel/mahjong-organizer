# Pre-event hardening audit

Logic/correctness review of the championship app ahead of a live European
championship. Focus: anything that produces a **wrong number, wrong order, wrong
flag, lost score, or a crash** on a live surface.

**Scope reviewed:** `mahj/scoring.py`, `mahj/views/{score_entry,scan,ceremony,public,public_modals,admin_views,display,scoring}.py`,
`mahj/{models,signals,consumers,scan_queue,webhook}.py`,
`mahj/management/commands/scan_worker.py`, the score-entry templates, and the
full test suite.

**Key architectural fact that bounds severity:** the official ranking
(`player_standings` / `team_standings`) is computed **only** from
`Position.minipoints` / `tablepoints`. `Hand` rows feed only stat highlights, the
detailed-hand modal, win/loss stats, and validation/completion badges. So
**hand-level bugs corrupt stats/badges, not the championship ranking**; only
`Position` bugs can corrupt the ranking.

**Status legend:** ✅ Fixed · 🔧 Open (fix proposed) · 🧭 Latent / process · ✔️ Verified correct

---

## A. Ranking correctness

### ✅ A1 — Standings ties wrong/non-deterministic under non-MCR (Riichi) rules — **FIXED** (`2a7c602`)
`_standings_rank_key` always included table points, but the non-MCR sort orders
by minipoints only. Three players on equal MP came out ranked 1/2/3 instead of
tied at 1, with the order decided by `rand_id`. Fixed across all four ranking
call sites (`scoring.py` player + team, `webhook.py`, `public_modals.py`) and the
tie key now mirrors the sort key per rule set. No-op for MCR (tuple equality is
order-independent in `groupby`; golden tests unchanged). Regression test added.
*This was the only ranking-correctness bug found.*

---

## B. Confirmed bugs

### ✅ B1 — MCR placement-rate stat silently dropped every tied table — **FIXED** (`451101b`)
- **Was:** `player_extra_stats` / `team_extra_stats` mapped a seat's table points to
  a place via `{4.0:1st, 2.0:2nd, 1.0:3rd, 0.0:4th}`. The score sheet *averages*
  `[4,2,1,0]` across minipoint-tied players, so ties produce fractional TP
  (3.0, 1.5, 0.5, 2.333…) that matched no key → the whole round was dropped from
  the placement counts, while percentages over the shrunken denominator still
  summed to 100%, hiding the loss. A player who tied for 1st three times showed
  "1st: 0%".
- **Fix:** Rank each seat within its own `(round, table)` — by table points for
  MCR, minipoints otherwise — with tied seats sharing a place (1, 1, 3, 4). No
  round is ever dropped; clean untied tables give identical results. Both rule
  branches folded into one `_placement_counts` helper. 3 regression tests added.

### ✅ B2 — Country flags: "Turkey" → no flag, "Korea" → North Korea — **FIXED** (`4dec78c`)
- **Was:** `_country_flag("Turkey")` returned `''` (pycountry renamed it
  "Türkiye"), so a Turkish player got no flag on the standings/EMA report **and
  could never be "Best European"** (`'' not in EUROPE`). `_country_flag("Korea")`
  returned `'kp'` (North Korea) instead of `'kr'`, showing the wrong country's
  flag on the projector and in the official EMA Country column.
- **Fix:** Explicit `_FLAG_ALIASES` map (`turkey→tr`, `korea`/`south korea`→`kr`,
  `chinese taipei→tw`) checked before the pycountry lookup. Tests added.
- **Residual:** I can't see the real participant list — other unusual country
  names could still mis-resolve via fuzzy match. **Eyeball the participant →
  flag mapping against the entry list before the event.**

### ✅ B3 — EMA results report mislabelled the discipline — **FIXED** (`4dec78c`)
- **Was:** `admin_print_EMA` correctly picked the MCR vs Riichi *template*, but the
  per-row "rules" column was hardcoded to `"Riichi"` — an MCR event's official
  submission file declared the wrong discipline.
- **Fix:** Use `variables.rules` for the discipline column.
- **Residual:** The "Countrycourt" column is still hardcoded `"SE"` (the
  organising Swedish federation). Left as-is because there's no per-event data
  source for it; **confirm "SE" is correct for this event** before submitting.

### ✔️ B4 — Hand-edit / clear / scan paths are NOT publish-locked — **NOT A BUG** (reviewed, by design)
Originally flagged because `update_hand_points`, `create_hand_points`,
`validate_score_sheet`, `clear_score_sheet` (`score_entry.py`) and `scan_prefill`
(`scan.py`) skip the `_round_is_published` check that the two `Position` paths
enforce. On review this is intended:
- **Publishing freezes the standings, not the hand detail.** `set_round_published`
  (`score_entry.py:401-406`) gates on Positions only (every seat needs `minipoints`
  + `tablepoints`); it checks nothing about `Hand` rows. So the per-hand detail and
  the `hand_nb=17` validation marker are legitimately backfilled *after* a round is
  published — typically during the next round. Hand data feeds only the detailed
  modal, the "biggest hand / most wins" stats, and the completion badge — never the
  ranking — so editing it post-publish is the expected workflow, not a leak.
- **`clear_score_sheet` is test-only.** Its sole caller is the fixtures toolbar in
  `admin_scores_per_table.html` (the `{% if subdomain == "test" %}` block); it is
  never rendered on the real event tenant. The destructive "wipes the validation
  marker on a published round" scenario can't happen through the live UI.
- **Residual (negligible):** the bare endpoints stay reachable via a hand-crafted
  authenticated (`is_scorer`) POST, so they aren't *enforced* read-only — but that's
  not part of any real workflow. No action.

### 🔧 B5 — `Position` has no optimistic lock; `update_positions_bulk` is last-writer-wins — MEDIUM
- **Where:** `models.py` `Position` (no `version` field); `score_entry.py`
  `bulk_update(..., ['minipoints','tablepoints'])`. The publish check is also a
  TOCTOU outside the `transaction.atomic()` block (narrow window).
- **What:** `Hand` has a `version` optimistic lock (409 on conflict). The
  score-bearing `Position` model has none — two scorers submitting the same table
  row → the later write silently clobbers the earlier with no 409.
- **Impact:** Silent lost score update on the model that decides the ranking. Low
  likelihood if one scorer owns a table, but it's the highest-stakes unprotected
  path.
- **Fix:** Add a `version` field + version-predicated update (mirror the Hand
  path), or re-read inside the transaction and reject on change.
- **Risk:** Medium-High this close to the event (schema migration + front-end
  plumbing on the live entry path). **Operational mitigation: one scorer per
  table.**

### 🔧 B6 — Scan ingestion can clobber manual work and writes unvalidated seats — MEDIUM *(only if camera-scan is used live)*
- **B6a** `scan.py`: the only overwrite guard is `existing.filter(pts__gt=0).exists()`.
  If a scorer corrected only **zero-point** hands, the gate passes and a re-scan
  overwrites their `confidence=1.0` edits with low-confidence OCR — no 409.
- **B6b** `_write_hand` does a **blind** `update(version=F('version')+1, …)` with no
  `version=` predicate, and the `already_filled` read isn't atomic with the writes
  → a scorer edit landing between gate and write is clobbered with no 409.
- **B6c** `pts` / `win_by` / `win_from` are written verbatim from OCR — no range
  check (seat ∈ 1..4 / 0), no `win_by != win_from`, no "winner ⇒ pts ≥ 8", no
  four-player balance check. A garbled digit silently mis-attributes or drops a
  win. `scan_prefill` is also intentionally unauthenticated.
- **Impact:** Corrupted hand data/stats and lost manual corrections. Standings
  unaffected.
- **Fix:** Tighten the overwrite gate, add a version/already-processed predicate,
  validate seat range + balance before persisting.
- **Risk:** Medium; touches the OCR happy path. **First confirm the scan feature is
  used at this event** — if not, deprioritise.

### 🔧 B7 — Excel import is destructive *before* validation — MEDIUM *(pre-event)*
- **Where:** `admin_views.py` deletes all `Player` / `Player_data` up front, then the
  generic `except` wipes Players, Hands, Positions, PublishedRounds, Schedule and
  sets `nb_rounds=0`.
- **Triggers:** any malformed sheet (missing/misnamed sheet; a text `nb_rounds`
  hitting `int + str`) → **the entire live tournament is wiped** rather than
  rejected. `nb_rounds = opt_vals[2] or 0` with no int/positivity cast → `0`/blank
  creates **zero positions** (import "succeeds" with no seating). Duplicate
  `rand_id` in the sheet → one player silently shadowed (seated twice / another
  never seated); an unknown/blank `rand_id` cell → `player=None` on a non-null FK
  → IntegrityError → wipe.
- **Fix:** Validate the whole workbook into memory (sheet names, `nb_rounds`
  positive int, `rand_id` unique & every seating cell resolvable, player count
  divisible by 4) **before** any delete; wrap in a transaction.
- **Risk:** Medium (rewrites the import flow). **Operational mitigation: back up
  first, import into a fresh/staging tenant.**

### 🔧 B8 — `cross_positions` print crashes on non-dense `rand_id` — LOW
`print_views.py` indexes `cross[...][player.rand_id - 1]` assuming `rand_id ∈ 1..n`;
draw numbers like 101–116 → IndexError → 500 on that one print page. Fix: map
`rand_id` → dense index. Risk: trivial.

### ✔️ B9 — `penalty` not applied to standings — **NOT A BUG** (reviewed, by design)
Initially flagged as a possible mis-score: standings rank on raw `minipoints` and
never add `penalty`. Confirmed with the user that this is correct — in the scoring
workflow the penalty is **already folded into the player's MP** on the paper, so the
entered `minipoints` *is* the after-penalty score. The separate `penalty` field is
only a sheet-balance reconciliation entry (the four players' MP no longer sum to
zero once a penalty leaves the game). Ranking on MP therefore *is* ranking on the
after-penalty total, and the model comment is accurate. Applying `penalty` on top of
MP would **double-count** it — so the current behaviour is the right one. No change.

---

## C. Latent / process (likely no code change pre-event)

### 🧭 C1 — `tournament_seating` vs `player_standings` use different end-of-tournament masking — LOW reachability
The two compute the suspense state separately. **In the normal end-game (all rounds
complete, last published `reveal=0`) they agree — no leak** (traced). A divergence
(seating leaks the held-back final round while standings hide it) requires a
*published* round to be *incomplete*, which the Position publish-lock prevents
through normal endpoints (would need a Django-admin/DB edit). Fix: drive both from
one shared helper. Risk: Medium (touches delicate masking) — defer given low
reachability.

### 🧭 C2 — Raising `nb_rounds` mid-suspense un-hides the final round — MEDIUM reachability (operator error)
`_last_round_reveal(nb_rounds)` reads the row at the *new* last round; bumping
`nb_rounds` during the `reveal=0` ceremony window makes it return `None` → masking
switches off → standings/seating/stats go fully public, bypassing the ceremony.
Mitigation: don't edit `nb_rounds` during the event.

### ✅ C3 — `reveal_level` docstring described a 1..11 progressive reveal that doesn't exist — **FIXED**
`models.py`. Only 0/100 are ever written; two consumers compare differently
(`!= 100` vs `== 0`). If anyone implemented the documented scheme, intermediate
values would make surfaces disagree. The stale wording was replaced with an
accurate description (0 = hidden suspense, 100 = visible; reveal is client-side).

### ✅ C4 — `_roll_up` seeded `best_value = 0` — **FIXED**
If every round's top minipoints was ≤ 0 (minipoints are zero-sum, so negatives are
normal), an all-negative field yielded an empty `mp_max` overall winner. Now seeds
`None` so the first non-empty round sets the bar; negative and zero tops are picked
correctly. Regression tests added.

### ✅ C5 — `schedule:{subdomain}` cache never invalidated — **FIXED**
`public.py`, 300s TTL, no deleter — re-importing the schedule showed the old one for
≤300s. The key is now deleted in `_invalidate_leaderboard` (which the import path
already calls).

### 🧭 C6 — Detailed per-table modal stale ≤30s after a hand edit — LOW (by design)
Hand writes don't bump `leaderboard_gen`; bounded by a 30s TTL. Documented/intended.

### 🧭 C7 — Name handling oddities — LOW
`Player.save()` corrupts real names containing "Player" (substring match) and gives
mononyms an empty last name; the import's first-name disambiguation uses `break`
(aborts the loop at the first placeholder). Team grouping is case-sensitive
(whitespace is handled). Cosmetic.

---

## D. Verified correct (checked, no bug)

- **Hand optimistic lock** (`update_hand_points`): version-predicated, 409 on
  conflict, monotonic via `F('version')+1`; random-fill + scan keep it monotonic.
- **`hand_nb=17` semantics** (`pts=1`=validated, `0`/absent=not final) consistent
  across all readers/writers; the placeholder is always `pts=0`.
- **Publish gating:** can't publish with a gap, can't publish an incomplete/empty
  round; unpublish cascades forward. Normal end-game suspense does not leak the
  final round (standings + projector holding-screen both verified).
- **Cache generation pattern** (`leaderboard_gen`): race-safe; a real write orphans
  old keys; the only state that changes *published* output invalidates.
- **Websocket consumers:** tenant-scoped group names (no cross-tenant bleed);
  unknown messages ignored; broadcast failures swallowed so a Redis blip can't 500
  a committed write.
- **Display rotation:** `rotation_time=0`, empty page list, 0 screens all guarded
  (no modulo-by-zero / IndexError).
- **Scan queue:** atomic `blpop`, no double-processing/ordering corruption; a worker
  crash leaves a job that simply reports `expired`.
- **Win/loss bucketing** and **`_top_win_streaks`** (`win_by=0` can't form a phantom
  group); all division-by-zero guards present; `tournament_seating` grid sized from
  the actual max round/table so no index overflow given valid 1..4 positions.

---

## Priority for remaining open items

| Item | Severity | Effort | Risk | Recommendation |
|---|---|---|---|---|
| B7 import validation-before-delete | Med | Med | Med | Harden, or mitigate operationally (backup + staging) |
| B5 `Position` optimistic lock | Med | High | High | Operational mitigation (one scorer/table); don't migrate pre-event |
| B6 scan hardening | Med | Med | Med | Only if camera-scan is used live |
| B8 `cross_positions` IndexError | Low | Low | Low | Fix if that print page is used |
| C1 / C2 masking consistency / `nb_rounds` | Low-Med | Med | Med | Process discipline; defer code change |
