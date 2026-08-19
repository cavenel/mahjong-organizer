# Cleanup plan (post-refactor follow-ups)

A living backlog of cleanups surfaced during the public-release refactor. Prune
items as they're done (like the deferred-cleanup notes before them); this file
should shrink to nothing.

Everything here is **behaviour-preserving cleanup**, not bug-fixing — verify with
the golden snapshots (byte-identical except intended field changes) + full suite.

## Migration reset (do LAST, right before the public merge)

Goal: a pristine single-migration baseline for fresh public installs without
breaking the existing prod DB (which has 0001–00NN applied; deploy auto-runs
`migrate --noinput` on container boot).

- **Use `python manage.py squashmigrations mahj 0001 00NN`**, not a manual wipe.
  The squash carries `replaces=[…]`, so prod recognises the originals as applied
  and skips re-running; fresh installs run only the squashed migration.
- Sequence: land all remaining schema changes first, **then** squash once.
  Don't squash mid-stream. (`0013` dropping the dead timestamp columns is in;
  nothing else schema-touching is outstanding.)
- After the next prod deploy applies the squash, delete the replaced migration
  files (follow-up) → single clean `0001`.
- A truly pristine `0001` with no `replaces` metadata is possible via a manual
  reset, but needs a coordinated `migrate --fake` on prod — only if maximum
  cleanliness is wanted and the prod step is done deliberately (not via blind
  auto-migrate).

## Deliberately NOT doing

- **`cross_positions`** (endpoint, template, `print_cross_positions.html`) keeps
  its name: "cross positions" is the established who-meets-whom chart term, and
  "cross seats" is not a thing. The rest of the `Position → Seat` rename is done.
- User-facing wording that means a physical place at a table ("Table positions"
  print menu, wind positions) stays "position"; so do the rank-meaning `pos` /
  `pos_se` / `history_pos` keys and CSS `position:`.
