# Click-through import fixtures

Two workbooks for exercising the whole chain by hand on a throwaway tenant:
import, draw, scoring, publish, ceremony and print.

| File | Rules | Field | Teams | Rounds |
|---|---|---|---|---|
| `click-through-MCR-16p-3r.xlsx` | MCR | 16 players, 4 tables | 4 × 4 | 3 |
| `click-through-Riichi-16p-3r.xlsx` | Riichi | 16 players, 4 tables | 4 × 4 | 3 |

The two files differ only in the `Rules` cell, so switching between them changes
the one variable the Riichi path turns on. Both carry a full pre-drawn seating
chart, with no rematch and no teammate ever sharing a table, and a schedule of
three rounds plus lunch and the prize-giving.

## Two of the names are hostile on purpose

Draw 1 is `Bobby </script><script>alert(1)</script>` and draw 2 is
`Aoife "Ace" O'Brien`. They make escaping bugs visible without editing anything
by hand. Every surface that shows a competitor name should show these as plain
text. If a name renders as bold, executes, or breaks the page, that page has a
problem.

## Importing wipes the tenant

Import is a full replace. It clears the players, the seating and every score. Use
a test tenant, never a live one. Importing the second file replaces the first.

## Scoring them

Tick **This is a test tournament** under Tournament settings and the score grid
shows a fixtures toolbar.

**All rounds** fills everything and publishes what it can. It leaves the last
round's final two tables blank, to exercise the incomplete-round path.

**This round only** fills the open tab and publishes nothing. Use it to step a
tournament forward one round at a time.

## Regenerating

These are plain exports. Seed a tournament, then use **Administration → Export
template**. The originals were made that way against a scratch database, then
imported through the real view to confirm the round trip.
