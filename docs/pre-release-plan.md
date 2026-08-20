# Pre-release plan

The last work before `refactor` merges to `main`. Closes out
`docs/dev/known-issues.md`: every item there is either fixed here or explicitly
accepted, with the reasoning recorded. Nothing is left in the "open, someday" state.

**Status: complete** (2026-08-20; P1 `74f1616`, P2 `f96154f`, P3 `039dc3c`, P4
`ec09a80` + `8f38cfd`; full suite 702 green). Three places where execution
deviated from the text below, each with the reason in its commit:

- **#5**: `sendBeacon` cannot set the CSRF header and Django reads the token
  only from form bodies, so "the endpoint needs no change" was wrong — the
  flush posts FormData with the JSON body under a `payload` field, and
  `update_seats_bulk` accepts that envelope alongside JSON.
- **#6**: the pre-checks raise `TemplateImportError` rendered on the options
  page, not `FieldError` — the import is a browser form POST, and the
  middleware's JSON 400 would land as raw JSON in the organizer's browser.
- **#4**: verified by an exhaustive equivalence sweep (a Python port of both
  old dialects vs the shared function, all 600 input combinations) rather than
  click-through alone; the sweep also showed the two dialects disagreed on a
  typed 0 in "From" — unreachable on the modal, and the shared function keeps
  the sheet's correct self-draw reading.

The migration squash additionally marked the six data backfills
`elidable=True` first, so the squashed baseline carries no `RunPython`; what
remains post-deploy is in `docs/dev/cleanup-plan.md`.

## How each item was judged

Not by how alarming it reads. By what it does to a tournament:

1. **Can it change the final ranking?** Standings are computed *only* from seat
   minipoints/tablepoints. A hand-level bug corrupts stats, badges and the detailed
   modal — never the result. That is a real severity floor and it applies to most of
   this list.
2. **Would anyone notice?** A wrong number on screen that contradicts the paper sheet
   gets caught by the scorer holding the paper. A wrong number the UI reports as
   *saved* does not.
3. **Can the operator fix it themselves?** Unpublish → correct → republish is a normal
   move that takes seconds. If the answer is yes, the bar for code is much lower.
4. **How likely is the trigger, at a real tournament, with real people?** A race whose
   window is one second between two specific actions is not the same risk as a stale
   page someone leaves open over lunch.

An issue that fails (1) is fixed. An issue that passes (2) and (3) is a candidate for
accepting.

## Design rules for the fixes

These are the last changes before release, so they must not add surface:

- **Reuse the conflict channel that exists.** `update_seats_bulk` already answers
  `409 {status, error, row}` and the grid already reverts the row, repaints it from
  the server's values and explains itself. Every new conflict below goes through that
  same response — no new status codes, no new client branches.
- **One definition per rule.** Where a fix touches logic that exists twice, the fix is
  to make it exist once. No parallel copies left behind "for now".
- **No new exception types, no scattered try/except.** `FieldError` +
  `FieldErrorMiddleware` already turn a rejected field into a 400 naming it.
- **Mirror the convention already in the codebase.** `Hand` has optimistic locking
  with a `version` column. `Seat` getting a *different* mechanism would be worse than
  `Seat` having none.

## Triage

| # | Issue | Ranking? | Visible? | Verdict |
|---|-------|----------|----------|---------|
| 1 | `Seat` has no optimistic lock | **yes** | converges silently | **fix** (P1) |
| 2 | Unresolved seat id silently skipped | **yes** | no — the pip says saved | **fix** (P1) |
| 3 | `scan._write_hand` gate isn't atomic; docstring lies | no | no | **fix** (P3) |
| 4 | TP preview accepts `from < 0` | no | yes, cell already flagged | **fix** (P2) |
| 5 | Score flush lost on navigate-away | maybe | on next load | **fix** (P2) |
| 6 | Import wipes before validating | **yes** | yes, loudly | **partial fix** (P3) |
| 7 | Penalty edit doesn't bust the cache | no | after ≤ 5 min | **won't fix** |
| 8 | OCR values not range/balance checked | no | yes, flagged in red | **won't fix** |
| 9 | Team grouping is case-sensitive | no | yes, import rejects | **won't fix** |

Six fixes. Note two changes from the earlier count: **penalty caching moves to
won't-fix** (it is the documented contract, not a bug — see below), and two items
previously filed as "partly fixed" are promoted, because #2 is the worst *visibility*
failure on the list and #6 the worst *blast radius*.

---

## P1 — Score-entry integrity

The only session here that can change a championship result. Both items are in
`views/score_entry.py:update_seats_bulk`, both answer through the existing 409.

### 1. `Seat` has no optimistic lock

**What actually happens.** Two scorers open the score grid. A saves table 3;
`broadcast_scorer_row` repaints B's row — *unless* B typed in that row in the last
3 seconds, which is exactly when two people are working the same table. B then saves
and overwrites A's whole row. Both screens end up showing B's numbers, so it looks
settled. Nobody learns that a different set was ever entered.

**How bad.** Fails test (1): these minipoints *are* the standings. And the zero-sum
row colouring can't catch it — the row is written as a unit inside one transaction, so
B's row is internally balanced too. This is the one item on the list that can quietly
produce a wrong final ranking.

**Why the existing lock doesn't cover it.** `select_for_update()` is already held, so
two *simultaneous* writes serialize correctly. That's not the failure. The failure is
a **stale read**: B's payload carries what B saw before A saved. Only a version
predicate catches that.

**Fix.** Mirror the `Hand` convention exactly — `version = models.IntegerField(default=0)`
on `Seat`, plus a migration.

- `_row_payload` gains `'version': p.version` per seat, so every place the row travels
  (initial render, row sync, 409 revert) carries it.
- The grid sends each seat's version back in the `seats` payload.
- In `update_seats_bulk`, the seats are *already* loaded under `select_for_update()`
  inside the transaction. So compare in Python — no concurrent writer can slip in
  while the lock is held, which makes a SQL predicate unnecessary:

  ```python
  stale = [s for e, s in pairs if int_param(e, 'version', default=0) != s.version]
  if stale:
      return JsonResponse({'status': 'stale', 'error': 'another scorer changed this row',
                           'row': _row_payload(tenant, round_nb, table_nb)}, status=409)
  ```
- `bulk_update` stays; add `version` to its field list with `s.version += 1`.
- Client: the existing 409 handler keys on `status === "locked"`. Widen it to treat
  `"stale"` the same way — revert to the server's row — with its own message
  ("another scorer changed this table; showing their scores"). Same branch, one extra
  case, no new mechanism.

**Deliberately not extended to `update_seat_penalty`.** Penalty is a single
display-only reconciliation field edited from the detailed modal, not concurrent score
entry. Versioning it would be ceremony for a field where last-writer-wins is the right
answer. Say so in the docstring so the asymmetry reads as a decision.

**Tests** (`test_score_entry.py`): a save carrying a stale version gets 409 + the
server's row and writes nothing; a save carrying the current version succeeds and
increments; the version in `_row_payload` round-trips; penalty saves are unaffected.

### 2. An unresolved seat id is silently skipped

**What actually happens.** `seats_by_id.get(seat_id)` returns `None` and the loop does
`continue`. The seat ids come from the server-rendered page, so an id that no longer
resolves means the page is stale — the roster was re-imported or the seating
regenerated while the sheet sat open. The scorer types four numbers, three save, and
the pip turns green.

**How bad.** Fails (1) *and* (2): a missing minipoint blocks publishing with no
indication of which seat, or worse, three-quarters of a table's scores are stored as
though complete. The UI actively reports success. This is the worst visibility failure
on the list, and the trigger — a page left open across an import — is mundane.

**Fix.** Don't skip. Any unresolved id means the client's view of the tournament is
gone, so answer the whole row through the same channel:

```python
if len(seats_by_id) != len(set(ids)):
    return JsonResponse({'status': 'stale', 'error': 'this page is out of date — reload it',
                         'row': _row_payload(tenant, round_nb, table_nb)}, status=409)
```

Same `status: 'stale'` as #1, because it is the same thing from the scorer's side: what
you were looking at is no longer what's there. The client branch added in #1 handles it
with no further work. Note `_row_payload` may come back empty (the seats are gone) —
the revert then blanks the row, which is correct.

**Tests:** a payload naming a deleted seat 409s and writes none of the others; the
partial-save-reported-as-success path is gone.

---

## P2 — Client-side consistency

Neither item can change a result. Both are places where the same rule is written twice
and the two copies disagree — which is the actual thing worth fixing before release.

### 4. The TP preview accepts `from < 0`

**What actually happens.** Both the editable sheet
(`admin_scores_per_hand.html:351`) and the read-only public modal
(`modal_detailed_scores.html:150`) guard with `from > 4` and not `from < 0`. Type `-1`
in a From cell: the guard passes, the else-branch runs, `p == from` never matches, so
all three non-winners are charged the flat −8 and the discarder isn't charged the
hand. The live preview shows a wrong breakdown.

**How bad.** Barely. The cell is already flagged red by `markSeatValidity`
(`/^[1-4]$/`), and the server rejects it outright — `_seat_cell` raises `FieldError`
for anything outside 0–4, so it can never be stored. On the public modal, `from`
comes from stored data that has already passed that check. This is a wrong number
shown for a second in a cell the UI has already marked invalid.

**Why fix it anyway.** Not for the bug — for the duplication. The MCR per-hand point
distribution (8-point base, self-draw vs discard, tie into the running total) is
currently written out twice in two dialects: one clamps NaN with
`if (isNaN(from)) from = 0`, the other with `selfDraw = isNaN(from) || from === by`.
They happen to agree today. That is exactly the setup S3 and S4 spent their time
undoing elsewhere, and `mcr_table_points.js` already establishes where this kind of
rule lives.

**Fix.** Extract `mahj/static/js/mcr_hand_points.js`:

```js
/* The four per-seat deltas for one played MCR hand … one definition, shared by the
   editable sheet and the read-only detail modal. */
window.mcrHandPointDeltas = function (points, winner, discarder) { … };  // null if unplayable
```

Both call sites become a call plus their own rendering (inputs vs text nodes, which is
all that legitimately differs). The guard is written once, with `winner`/`discarder`
both bounded on both sides. No behaviour change beyond negatives now reading as
unplayable, which is what the flag already claims.

**Verification.** There is no JS test harness in this repo and this is not the change
to add one for. It is verified by the click-through: enter a normal win, a self-draw,
a draw, and a `-1`, on both the sheet and the public modal, and confirm the
breakdowns match what the old code produced. Say that plainly in the commit rather
than implying test coverage.

### 5. The score flush is lost on navigate-away

**What actually happens.** An edit is debounced 2 s. Navigating away inside that
window fires the flush. The two score pages do this *differently*: the per-hand sheet
uses `navigator.sendBeacon` on both `beforeunload` and `pagehide`; the grid uses
`fetch(…, {keepalive: true})` on `beforeunload` only.

**Reframing the documented issue.** `known-issues.md` files this as "no error
handling". That is not fixable: at `pagehide` there is no page left to show a dialog
on and `sendBeacon` returns only whether the browser queued the request. Chasing it
would mean inventing a whole reconcile-on-return mechanism for a 2-second window —
new surface, right before a release, for a rare case.

The *real* defect is next to it and is worth fixing: **`beforeunload` alone is
unreliable.** Mobile browsers and backgrounded tabs frequently skip it, firing only
`pagehide`. So on the grid — the page scorers actually live in, on phones — the edit
isn't lost to a rejected write, it's lost because **nothing was ever sent**. That's
both more likely and worse, and the per-hand sheet already shows the fix.

**How bad.** Fails (1) if the lost cell is a minipoint, but it is visible on next load
and trivially correctable, and the scorer has the paper sheet. Moderate probability,
low impact.

**Fix.** Make the grid match the sheet: `navigator.sendBeacon` with a
`Blob(…, {type: 'application/json'})` — `json_body` reads `request.body`, so the
endpoint needs no change — registered on both `beforeunload` and `pagehide`. Two
pages, one flush idiom.

**Accepted and documented, not fixed:** a flush that lands on a round published in the
meantime is rejected and the scorer isn't told at that moment. They see the server's
value on next load, and unpublish → correct → republish fixes it. Record this in
known-issues under "accepted" with the reason, so it isn't re-opened as an oversight.

---

## P3 — Scan and import guards

### 3. `scan._write_hand`: non-atomic gate, and a docstring that lies

**What actually happens.** `scan_prefill` checks "is this table already filled?", then
loops writing hands. A scorer starting entry between the check and the writes gets
overwritten. Separately, `_write_hand` does
`.filter(tenant, round_nb, table_nb, hand_nb).update(version=F('version') + 1, …)`
with **no** `version=` predicate — so its docstring's claim that a concurrent
`update_hand_points` "gets a 409 instead of being silently clobbered" is false. It
clobbers.

**How bad.** The window is the few hundred milliseconds between gate and writes, and
the scan refuses any table that already has data, so the scorer must begin entry in
that exact instant. Hand data never reaches the standings, so it fails nothing at
level (1). Low probability, low impact.

**Why fix it anyway.** Three lines, and the docstring is actively dangerous: it tells
the next person a protection exists where none does. A wrong comment is worse than a
missing one.

**Fix.** Wrap the `already_filled` check and the write loop in one
`transaction.atomic()` with `select_for_update()` on the table's hands, so the gate and
the writes are one decision. Then correct the docstring to say what the code does: the
bump exists so a *concurrent per-cell editor's* next save fails its own version check —
this path itself does not detect a conflict, it relies on the emptiness gate. Describe
the mechanism, not the aspiration.

**Tests** (`test_scan.py`): a prefill into a table that gained a hand mid-flight
doesn't overwrite it; the version bump still invalidates a stale per-cell save.

### 6. The import wipes before it validates

**What actually happens.** `admin_upload_from_template` deletes players, then parses.
S6 wrapped the whole wipe-and-load in one transaction, so a killed worker or dropped
connection no longer commits a half-import — that part is done. What remains: a
*parse* failure is caught, and the `except` deliberately wipes the tournament to empty
rather than leaving it half-loaded. So uploading the wrong file, or an old-format one,
erases whatever was there.

**How bad.** Fails (1) outright if done mid-tournament. But: the import is a
pre-tournament action, run from a workbook this app exports itself, behind a confirm
dialog that names how many players and whether scores exist. The operator is warned,
in specific terms, immediately before it happens.

**Fix — the cheap half only.** Full in-memory validation (parse everything, then
delete) is the right architecture and the wrong thing to start days before a release:
it restructures the longest function in the codebase with no way to prove equivalence
except by importing every workbook shape by hand.

Instead, hoist the checks that catch the *realistic* mistakes above the first
`delete()`, where they cost nothing:

- required sheets present, by name;
- `nb_rounds` reads as an int;
- player count > 0 and divisible by 4;
- no duplicate or blank draw ids.

Raise `FieldError` for each, so the existing middleware turns it into a 400 naming the
field and no new error plumbing appears. That converts "wrong file erased my
tournament" into "wrong file was rejected", which is the whole realistic failure class.
A workbook that passes these and still fails deeper in the parse keeps today's
wipe-to-empty behaviour, and that stays documented as deliberate.

**Tests** (`test_import.py`): each pre-check rejects with the tournament **untouched**
(assert the players still exist — that's the property that matters); a good workbook
still imports byte-identically.

---

## Won't fix — accepted, with reasons

These go into `known-issues.md` under a new "Accepted" heading, not left under "Open".
An accepted risk with a written reason is a decision; the same risk under "Open" reads
as a lapse.

### 7. A penalty edit doesn't bust the leaderboard cache

**Not a bug — it is the contract.** `views/scoring.py` states it explicitly: score
entry does not invalidate these caches, because it writes via `update()` /
`bulk_update()` which fire no signals, and because scorers see their own edits through
the WebSocket row sync rather than through the cache. A standings read can therefore
lag live entry by up to `SUB_CACHE_TTL` (5 minutes) — **for every score field, not
just penalty.**

So a penalty edit behaving exactly like a minipoints edit is the *correct*, homogeneous
outcome. Making `update_seat_penalty` the one write in the app that busts the
leaderboard would be the inconsistency. Penalty is display-only for ranking anyway
(the entered minipoints is already after-penalty), so the stale value is cosmetic on
one modal, for up to 5 minutes, and self-corrects.

**Action: none in code.** Rewrite the known-issues entry — it currently reads as a
bug report against a documented design.

### 8. OCR values aren't range- or balance-checked

The scan is a **prefill**, not a save. `_parse_hand` is tolerant by design so a
garbled digit leaves the scorer an empty cell rather than failing the sheet, and it
already bounds what it stores: seats via `_seat` (1–4 or null), `hand_nb` to 1–16,
`win_from == win_by` collapsed to a self-draw. What's missing is the MCR 8-point
minimum and the four-player balance — and the sheet already flags a value below 8 in
red (`markValueValidity`), and a scorer must validate the sheet before it counts.

Adding server-side rejection here would make the OCR *worse*: a rejected row is a
blank cell the scorer must notice, where today it's a wrong cell the sheet paints red.
The human check is the design, and it is a better check.

### 9. Team grouping is case-sensitive

`"Dragons"` ≠ `"dragons"`. The import's "team size must be 4" check rejects a
case-split team rather than silently splitting it, so the failure mode is a clear
import error before play, not corrupt standings during it. Cosmetic, guarded, and
touching team identity for it risks more than it fixes.

---

## Ordering

**P1** first — it is the only session that can change a result, and #2 is the only
item where the UI reports success for a write that didn't happen. **P2** and **P3** are
independent and can go in either order. **P4** last: rewrite
`docs/dev/known-issues.md` against what actually shipped — every remaining entry is
either "Accepted" with its reason or an invariant not to "fix", and the file has no
"Open" section left. Then the migration squash from `docs/dev/cleanup-plan.md`, which
must be the final schema change before the merge (P1 adds one migration, so squash
after it, not before).

Full suite green at the end of each session, as with the release fix plan.
