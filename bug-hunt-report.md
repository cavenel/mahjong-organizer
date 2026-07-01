# Tournament app — bug hunt report

Six parallel auditors swept the code by area. This is a **bug** report (wrong
number / wrong order / lost score / missed hand/player / premature reveal /
crash), distinct from `pre-event-audit.md` which is mostly technical/process.
Findings are de-duplicated and marked **Confirmed** (traced) or **Suspected**
(data/runtime-dependent).

**Bounding fact:** ranking comes only from `Position.minipoints/tablepoints`;
`Hand` feeds stats/badges. So the highest-severity *new* findings aren't ranking
errors — they're **ceremony spoilers** and **silent score-loss on save**.

## 0. Top priorities

| # | Sev | Area | One-liner | Status |
|---|---|---|---|---|
| **L1** | 🔴 | Reveal | Public "overall" stat cards leak the final round before the ceremony (found independently by 2 auditors) | ✅ Fixed `65bdc25` |
| **L2** | 🔴 | Reveal | `detailed_scores` modal is fully public & unmasked for any round/table | ✅ Fixed `a524c0a` |
| **S1-IMP** | 🔴 | Import | Excel import deletes everything **before** validation, **no transaction** → malformed sheet wipes the live tournament | ⬜ Open |
| **L3** | 🟠 | Reveal | Player/Team modal placement & win/loss cards fold in the withheld final round | ✅ Fixed `c81262b` |
| **E1** | 🟠 | Sync | Published-round penalty edit never busts the detailed-modal cache → stale penalty shown publicly | ⬜ Open |
| **J1** | 🟠 | JS | Score correction made just before navigating away silently lost on `beforeunload` flush | ⬜ Open |
| **S-W1** | ⬜ WON'T FIX | Sync | Dropped websocket update leaves a board badge stale → dashboard-staleness only; publish re-validates server-side, so no wrong/lost score. See `s-w1-report.md`. | — |

**All three reveal leaks (L1, L2, L3) are now fixed** and share one cutoff helper,
`scoring.public_round_max`. Commit `ed7b2ea` also routed the overall/per-round
winner cards through it, so the standings, both detail modals and the winner cards
now mask identically (and consistently during the `reveal==0` ceremony-pending
window). Everything in §4 (scan/OCR) only matters **if camera-scan is used live**.

---

## 1. Premature reveal / ceremony spoilers (NEW, highest stakes)

Masked surfaces key on **publish state** (`reveal==0` + `round_max==nb_rounds`).
Three public surfaces key on **completion state** and leak during the "final round
scored but not yet published" window.

**L1 — ✅ Fixed (`65bdc25`, follow-up `ed7b2ea`).** `views/scoring.py` (`stat_all_rounds`)
→ `scoring.py` (`overall_winners`) → `round_winners()` ran with default
`check_final=False`, bounded by `_last_complete_round` (ignored publish). The
always-visible cards at `desktop.html` ("Highest Points in One Game", "Highest
Self-Draw Hand", "Highest Winning Hand", "Most Winning…") folded in the held-back
final round — the champion's biggest hand/top game showed publicly *before* the
ceremony revealed those categories. **Fixed:** threaded `check_final` through
`overall_winners`, pass it from `desktop()`, keyed the `stat_all:` cache on
`check_final` + invalidate both variants. `ed7b2ea` then routed `round_winners`'
public branch through `public_round_max` so it shows rounds 1..n-1 during the
`reveal==0` window (was an outright `return []`), matching every other surface.

**L2 — ✅ Fixed (`a524c0a`).** `public_modals.py` `detailed_scores`, route
`apps/urls.py:52` (no decorator), returned raw `Position`/`Hand` for *any* round
with no reveal check. Reachable two ways during suspense: (1) guessable URL
`detailed_scores_<nb_rounds>_<table>`; (2) the player modal linked straight into it.
**Fixed:** non-staff requests past `public_round_max` get a "Results not yet
revealed" placeholder (`modal_not_revealed.html`); cache key now includes
`is_admin`; the in-modal link is gated on `scores_json.scores|length`.

**L3 — ✅ Fixed (`c81262b`).** `scoring.py` `player_extra_stats`/`team_extra_stats`
built placement-rate and win/loss cards with no reveal masking — final round folded
in, while the same modal's `scores_json` correctly hid it. **Fixed:** both functions
take a `max_round` cap; the modals pass `public_round_max(force_all=is_admin)`, so
the cards show the same rounds as the score grid beside them.

**L4 — 🟡 Confirmed (low reach).** `display.py:180` `prepublish` keys only on the
last-round row, not on `round_max==nb_rounds` — projector can show "Waiting for
ceremony" on an incomplete round. **L5 — 🟡 Suspected:** `public_modals.py:35,37`
`details_player` 500s on a stale/foreign id (use `get_object_or_404`).

---

## 2. Score entry, save & sync

**E1 — 🟠 Confirmed.** `score_entry.py:296-318` `update_position_penalty` (publish-lock
removed in `c20f42a`) saves `update_fields=['penalty']` but never bumps
`leaderboard_gen`. The detailed modal caches under `modal_detailed:…:{leaderboard_gen}`
(30s) and renders the penalty → stale/wrong penalty publicly for ≤30s, re-warmed if a
spectator opens it. **Fix:** one line — `_bump_leaderboard_gen(subdomain)` on success.

**E2 — 🟠 Medium likelihood, highest stakes. Confirmed.** `score_entry.py:335-368`;
`Position` has no `version` (`models.py:62`). Three races: (a) two scorers same row →
blind `bulk_update` clobbers, client sync only suppresses inbound 3s; (b) publish check
at `:360` is a TOCTOU outside the `transaction.atomic()` at `:367`; (c) the `beforeunload`
`fetch(keepalive)` flush (`admin_scores_per_table.html:356-361`) has **no 409 handling**
so a write landing on a now-published round silently diverges. **Fix:** version-predicated
`Position` update — *but schema migration is risky this close; mitigate with one scorer
per table.*

**E3 — 🟠 Confirmed.** `score_entry.py:335-347`: an unresolvable seat id is silently
`continue`'d (partial 3-of-4 save, scorer sees success); a non-numeric MP/TP is coerced
to `None` → blocks publishing with no indication which seat. **Fix:** return 409 on
unresolved id; reject non-numeric instead of nulling.

✅ Re-verified correct: Hand optimistic lock, publish/unpublish cascade, `leaderboard_gen`
race-safety + C5 deleter, no duplicate signal firing.

---

## 3. Frontend / JavaScript

- **J1 — 🟠 Confirmed.** `admin_scores_per_table.html:341,481-505`. Clearing one cell of
  a full row schedules a 2s-debounced save; navigating away inside that window only fires
  `flushPending` → `fetch(keepalive)` with no error handling/UI. A 409 or network failure
  is invisible. **Fix:** flush on `visibilitychange`/`pagehide`, validate on next focus.
- **J2 — 🟠 Confirmed.** `admin_display.html:551-562` `set_variable` serialises all inputs
  into a URL query string with **no `encodeURIComponent`** → welcome/title text containing
  `&`/`=`/space is truncated/corrupted. **Fix:** encode each pair or POST a form body.
- **J3 — 🟠 Confirmed.** `modal_detailed_scores.html:178`, `admin_scores_per_hand.html:344`:
  TP-recompute guard checks `from>4` but **not `from<0`** → negative seat mis-distributes
  points. **Fix:** `from<0 || from>4`.
- **J4 — 🟠 Confirmed.** `admin_team_draw.html:648-697`: autosave fires once on the final
  slot; failure shows only a small red caption, no retry; missing CSRF cookie → silent 403.
  **Fix:** visible Retry button + loud warning.
- **J5 — 🟡 Confirmed.** `display_scores.html:132`, `display_schedule.html:44`:
  `onUpdate: location.reload()` with no debounce → reload storm on Pi-class projectors
  during a publish burst.
- **J6 — 🟡 Suspected.** `parseInt` without radix throughout score parsing; one `NaN`
  flags a row red yet still saves 3 seats + a NULL.
- **J7 — 🟡 Confirmed (minor):** reconnect-reload dead-end after a server restart
  (`display_socket.js:162`); unbounded `new Audio()` probe (`display_counter.html:390`);
  iframe click-listener rebind (`desktop.html:642`).

---

## 4. Scan / OCR / queue / websocket  ⚠️ §4 except S-W1 only fires IF camera-scan is used

- **S-W1 — ⬜ WON'T FIX (was 🟠).** `consumers.py:52-56` connect only does `group_add`;
  no state push, so a broadcast dropped while the socket stays up (swallowed Redis blip /
  per-channel drop) leaves a stale board badge until reload. **But impact is
  dashboard-staleness only:** "Scored" is `Position` MP/TP (publish re-validates these
  server-side at `score_entry.py:407-412`, so no wrong/incomplete publish); "Validated/
  in-progress" are `Hand` score-sheet QA signals that don't feed the ranking. No path to a
  wrong published leaderboard or a lost score. Snapshot-on-connect fix recorded in
  `s-w1-report.md` if the board UX is ever revisited.
- **S1 — 🔴 Confirmed (scan).** `scan.py:345-352`: the `try/except (ValueError,KeyError)`
  wraps only `json.loads`; the `int(body['round_nb'/'table_nb'])` that actually raises is
  **outside** it → public 500 on any malformed POST. **Fix:** move the parses inside the try.
- **S2 — 🟠 Confirmed (scan).** Overwrite gate is `pts>0`-only (zero-point corrections
  unprotected); `_write_hand` does a **blind** `version=F+1` with no predicate and a docstring
  that's **backwards** ("gets a 409" — it doesn't). Gate+writes not atomic → scorer edits
  clobbered with no 409.
- **S3 — 🟠 Confirmed (scan).** `scan.py:373-381`: OCR `pts`/`win_by`/`win_from` written
  verbatim, no range/balance check → `win_by=7` silently drops a win. **S4 — 🟡:** prefill
  writes to the *client's* round/table, not the scanned job's. **S5/S6/Q2 — 🟡:** orphaned
  result past 90s deadline; partial write (no transaction) can wedge a table; SIGKILL mid-job
  loses it + leaks the image. **S-H1 — 🟡:** outbound webhook uses a static secret and allows
  `http://`.

✅ Re-verified: queue atomicity/FIFO, Redis-down handling, WS tenant isolation/ping-pong,
webhook payload tie-aware.

---

## 5. Import / admin / print

- **S1-IMPORT — 🔴 Confirmed (worse than B7).** `admin_views.py:200-361` runs with **no
  `transaction.atomic()`** and deletes Players/Schedule/Hands/Positions/PublishedRounds
  *before* validating; the bare `except` then wipes everything + `nb_rounds=0`. Any malformed
  sheet → erased tournament. Triggers: missing/misnamed sheet, non-int `nb_rounds` (`:233`,
  also blank/0 → zero seating, "succeeds"), non-numeric `rand_id` (`:280`), duplicate
  `rand_id` (`:318` → one player seated twice, the other never seated). **Fix:** validate the
  whole workbook in memory, then deletes+creates in one transaction. **Back up + use staging
  until then.**
- **I2 — 🟠 Confirmed.** `nb_tables = nb_players//4` → players not ÷4 silently dropped from
  all seating. **I3 — 🟠:** unresolvable seat cell → `player=None` on a non-null FK →
  IntegrityError → wipe.
- **I4 — 🟡 Confirmed.** `admin_views.py:652-686`: destructive screen/mode actions via **GET**
  reachable by non-staff `can_access_admin` users (no CSRF); `remove_screen` 500s on empty
  list (`.last().delete()`).
- **I5/B8 — 🟡 Confirmed.** `print_views.py`: 6 print/export views **unauthenticated** → leak
  the seating draw; `cross_positions:54-56` also `IndexError`/500 on non-dense `rand_id`
  (e.g. 101–116).
- **I6 — 🟡 Confirmed.** `admin_team_draw.html:631` CSV export has no `"`-escaping.
  **I7 — 🟡:** `randomize`/`team_draw_save` lack uniqueness + atomicity, silently skip
  unresolved assignments. **I8 — 🟡 Confirmed:** team per-round breakdown (`scoring.py:259`)
  keys by enumerate index, so a player's round gap shifts the whole team chart by one round
  (totals stay correct).
- **I9 — 🟢:** `_placement_counts` (`scoring.py:544`) drops a seat (and shrinks the
  denominator) if a table has >4 scored peers. **I10 — 🟢:** name/EMA cosmetics (mononym
  blank surname, "Player" substring, `break` vs `continue`, EMA-member NO false-negative,
  flag residual `Macau`/`Catalonia`).

✅ Re-verified: `user_admin` reauth/staff guards, `table_posters`/`randomize` grid sizing,
scoped logo/counter saves.

---

## 6. Scoring core
No ranking-correctness bug found beyond the existing audit. Independently re-verified
correct: A1 tie keys, B1 placement (tie case; see I9), B2 flags, C4 roll-up seed, all
empty/single/all-equal/all-negative edge guards, B9 penalty display-only, history off-by-one,
completion-row `win_by=0` handling.

---

## 7. Recommended order
1. ~~**L1 + L2** (then L3) — ceremony spoilers on always-public surfaces.~~ ✅ **Done**
   (`65bdc25`, `a524c0a`, `c81262b`; unified on `public_round_max` in `ed7b2ea`).
2. **E1** — one-line cache bump. ⬅ **next**
3. **Import S1-IMP + I2 + I3** — validate-before-delete in a transaction; biggest data-loss
   risk. Until then: back up + staging only.
4. **J1–J4** — small live-operator front-end fixes.
5. ~~**S-W1** — reconnect resync.~~ **WON'T FIX** — dashboard-staleness only; publish
   re-validates server-side, so no wrong/lost score (see `s-w1-report.md`).
6. **If scan is used:** S1, S2, S3.
7. Remainder (I4–I9, J5–J7, E2/E3) as time permits — E2's schema change is risky this close;
   prefer one-scorer-per-table.
