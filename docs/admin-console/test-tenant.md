# Test tenant (`test.mahj.ovh`)

← back to [admin console overview](README.md)

The **test tenant** is a throwaway tournament for rehearsing the whole system —
training scorers, checking how the leaderboard/screens look with data, and
practising the prize-giving ceremony — **without touching a real event**.

It lives at:

```
https://test.mahj.ovh/
```

Its subdomain is literally `test`. The only functional difference from a real
tenant is a **fake-data toolbar** on the Scoring page that can fill or clear *every
round at once* with random-but-valid results. Everything else (screens, ceremony,
publishing, printing) behaves exactly like production, so it's a faithful
rehearsal.

---

## 1. Seed players & schedule first

The fake-data buttons fill **scores** for the players and tables that already
exist — they do **not** create players. So first set up a tournament structure,
exactly like a real event:

1. Sign in as **staff** at `https://test.mahj.ovh/admin`.
2. Go to **Import from template** and upload a filled-in spreadsheet (download the
   blank template from the link on that page). This creates the players,
   seating/positions for every round, the schedule, and the tournament options
   (title, number of rounds, **rules** — MCR or Riichi).

> See [README → Import](README.md) and the *Preparation* sidebar section. Importing
> wipes any previous scores on the tenant — which is exactly what you want on the
> test tenant.

> 📸 **Screenshot — Import from template page.**
> `![Import](screenshots/40-import-template.png)`

---

## 2. The fake-data toolbar 🧪

On `test.mahj.ovh`, the **Scoring** page shows an extra dashed amber toolbar at the
top (it is rendered **only** when the subdomain is `test`):

> 📸 **Screenshot — the "🧪 Test data" toolbar above the round tabs.**
> `![Test toolbar](screenshots/41-test-toolbar.png)`

| Button | What it does |
|---|---|
| **Fill all rounds — scores** | Fills the four seats of every table in **every round** with random Minipoints that sum to zero, auto-computes Table Points, and saves. If you're a publisher/staff it then **publishes rounds in order** too. |
| **Fill all rounds — score sheets** | Generates a full 16-hand **score sheet** for every table in every round (random hand values/winners/discarders) and marks each as **validated**. |
| **Clear all rounds — scores** | Clears all entered Minipoints/Table Points (unpublishing first if needed). |
| **Clear all rounds — score sheets** | Deletes all per-hand score-sheet data (and validation marks). |

> The note *"some last-round scores are left empty on purpose"* is intentional: the
> tool leaves the **last two tables of the final round blank** so you can exercise
> the *incomplete-round / can't-publish* path and the podium-suspense flow.

### What "scores" vs "score sheets" means

- **Scores** = the per-seat **Minipoints/Table Points totals** on the Scoring grid
  (this is what the leaderboard and displays use).
- **Score sheets** = the detailed **16 hands** behind each table (the MCR per-hand
  data). Score sheets are MCR-only.

For a quick leaderboard/ceremony rehearsal you usually only need **Fill all rounds
— scores**. Use **Fill all rounds — score sheets** when you also want realistic
per-hand data and validated ✓ badges.

### Publishing during fill

If you run **Fill all rounds — scores** while signed in as a **publisher or staff**
account, the tool also publishes every completed round (in order) right after
filling them — so the public leaderboard and screens light up immediately. If
you're a plain scorer it just fills the scores and leaves publishing to a
publisher.

> 📸 **Screenshot — Scoring page after "Fill all rounds — scores" (green rows,
> published rounds).**
> `![Filled test data](screenshots/42-filled-data.png)`

> The same fill/clear helpers exist for the *currently visible round only* from the
> browser console (`random_fill_score()`, `clear_score()`, etc.). The toolbar
> buttons simply call them with "all rounds" turned on.

---

## 3. Rehearsing each part of the system

Once the test tenant has data you can exercise the full operator workflow:

1. **Leaderboard / public site** — open `https://test.mahj.ovh/` to see standings,
   seating, and stats render with the fake data.
2. **Screens** — as a [display operator](display-operators.md): add a screen, open
   `https://test.mahj.ovh/1`, and try each view (`scores all`, `counter`,
   `schedule`, …). Practise the **timer** and **display modes**.
3. **Publishing** — as a [publisher](publishers.md): publish/unpublish rounds, see
   the lock behaviour and the cascade on unpublish.
4. **Ceremony** — as a display operator: run the **Ceremony console** end to end
   (Teams → Players → Stats → *Publish to everyone & end*). The deliberately
   incomplete last round lets you practise the suspense → reveal flow.
5. **Scanning** — print or open a score sheet, try the **QR → scan** path on a
   phone (requires the OCR service to be configured on the host).

### Reset between rehearsals

- **Clear all rounds — scores** (and **— score sheets**) wipes results but keeps
  the players/seating.
- Re-running **Import from template** resets the whole tenant (players, seating,
  schedule, options) and clears all scores and published rounds.

---

## 4. Notes & caveats

- The toolbar is gated purely on the **subdomain being `test`** — it never appears
  on a real tenant, so there's no risk of accidentally generating junk on a live
  event.
- Random fills produce **valid** tables (each table's Minipoints sum to zero) so the
  leaderboard maths and the score-sheet cross-checks behave like the real thing.
- You still need the appropriate **role** to do each action on the test tenant
  (e.g. publishing needs `Publisher`/staff) — the test tenant is a good place to
  confirm a new user's roles are set correctly before the real event.

---

## Quick reference

| Goal | Steps |
|---|---|
| Stand up a test event | Sign in as staff → **Import from template** |
| Fill the leaderboard fast | Scoring → **Fill all rounds — scores** (as publisher/staff to also publish) |
| Add realistic hand data | Scoring → **Fill all rounds — score sheets** |
| Start over (keep players) | **Clear all rounds — scores** / **— score sheets** |
| Full reset | Re-run **Import from template** |
