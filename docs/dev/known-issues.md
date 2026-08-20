# Known issues (developer notes)

The risks this codebase knowingly carries, for maintainers — not user-facing.
There is no "open" list: at release every known issue was either fixed (the
fixes and their reasoning are in the git history) or accepted here, with the
reasoning written down so it isn't re-opened as an oversight.

One severity floor frames everything below: standings are computed **only**
from seat minipoints/tablepoints. Hand-level data feeds stats, badges and the
detailed-scores modal — never the final ranking.

## Accepted

Risks weighed and kept. Each entry says what happens, why it is the right
trade, and what would have to change to revisit it.

- **Standings reads can lag live score entry by up to `SUB_CACHE_TTL` (5
  minutes) — for every score field, penalty included.** This is the documented
  invalidation contract, stated in full in `views/scoring.py`: score entry
  writes with `update()`/`bulk_update()` (which fire no signals) and does not
  bust the leaderboard caches; scorers see their own edits through the
  WebSocket row sync, and the caches are invalidated explicitly by the events
  that change what the standings should say (publish/unpublish, import, reset,
  the draw). A penalty edit behaving exactly like a minipoints edit is the
  correct, homogeneous outcome — making `update_seat_penalty` the one write in
  the app that busts the cache would be the inconsistency. The stale value is
  cosmetic (penalty is display-only for ranking), appears on one modal, and
  self-corrects within the TTL.

- **An import that fails deep in the parse wipes the tournament to empty.**
  The import is a full replace by design, and a half-loaded tournament is worse
  than none — the `except` deliberately clears every player/seating/score table
  rather than leave a ghost. The realistic wrong-file mistakes never get that
  far: `_precheck_template` rejects a missing sheet, an unreadable rounds
  count, an unplayable player count, colliding draw numbers, or a file that
  isn't a workbook at all *before anything is deleted*, with the tournament
  untouched. What remains wipe-to-empty is a workbook that passes those checks
  and still fails mid-parse (a bad cell deep in the player list, a broken
  seating chart) — rare, loudly reported on the options page, and run from a
  pre-tournament action behind a confirm dialog that names what will be
  erased. Full parse-then-delete validation would mean restructuring the
  longest function in the codebase with no way to prove equivalence except
  importing every workbook shape by hand.

- **A navigate-away flush can be rejected without telling the scorer.** The
  score grid's debounced save is flushed with `sendBeacon` on `beforeunload`
  and `pagehide` (both events, because mobile browsers often skip
  `beforeunload`) — but a beacon is fire-and-forget by nature: at `pagehide`
  there is no page left to show a dialog on, and `sendBeacon` reports only
  whether the browser queued the request. So a flush that lands on a round
  published in the meantime is rejected (correctly — the round is locked) and
  the scorer isn't told at that moment. Accepted because the failure is visible
  on next load, the scorer holds the paper sheet, and unpublish → correct →
  republish fixes it in seconds. Handling it "properly" would mean a
  reconcile-on-return mechanism for a 2-second window — new surface for a
  rare, self-revealing case.

- **OCR values are not range- or balance-checked beyond structural bounds.**
  The scan is a *prefill*, not a save. `_parse_hand` is tolerant by design — a
  garbled digit leaves the scorer an empty cell rather than failing the sheet —
  and it already bounds what it stores: seats via `_seat` (1–4 or null),
  `hand_nb` to 1–16, `win_from == win_by` collapsed to a self-draw. What it
  does not enforce is the MCR 8-point minimum or the four-player balance; the
  sheet flags a value below 8 in red (`markValueValidity`), and a scorer must
  validate the sheet before it counts. Rejecting such rows server-side would
  make the OCR *worse*: a rejected row is a blank cell the scorer must notice,
  where today it is a wrong cell the sheet paints red. The human check is the
  design, and it is a better check.

- **Team grouping is case-sensitive.** `"Dragons"` ≠ `"dragons"`. The import's
  "team size must be 4" check rejects a case-split team rather than silently
  splitting it, so the failure mode is a clear import error before play, not
  corrupt standings during it. Cosmetic, guarded, and touching team identity
  for it risks more than it fixes.

## Invariants worth preserving (verified correct — do not "fix")

- **Penalty is display-only for ranking.** Standings rank on raw minipoints and
  never add `penalty`; the entered minipoints is already the after-penalty score.
  The `penalty` field is a sheet-balance reconciliation entry only. Adding it on
  top of MP would double-count.
- **Publishing freezes the standings, not the hand detail.** The publish gate
  checks only that every seat has minipoints+tablepoints; per-hand detail and the
  table-validation marker are legitimately backfilled after a round is published.
  Editing hand data post-publish is the expected workflow.
- **End-of-tournament suspense masking** must stay driven from one shared cutoff
  helper so standings, seating, detail modals and winner cards mask identically
  (see the visibility policy in the scoring package). Raising `nb_rounds` during
  the ceremony-pending window un-hides the withheld final round — an operator
  hazard, not a code path to leave un-centralized.
- **Two "position" spellings survive the `Position → Seat` rename on purpose.**
  `cross_positions` (endpoint, template, `print_cross_positions.html`) keeps its
  name: "cross positions" is the established who-meets-whom chart term, and
  "cross seats" is not a thing. User-facing wording that means a physical place
  at a table ("Table positions" print menu, wind positions) stays "position";
  so do the rank-meaning `pos` / `pos_se` / `history_pos` keys and CSS
  `position:`. None of these are missed renames.
