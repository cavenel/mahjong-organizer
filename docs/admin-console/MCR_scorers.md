# Admin console — scorer guide (MCR)

**Mahj.OVH** (Mahjong Organizer Virtual Hub) is an all-in-one toolbox for running
a Mahjong tournament. This short guide covers just the **scorer's** job on an
**MCR** (Mahjong Competition Rules) tournament: entering and correcting table
results, filling and validating per-table **score sheets**, and **scanning** paper
sheets with a phone camera.

> **This is the scorer edition for MCR events.** Publishing rounds, driving the
> screens, and running the ceremony are covered in the
> [head-scorer guide (MCR)](MCR_head_scorer.md); the complete reference is
> [full_admin.md](full_admin.md). For a Riichi tournament, use
> [Riichi_scorers.md](Riichi_scorers.md) instead.

**Who is a scorer?** Any user in the `Scorer` group (a staff organizer sets this
up). When a scorer signs in, they land directly on the **Scoring** page.

---

## 1. Signing in

The tournament lives on its own **subdomain**:

```
https://<tenant>.mahj.ovh/admin
```

For example `https://oemc2026.mahj.ovh/admin`. If you are not signed in you are
sent to the login page; enter your username and password. As a scorer you land
directly on the **Scoring** page.

> ![Login](screenshots/01-login.png)<br>
> 📸 **Screenshot — login page.**

To **log out**, use the avatar menu in the top-right corner → *Log out*.

> Need an account, or your `Scorer` role added? Ask a **staff organizer**.

The sidebar (☰ on mobile) shows only the section you can use — **Scoring**.

> ![Sidebar scorer](screenshots/04-sidebar-scorer.png)<br>
> 📸 **Screenshot — sidebar as seen by a scorer (Scoring only).**

### Live updates

Scoring pages stay in sync over WebSockets — you rarely need to refresh. Edits
made by another scorer appear in your grid within a second, and publish/validation
state changes show live. If a page ever looks stuck, just refresh the tab — it
rejoins at the current state.

---

## 2. The Scoring page

Open **Scoring** (or just sign in). The page shows one **tab per round**; click a
tab to switch rounds.

> ![Scoring page](screenshots/10-scoring-page.png)<br>
> 📸 **Screenshot — Scoring page: round tabs + table grid for the active round.**

Each round is a grid with **one row per table**:

| Column | Meaning |
|---|---|
| ● (status pip) | Per-row save/validity indicator (see below) |
| **Table N** | Table number |
| **East / South / West / North** | The four seats: player name + score input(s) |
| **Sum** | Sum of the four Minipoints — must equal **0** |
| **Score sheet** | Opens the per-hand entry sheet, plus a ✓ validation badge |

### Entering a table's scores

For each seat you type the player's **Minipoints (MP)** in the large input. The
smaller **Table Points (TP)** box next to it is **read-only** — the console
computes it automatically (4 / 2 / 1 / 0 points by rank, splitting ties by
averaging).

As soon as all four seats are filled:

- The **Sum** cell turns **green (`0`)** if the four MP add up to zero (a valid
  table), or **red** showing the non-zero total if they don't.
- The row's **status pip** turns amber ("pending, not yet saved") then green once
  the save lands.

Scores **save automatically** ~2 seconds after you stop typing (and immediately if
you navigate away). There is no "Save" button.

> ![Filled row](screenshots/11-filled-row.png)<br>
> 📸 **Screenshot — a filled table row: green sum, four seats, TP auto-filled.**

#### Status pip colours

| Pip | Meaning |
|---|---|
| ⚪ grey | Row incomplete / not started |
| 🟡 amber | Edited, save pending (debounced) |
| 🟢 green | Saved successfully |
| 🔴 red | Save failed (e.g. network error, or the round is locked) |

#### Live collaboration

Multiple scorers can work the same round at once. Edits made by another scorer
appear in your grid within a second (rows you are *actively* editing are not
overwritten for a few seconds, so you won't lose your in-progress typing).

---

## 3. The per-table score sheet

Each table also has a detailed **score sheet** covering the **16 hands** played at
that table. Click the **Score sheet** button on a table row to open it in a modal.

> ![Score sheet](screenshots/12-score-sheet.png)<br>
> 📸 **Screenshot — score sheet modal for one table (16 hands).**

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

> ![Scan confidence](screenshots/13-scan-confidence.png)<br>
> 📸 **Screenshot — a scanned score sheet with low-confidence pink cells.**

---

## 4. Scanning a paper score sheet 📷

Tables hand in **paper** A4 score sheets. Instead of typing all 16 hands, you can
photograph the sheet and let the OCR fill it in.

**Getting to the scan page:**

- **Easiest:** open a table's **Score sheet** — it shows a **QR code** ("Scan to
  fill on phone"). Scan it with your phone to open the scan page already filled in
  with that round & table.
- **Or** go directly to `https://<tenant>.mahj.ovh/scan` (you must be signed in as
  a scorer on the phone).

> ![Scan QR](screenshots/14-scan-qr.png)<br>
> 📸 **Screenshot — QR code on the score-sheet header.**

**Using the scan page (on a phone):**

> ![Scan page](screenshots/15-scan-page.png)<br>
> 📸 **Screenshot — the mobile scan page.**

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

> Notes:
> - Scanning **does not auto-validate** — you still open the sheet, check the pink
>   cells, and tick **Valid**.
> - If a table already has data, the app asks before overwriting.
> - The page handles being offline / a lapsed session gracefully and tells you what
>   to do (reconnect, or reload & sign in again).

---

## 5. What scorers cannot do

- **Publish rounds.** The *Publish round N* checkbox on each round is **disabled**
  for scorers and labelled *"— staff or publisher only"*. A **publisher** does
  this (see the [head-scorer guide](MCR_head_scorer.md)).
- **Edit a published round.** Once a round is published its score inputs are
  **locked** (greyed, "unpublish to edit scores"). Ask a publisher to unpublish it
  first. Attempting to save a locked round fails with a red pip.
- **Import players, manage screens, run the ceremony, or export reports** — these
  are staff / display-operator tools.

---

## 6. Quick reference

| Task | Where |
|---|---|
| Enter table MP | Scoring page → seat inputs (auto-saves) |
| Enter all 16 hands | Scoring row → **Score sheet** |
| Confirm a table | Score sheet → **Valid** checkbox |
| Read a paper sheet | **Score sheet → QR**, or `/scan` on a phone |
| Check a scanned table | Open its score sheet, review pink cells, tick **Valid** |
| Fix a published round | Ask a **publisher** to unpublish it first |

---

## 7. Practising on the test tenant (optional)

The **test tenant** at `https://test.mahj.ovh/` is a throwaway tournament for
rehearsing without touching a real event. On it, the **Scoring** page shows an
extra dashed amber **fake-data toolbar** (rendered only when the subdomain is
`test`) so you can practise the scoring workflow with realistic data.

> ![Test toolbar](screenshots/41-test-toolbar.png)<br>
> 📸 **Screenshot — the "🧪 Test data" toolbar above the round tabs.**

| Button | What it does (as a scorer) |
|---|---|
| **Fill all rounds — scores** | Fills every table in every round with random Minipoints that sum to zero, auto-computes Table Points, and saves. (Publishing is left to a publisher.) |
| **Fill all rounds — score sheets** | Generates a full 16-hand score sheet for every table and marks each as **validated**. |
| **Clear all rounds — scores** | Clears all entered Minipoints/Table Points. |
| **Clear all rounds — score sheets** | Deletes all per-hand score-sheet data (and validation marks). |

- The tenant must first be set up with players/seating — ask a **staff organizer**
  to run **Import from template** before you rehearse.
- You can also try the **QR → scan** path on a phone here.
- Random fills produce **valid** tables (each table sums to zero), so the
  cross-checks behave like the real thing.

> ![Filled test data](screenshots/42-filled-data.png)<br>
> 📸 **Screenshot — round overview after "Fill all rounds — scores": every round filled except the last, which still has 2 tables empty.**
