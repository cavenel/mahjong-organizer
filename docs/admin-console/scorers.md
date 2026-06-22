# Scorer guide

← back to [admin console overview](README.md)

A **scorer** enters and corrects table results during the tournament. Scorers
work on the **Scoring** page, can open a per-table **score sheet** to enter every
hand, and can **scan a paper score sheet** with a phone camera.

**Who is a scorer?** Any user in the `Scorer` group, or any staff user. When a
scorer (non-staff) opens `/admin`, they land directly on the Scoring page.

> Scorers **cannot** publish rounds (that's the [Publisher](publishers.md) role)
> and **cannot** manage screens or the ceremony (the
> [Display operator](display-operators.md) role). Those controls are hidden or
> disabled for scorer accounts.

---

## 1. The Scoring page

Open **Scoring** from the sidebar (or just sign in as a scorer). The page shows
one **tab per round**; click a tab to switch rounds.

> 📸 **Screenshot — Scoring page: round tabs + table grid for the active round.**
> `![Scoring page](screenshots/10-scoring-page.png)`

Each round is a grid with **one row per table**:

| Column | Meaning |
|---|---|
| ● (status pip) | Per-row save/validity indicator (see below) |
| **Table N** | Table number |
| **East / South / West / North** | The four seats: player name + score input(s) |
| **Sum** | Sum of the four Minipoints — must equal **0** |
| *(MCR only)* **Score sheet** | Opens the per-hand entry sheet, plus a ✓ validation badge |

### Entering a table's scores

For each seat you type the player's **Minipoints (MP)** in the large input.

- **MCR:** the smaller **Table Points (TP)** box next to it is **read-only** — the
  console computes it automatically (4 / 2 / 1 / 0 points by rank, splitting ties
  by averaging).
- **Riichi / other rules:** only the MP box is shown; there is no TP.

As soon as all four seats are filled:

- The **Sum** cell turns **green (`0`)** if the four MP add up to zero (a valid
  table), or **red** showing the non-zero total if they don't.
- The row's **status pip** turns amber ("pending, not yet saved") then green once
  the save lands.

Scores **save automatically** ~2 seconds after you stop typing (and immediately if
you navigate away). There is no "Save" button.

> 📸 **Screenshot — a filled table row: green sum, four seats, TP auto-filled.**
> `![Filled row](screenshots/11-filled-row.png)`

#### Status pip colours

| Pip | Meaning |
|---|---|
| ⚪ grey | Row incomplete / not started |
| 🟡 amber | Edited, save pending (debounced) |
| 🟢 green | Saved successfully |
| 🔴 red | Save failed (e.g. network error, or the round is locked) |

### Live collaboration

Multiple scorers can work the same round at once. Edits made by another scorer
appear in your grid within a second (rows you are *actively* editing are not
overwritten for a few seconds, so you won't lose your in-progress typing).

---

## 2. The per-table score sheet (MCR)

For MCR events each table also has a detailed **score sheet** covering the **16
hands** played at that table. Click the **Score sheet** button on a table row to
open it in a modal.

> 📸 **Screenshot — score sheet modal for one table (16 hands).**
> `![Score sheet](screenshots/12-score-sheet.png)`

For each hand you enter three numbers:

| Field | Meaning |
|---|---|
| **Value** | The hand's point value (the winning hand's points, ≥ 8) |
| **Win** | The **seat** that won the hand (1 = East … 4 = North) |
| **From** | The seat that **discarded** the winning tile (or the winner's own seat / `0` for a self-draw) |

As you type, the sheet **computes the resulting score for each player** and the
**running totals**, and shows the derived **per-player total (MP)** and **Table
Points (TP)** at the bottom. These computed totals are compared with the MP/TP you
entered on the main Scoring grid:

- **Green** cell = the score sheet agrees with the entered total. ✅
- **Red** cell = mismatch — re-check either the hands or the table totals. ❗

This is the cross-check that catches data-entry mistakes: a correctly entered
score sheet should produce green totals that match the grid.

### Validating a sheet

The **Valid** checkbox (bottom-left of the sheet) marks the table as **checked and
confirmed**. When ticked, the 16 hand inputs are **locked** (greyed out) so a
validated sheet can't be edited by accident — untick to edit again.

Validation state is reflected by the **✓ badge** on the table row in the Scoring
grid:

| Badge | Meaning |
|---|---|
| grey ✓ | No hand data yet |
| 🟡 amber ✓ | Hands entered, **not yet validated** ("in progress") |
| 🟢 green ✓ | Sheet **validated** |

> Score sheets save per-hand automatically (~1 second after each edit) using an
> optimistic version lock: if two people edit the same hand at once, the second
> save is rejected so no edit is silently lost.

### Confidence tint (from scanning)

Hands filled by the **scan** tool (below) start tinted **pink** in proportion to
how *uncertain* the OCR was about that cell. Editing a tinted cell — or simply
confirming it — clears the tint, so you can quickly eyeball which scanned numbers
need a human check.

> 📸 **Screenshot — a scanned score sheet with low-confidence pink cells.**
> `![Scan confidence](screenshots/13-scan-confidence.png)`

---

## 3. Scanning a paper score sheet 📷

Tables hand in **paper** A4 score sheets. Instead of typing all 16 hands, a scorer
can photograph the sheet and let the OCR fill it in.

### Getting to the scan page

- **Easiest:** open a table's **Score sheet** (MCR) — it shows a **QR code**
  ("Scan to fill on phone"). Scan it with your phone to open the scan page already
  filled in with that round & table.
- **Or** go directly to `https://<tenant>.mahj.ovh/scan` (you must be signed in as
  a scorer on the phone).

> 📸 **Screenshot — QR code on the score-sheet header.**
> `![Scan QR](screenshots/14-scan-qr.png)`

### Using the scan page (on a phone)

> 📸 **Screenshot — the mobile scan page.**
> `![Scan page](screenshots/15-scan-page.png)`

1. Enter (or confirm) the **Round** and **Table**. The page shows a status badge
   for that table:
   - 🟢 **Empty — ready to scan**
   - 🟠 **Pre-filled but not valid — scanning will overwrite**
   - 🔵 **Already validated — locked** (edit it from the score sheet instead)
   - 🟡 **No positions found** (you can still scan)
2. Tap **Take photo** and shoot the **whole sheet**, flat and well-lit.
3. The photo is aligned to the template and read by OCR (this runs on the server;
   the page polls until it's done — usually a few seconds).
4. On success you get a green result card with **Open score sheet** — tap it to
   review the filled-in hands. Scanned data is saved **but left _not_ validated**,
   and low-confidence cells are tinted pink so you can verify them.
5. The Table field clears (Round is kept) so you can shoot the next table.

> Notes for scorers:
> - Scanning **does not auto-validate** — a human still opens the sheet, checks the
>   pink cells, and ticks **Valid**.
> - If a table already has data, the app asks before overwriting.
> - The page handles being offline / a lapsed session gracefully and tells you what
>   to do (reconnect, or reload & sign in again).

---

## 4. What scorers cannot do

- **Publish rounds.** The *Publish round N* checkbox on each round is **disabled**
  for scorers and labelled *"— staff or publisher only"*. See
  [publishers.md](publishers.md).
- **Edit a published round.** Once a round is published its score inputs are
  **locked** (greyed, "unpublish to edit scores"). A publisher must unpublish it
  first. Attempting to save a locked round fails with a red pip.
- **Import players, draw teams, manage screens, run the ceremony, or export the
  EMA report** — these are staff / display-operator tools.

---

## Quick reference

| Task | Where |
|---|---|
| Enter table MP | Scoring page → seat inputs (auto-saves) |
| Enter all 16 hands | Scoring row → **Score sheet** |
| Confirm a table | Score sheet → **Valid** checkbox |
| Read a paper sheet | **Score sheet → QR**, or `/scan` on a phone |
| Check a scanned table | Open its score sheet, review pink cells, tick **Valid** |
| Fix a published round | Ask a **publisher** to unpublish it first |
