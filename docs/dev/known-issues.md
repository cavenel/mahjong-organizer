# Known issues (developer notes)

Open correctness/robustness issues carried over from internal pre-release
auditing. Kept for maintainers; not user-facing. Ranking is computed **only**
from seat minipoints/tablepoints — hand-level bugs corrupt stats/badges and the
detailed-hand modal, never the championship ranking.

Several of these were deferred in the past only because a schema migration was
too risky before a live event. That constraint is gone during the public
refactor (fresh migration baseline), so the schema-touching ones are good
candidates to fix as their surrounding code is rewritten.

## Open

- **Seat has no optimistic lock (score entry is last-writer-wins).** The
  score-bearing seat model has no `version` field, unlike `Hand`. Two scorers
  submitting the same table row → the later `bulk_update` silently clobbers the
  earlier with no 409. The publish check is also a TOCTOU just outside the
  surrounding transaction. Fix: add a `version` field + version-predicated
  update (mirror the Hand path). *Natural to do alongside the Phase 2 schema
  redesign.* Interim mitigation: one scorer per table.

- **Excel import is destructive before validation.** The import path deletes
  Players/Schedule/Hands/Seats/PublishedRounds up front, then a broad `except`
  wipes everything and sets `nb_rounds=0`. Any malformed sheet (missing/misnamed
  sheet, non-int `nb_rounds`, duplicate/blank `rand_id`, player count not
  divisible by 4) can erase a live tournament instead of being rejected. Fix:
  validate the whole workbook in memory, then do all deletes+creates in one
  transaction. *Natural to do alongside the Phase 2 import-flow rewrite.*
  Interim mitigation: back up first, import into a fresh/staging tenant.

- **Penalty edit on a published round doesn't bust the detailed-modal cache.**
  Saving `penalty` with `update_fields=['penalty']` never bumps
  `leaderboard_gen`, so the detailed modal shows a stale penalty publicly for up
  to the cache TTL. Fix: bump the leaderboard generation on penalty save.

- **beforeunload score flush has no error handling.** Clearing a cell schedules a
  debounced save; navigating away inside that window fires a `fetch(keepalive)`
  with no 409/network handling, so a write landing on a now-published round can
  silently diverge. Fix: flush on `visibilitychange`/`pagehide`, re-validate on
  next focus.

- **Score entry swallows bad input.** An unresolvable seat id is silently skipped
  (partial 3-of-4 save reported as success); a non-numeric MP/TP is coerced to
  `None`, which blocks publishing with no indication of which seat. Fix: 409 on
  unresolved id; reject non-numeric instead of nulling.

- **set_variable builds a query string with no `encodeURIComponent`.** Welcome/
  title text containing `&`/`=`/space is truncated or corrupted. Fix: encode each
  pair, or POST a form body.

- **TP-recompute guard checks `from > 4` but not `from < 0`.** A negative seat
  index mis-distributes points. Fix: guard `from < 0 || from > 4`.

- **`cross_positions` print crashes on non-dense `rand_id`.** Indexes
  `cross[...][rand_id - 1]` assuming `rand_id ∈ 1..n`; draw numbers like 101–116
  → IndexError → 500 on that print page. Fix: map `rand_id` → dense index.

- **Name handling oddities (cosmetic).** `Player.save()` corrupts real names
  containing the substring "Player" and gives mononyms an empty last name; the
  import's first-name disambiguation aborts at the first placeholder; team
  grouping is case-sensitive. Fix opportunistically.

### Scan / OCR (only if camera-scan is used live)

- **Overwrite gate is `pts > 0`-only.** Zero-point manual corrections aren't
  protected; a re-scan overwrites `confidence=1.0` edits with low-confidence OCR.
- **`_write_hand` does a blind version bump** with no `version=` predicate (its
  docstring claiming a 409 is wrong), and the gate+write aren't atomic → scorer
  edits between gate and write are clobbered.
- **OCR values written verbatim** with no range/balance check (seat ∈ 1..4/null,
  `win_by != win_from`, winner ⇒ points ≥ 8, four-player balance). A garbled digit
  silently mis-attributes or drops a win.

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
