# Cleanup plan (post-refactor follow-ups)

A living backlog of cleanups surfaced during the public-release refactor. Prune
items as they're done (like the deferred-cleanup notes before them); this file
should shrink to nothing.

Everything here is **behaviour-preserving cleanup**, not bug-fixing — verify with
the golden snapshots (byte-identical except intended field changes) + full suite.

## Dead code (safe deletes)

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
- `mahj/seating.py:6` — "so the app *no longer depends* on an Excel seating
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

1. Dead timestamp fields (one migration).
2. History-comment rewording (trivial).
3. **Last, pre-publish:** squash migrations.

## Deliberately NOT doing

- **`cross_positions`** (endpoint, template, `print_cross_positions.html`) keeps
  its name: "cross positions" is the established who-meets-whom chart term, and
  "cross seats" is not a thing. The rest of the `Position → Seat` rename is done.
- User-facing wording that means a physical place at a table ("Table positions"
  print menu, wind positions) stays "position"; so do the rank-meaning `pos` /
  `pos_se` / `history_pos` keys and CSS `position:`.
