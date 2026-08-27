# Screenshots

The images used by [../guide.md](../guide.md). The filenames must stay as they
are, so the `![...](screenshots/<file>)` links keep working.

All of them match the current console. They were recaptured on 2026-08-20 at a
1150x800 viewport and 2x scale. A4 figures are not wide, so a wider capture only
makes the text smaller in the PDFs.

## Recapturing

[`scripts/screenshots/`](../../../scripts/screenshots/) regenerates the whole
set. It runs the app on the standalone profile, with no Docker, Postgres or
Redis. It seeds the `test` tenant from the click-through fixtures, signs in as
each role, and reshoots every file here. Its README has the recipe.

To capture one by hand, work on the **test tenant** so nothing touches a real
event. Sign in as an admin, run **Excel import / export**, then **Fill all rounds
/ scores** and **/ score sheets** so every page holds realistic data. If a shot
needs a specific role, sign in with an account that holds it.
