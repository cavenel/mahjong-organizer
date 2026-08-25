# Click-through import fixtures

Two workbooks for manually exercising the whole chain — import → draw → scoring →
publish → ceremony → print — on a throwaway tenant.

| File | Rules | Field | Teams | Rounds |
|---|---|---|---|---|
| `click-through-MCR-16p-3r.xlsx` | MCR | 16 players, 4 tables | 4 × 4 | 3 |
| `click-through-Riichi-16p-3r.xlsx` | Riichi | 16 players, 4 tables | 4 × 4 | 3 |

Identical but for the `Rules` cell, so switching between them changes exactly the one
variable the Riichi path turns on. Both carry a full pre-drawn seating chart
(rematch-free, no teammate ever sharing a table) and a schedule of three rounds plus
lunch and the prize-giving.

## Two names are deliberately hostile

Draw 1 is `Bobby </script><script>alert(1)</script>` and draw 2 is
`Aoife "Ace" O'Brien`. They exist so the escaping work is visible without hand-editing
anything: if a name renders as bold text, executes, or breaks a page, that page has a
problem. Every surface that shows a competitor name should show these as literal text.

## Importing wipes the tenant

Import is a full replace by design — it clears players, seating and every score. Use a
test tenant, never a live one. Importing the second file replaces the first.

## Scoring them

On a test tournament (Tournament settings → **This is a test tournament**) the score grid shows a fixtures toolbar. **All rounds** fills
everything (leaving the last round's final two tables blank on purpose, to exercise the
incomplete-round path) and publishes what it can. **This round only** fills the open tab
and publishes nothing, for stepping a tournament forward a round at a time.

## Regenerating

They are plain exports: seed a tournament, then **Administration → Export template**.
The originals were produced that way against a scratch database and re-imported through
the real view to confirm the round trip.
