# Known issues (developer notes)

Open correctness and robustness issues, for maintainers — not user-facing. None
of them affect the championship ranking: standings are computed **only** from
seat minipoints/tablepoints, so the hand-level bugs below can corrupt
stats/badges and the detailed-hand modal, but never the final ranking.

## Open

- **Seat has no optimistic lock (score entry is last-writer-wins).** The
  score-bearing seat model has no `version` field, unlike `Hand`. Two scorers
  submitting the same table row → the later `bulk_update` silently clobbers the
  earlier with no 409. The publish check is also a TOCTOU just outside the
  surrounding transaction. Fix: add a `version` field + version-predicated
  update (mirror the Hand path). Interim mitigation: one scorer per table.

- **Excel import is destructive before validation.** The import path deletes
  Players/Schedule/Hands/Seats/PublishedRounds up front, then a broad `except`
  wipes everything and sets `nb_rounds=0`. Any malformed sheet (missing/misnamed
  sheet, non-int `nb_rounds`, duplicate/blank `rand_id`, player count not
  divisible by 4) can erase a live tournament instead of being rejected. Fix:
  validate the whole workbook in memory, then do all deletes+creates in one
  transaction. Interim mitigation: back up first, import into a fresh/staging
  tenant.

- **Penalty edit on a published round doesn't bust the detailed-modal cache.**
  Saving `penalty` with `update_fields=['penalty']` never bumps
  `leaderboard_gen`, so the detailed modal shows a stale penalty publicly for up
  to the cache TTL. Fix: bump the leaderboard generation on penalty save.

- **Score entry swallows bad input.** An unresolvable seat id is silently skipped
  (partial 3-of-4 save reported as success); a non-numeric MP/TP is coerced to
  `None`, which blocks publishing with no indication of which seat. Fix: 409 on
  unresolved id; reject non-numeric instead of nulling.

- **TP-recompute guard checks `from > 4` but not `from < 0`.** A negative seat
  index mis-distributes points. The guard bounds the winner both sides but only
  clamps `from` on the high end (`admin_scores_per_hand.html`,
  `modal_detailed_scores.html`). Fix: guard `from < 0 || from > 4`.

- **Team grouping is case-sensitive (cosmetic).** `"Dragons"` ≠ `"dragons"` —
  mitigated, since a case-split team fails the "team size must be 4" import check
  rather than silently splitting. Fix opportunistically. (The former `last_name()`
  mononym / multi-word-surname oddity was resolved by the first/last-name refactor —
  names are now stored raw from the import's two columns.)

### Scan / OCR (only if camera-scan is used live)

- **`_write_hand` does a blind version bump** with no `version=` predicate (its
  docstring claiming a 409 is wrong), and the gate+write aren't atomic → scorer
  edits between gate and write are clobbered.
- **OCR values written verbatim** with no range/balance check (seat ∈ 1..4/null,
  `win_by != win_from`, winner ⇒ points ≥ 8, four-player balance). A garbled digit
  silently mis-attributes or drops a win.

## Accepted

Risks weighed and kept, with the reason — so they aren't re-opened as oversights.

- **A navigate-away flush can be rejected without telling the scorer.** The score
  grid's debounced save is flushed with `sendBeacon` on `beforeunload` and
  `pagehide` (both events, because mobile browsers often skip `beforeunload`) —
  but a beacon is fire-and-forget by nature: at `pagehide` there is no page left
  to show a dialog on, and `sendBeacon` reports only whether the browser queued
  the request. So a flush that lands on a round published in the meantime is
  rejected (correctly — the round is locked) and the scorer isn't told at that
  moment. Accepted because the failure is visible on next load, the scorer holds
  the paper sheet, and unpublish → correct → republish fixes it in seconds.
  Handling it "properly" would mean a reconcile-on-return mechanism for a
  2-second window — new surface for a rare, self-revealing case.

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
