# Cleanup plan (post-refactor follow-ups)

A living backlog of cleanups surfaced during the public-release refactor. Prune
items as they're done (like the deferred-cleanup notes before them); this file
should shrink to nothing.

Everything here is **behaviour-preserving cleanup**, not bug-fixing — verify with
the golden snapshots (byte-identical except intended field changes) + full suite.

## Migration squash follow-up (after the next prod deploy)

The 0001–0015 history is squashed into
`0001_initial_squashed_0015_seat_version`. Fresh installs run only the squash;
prod recognises the originals as applied via its `replaces = […]` and records
the squash as applied without re-running anything. The data backfills were
marked `elidable=True` before squashing (they touch only rows that predate
their own schema change), so the squash carries no `RunPython` at all.

Remaining, once the next prod deploy has run (i.e. every instance has the
squash recorded as applied) — the standard Django procedure:

- delete the 15 replaced migration files;
- remove the `replaces = […]` attribute from the squashed migration
  (that attribute is what marks it as a squash);
- optionally rename it to a plain `0001_initial`.

## Deliberately NOT doing

- **`cross_positions`** (endpoint, template, `print_cross_positions.html`) keeps
  its name: "cross positions" is the established who-meets-whom chart term, and
  "cross seats" is not a thing. The rest of the `Position → Seat` rename is done.
- User-facing wording that means a physical place at a table ("Table positions"
  print menu, wind positions) stays "position"; so do the rank-meaning `pos` /
  `pos_se` / `history_pos` keys and CSS `position:`.
