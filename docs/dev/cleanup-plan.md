# Cleanup plan (post-refactor follow-ups)

A living backlog of cleanups surfaced during the public-release refactor, after
the `variables → tournament`, scoring-dict-key, and reveal-masking work landed.
Prune items as they're done (like the deferred-cleanup notes before them); this
file should shrink to nothing.

Everything here is **behaviour-preserving cleanup**, not bug-fixing — verify with
the golden snapshots (byte-identical except intended field changes) + full suite.
The one genuine bug the sweep found (leaderboard/seating cache invalidation not
matching the new `full_view` key) is already fixed.

## The big one: `Position → Seat` naming

The DB model was renamed `Position → Seat`, but "position" lingers ~280 places.
Worth doing (it's the direct analog of `variables → tournament`) and it removes a
real ambiguity: `positions` (a list of `Seat` rows) reads confusingly next to
`pos` / `pos_se` / `history_pos` (which mean *rank*, and must stay). Do NOT touch
the rank-meaning `pos*`, CSS `position:`, or wind-position wording.

Split by risk — do the safe tier, hold the wire tier:

### Safe / internal (do now, one commit each)
- **`positions` / `position_vals` / `positions_map` locals + kwargs → `seats`.**
  ~170 sites across `scoring/standings.py`, `scoring/stats.py`, `views/scoring.py`,
  `views/public.py`, `views/print_views.py`, `views/public_modals.py`,
  `views/score_entry.py` (+ ~30 in tests). Pure internal identifiers.
- **`_json` functions that don't return JSON → drop the suffix.**
  `scores_per_table_json` (returns a nested Python grid of `Seat`/dicts),
  `scores_per_player_json` (returns `list[dict]`), `player_rounds_json` (plain
  Python). ~27 call sites. Pick names that don't collide with the `mahj/scoring/`
  originals (e.g. `_grid` / `_rows`). The template context key `scores_json` is
  the same misnomer but a separate, optional cosmetic rename.
- **`PositionForm`** (`views/helpers.py`) — dead *and* stale-named: never
  instantiated (only its own def + the re-export). Delete it (see Dead code).
- **`position_div`** CSS class in `admin_scores_per_table.html` → `seat_cell`
  (self-contained: one file's class + its own JS).

### Risky / wire (defer, or do as a clearly-flagged separate change after merge)
Same rolling-deploy caveat as the WS/DOM renames, plus URLs may be bookmarked or
printed. Rename path + `name=` + every hardcoded JS/href/test string in lockstep:
- **Endpoint names**: `update_position_penalty`, `update_positions_bulk`,
  `scan_positions`, `cross_positions` (weakest case — "cross positions" is an
  established who-meets-whom chart term) → `*_seat*`.
- **Wire/JSON payload key `positions`** (WS `_row_payload`, scan response,
  `update_positions_bulk` body) → `seats`. Touches Python + `admin_scores_per_table.html`
  + `admin_publisher_overview.html` + `scan.html` together.
- **`scan.py:339` inner `'position': p.wind`** — redundant with the sibling
  `'wind'` key and appears unused by the frontend (`scan.html` only reads
  `data.positions.length`). Verify no consumer, then drop.
- **`restore_worker.py` `AS positions` / `db_counts.positions`** — counts `Seat`
  rows; surfaces in `admin_database_restore.html`. → `seats`.

## Dead code (safe deletes)

- **`PositionForm`** (`views/helpers.py`) — never instantiated. Delete + drop the
  `views/__init__.py` re-export.
- **Four write-only timestamp fields, never read anywhere** (each needs a schema
  migration — batch them, then fold into the pre-publish squash):
  - `ScoreSheet.updated_at`, `CeremonyState.updated_at`, `PublishedRound.published_at`
    (all `auto_now`) and `Screen.last_refresh` (`auto_now_add`, orphaned
    screen-heartbeat scaffolding). Confirmed: each field name's only occurrence is
    its own definition; none of these models is serialised wholesale.
- Note: `Hand.win_from_player()` is only used by `Hand.__str__` — **keep** it (not
  truly dead; mirrors `win_by_player()`).

## Comments AND docs describing history, not current state

The same "describe what it is now, not the diff" principle applies to prose docs
written during the refactor — a public reader has no "before" to compare against.
Reword:
- `scoring/seating.py:6` — "so the app *no longer depends* on an Excel seating
  sheet…" → state current behaviour.
- `views/admin_views.py:340` — "first empty last-name cell *used to drop*…" →
  trim the historical-bug note.
- `docs/data-model.md:100` — heading "## Ranking **(unchanged)**" → just
  "## Ranking" (unchanged relative to the pre-refactor schema — meaningless once
  merged). Re-scan the dev docs for similar refactor-relative asides.
- Leave genuine back-compat justifications (`routing.py:9` legacy WS alias) and
  migration data-history comments.

## Migration reset (do LAST, right before the public merge)

Goal: a pristine single-migration baseline for fresh public installs without
breaking the existing prod DB (which has 0001–00NN applied; deploy auto-runs
`migrate --noinput` on container boot).

- **Use `python manage.py squashmigrations mahj 0001 00NN`**, not a manual wipe.
  The squash carries `replaces=[…]`, so prod recognises the originals as applied
  and skips re-running; fresh installs run only the squashed migration.
- Sequence: land all remaining schema changes first (the dead timestamp fields),
  **then** squash once. Don't squash mid-stream.
- After the next prod deploy applies the squash, delete the replaced migration
  files (follow-up) → single clean `0001`.
- A truly pristine `0001` with no `replaces` metadata is possible via a manual
  reset, but needs a coordinated `migrate --fake` on prod — only if maximum
  cleanliness is wanted and the prod step is done deliberately (not via blind
  auto-migrate).

## Suggested order

1. `positions → seats` internal rename (safe, biggest, disambiguates rank).
2. `_json` misnomer rename (safe).
3. `PositionForm` removal + `position_div` (safe, small).
4. Dead timestamp fields (one migration).
5. History-comment rewording (trivial).
6. *(optional, post-merge)* the wire tier: endpoints + WS/scan payload keys.
7. **Last, pre-publish:** squash migrations.
