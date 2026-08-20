# Screenshots

The images referenced by [../guide.md](../guide.md), under these exact filenames
so the `![...](screenshots/<file>)` links resolve. All of them are current with
the console (recaptured 2026-08-20, 1150×800 viewport at 2× scale — A4 figures
aren't wide, so wider captures only shrink the text in the PDFs).

## Recapturing

The whole set regenerates automatically: [`scripts/screenshots/`](../../../scripts/screenshots/)
runs the app locally on the standalone profile (no Docker/Postgres/Redis),
seeds the `test` tenant from the click-through fixtures, signs in as each role,
and re-shoots every file here. See its README for the recipe.

To capture something by hand instead, follow the same idea: use the **test
tenant** so nothing touches a real event — sign in as an **admin**, run
**Import from template**, then **Fill all rounds — scores / — score sheets**
so every page has realistic data. Where a shot needs a specific role, sign in
with an account holding that role.
