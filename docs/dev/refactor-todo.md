# Refactor working notes — deferred / follow-up items

Scratch list kept during the public-release refactor. Delete (or fold into
issues) before the public orphan commit in Phase 8.

## Deferred within/after Phase 2 (schema redesign)

- **Cosmetic: internal scoring output dict keys.** Model fields are now `points`
  and `wind`, but some internal scoring output dicts still use the old keys
  (`'pts'`, `'position'`) to avoid needless golden/template churn (they aren't
  model fields, so it's not a schema-readability issue). Candidate for tidy-up
  during the Phase 3 scoring-package restructure, if worthwhile.

- **`Player.save()` first-name parsing.** The old "if 'Player' in full_name"
  placeholder handling is gone (undrawn seats now render "Player #<n>" without
  fake player-list rows), which also removes the known-issue bug where a real name
  containing "Player" got corrupted. Keep an eye on the disambiguation loop moved
  into the import.

## Opportunistic fixes now unblocked by the fresh migration baseline

From docs/dev/known-issues.md — these were deferred pre-event only because a
schema migration was risky. The fresh Phase 2 baseline removes that constraint,
so consider doing them while the surrounding code is being rewritten:

- **Seat optimistic-lock / `version` field.** Add a `version` to `Seat` and make
  `update_positions_bulk` version-predicated (mirror `Hand`) to close the
  last-writer-wins race. (Not yet added — Seat currently has no version. Decide
  whether to include in Phase 2 or defer.)
- **Import validate-before-delete in a transaction.** The import still deletes
  before validating the workbook. Wrap parse+delete+create in one transaction and
  validate in memory first. Natural to fold into the Phase 2 import rewrite.

## Scope decisions taken (for the record)

- Player model: merged Player_data + Player into a single human `Player`. The
  draw is stored **once** on `Player.draw_number` (unique per tenant, null until
  drawn); `Seat` keeps only its own structural `draw_number` and has **no** player
  FK. The competitor at a seat = the Player holding that draw number. Re-drawing
  changes only `Player.draw_number`; seats are immutable structure. See
  docs/data-model.md. (User chose this over the redundant `Seat.player` FK, which
  the DB couldn't keep consistent.)
- Hands: prune-to-played-hands happens at validation time, so the entry UX and
  the per-cell optimistic-lock save are unchanged; validated sheets carry exactly
  the hands played. Draw = `win_by` NULL, self-draw = `win_from` NULL.
- **Deferred:** the plan's `variables` → `tournament` template context-key rename
  (~90 sites, silent-failure risk). Kept `variables`; the model is
  TournamentSettings, which is the substantive readability win.

## Phase 3 — done, with one deliberate stop-short

- scoring.py split into `mahj/scoring/` (visibility / standings / stats / _common
  + __init__ re-exporting the full surface). Visibility policy centralized in
  `visibility.py` (`publish_state`, `final_withheld_now`, `public_round_max`) — the
  end-of-tournament "withhold the final round" rule is stated once and shared by
  player_standings, tournament_seating and public_round_max. Goldens byte-identical.
- **Deferred:** the fuller ask was to replace the `check_final`/`force_all` boolean
  pair with a single `viewer` concept. Kept the flags (documented with their
  viewer-mode mapping in visibility.py) to avoid churning ~7 signatures + view
  callers and risking the reveal-masking. Collapsing to `viewer ∈ {public, admin,
  display}` is a safe follow-up if wanted.

## Later phases — reminders

- Phase 4: `mahj.ovh` still hardcoded in several templates/helpers (BASE_DOMAIN).
- Phase 7: author/venue strings (`© 2018 … Christophe Avenel` footer in
  admin.html; `"SE"` countrycourt and `'Sweden'`/`pos_se` host-nation ranking in
  scoring/admin_views) — decide what to neutralize vs keep configurable.
