# Admin console — scorer guide (Riichi)

**Mahj.OVH** (Mahjong Organizer Virtual Hub) is an all-in-one toolbox for running
a Mahjong tournament. This short guide covers just the **scorer's** job on a
**Riichi** tournament: entering and correcting each table's results.

> **This is the scorer edition for Riichi events.** On Riichi only the per-player
> **Minipoints** are entered — there is **no Table Points column** and **no
> per-hand score sheet / scan tool** (those are MCR-only). Publishing rounds,
> driving the screens, and running the ceremony are covered in the
> [head-scorer guide (Riichi)](Riichi_head_scorer.md); the complete reference is
> [full_admin.md](full_admin.md). For an MCR tournament, use
> [MCR_scorers.md](MCR_scorers.md) instead.

**Who is a scorer?** Any user in the `Scorer` group (a staff organizer sets this
up). When a scorer signs in, they land directly on the **Scoring** page.

---

## 1. Signing in

The tournament lives on its own **subdomain**:

```
https://<tenant>.mahj.ovh/admin
```

For example `https://myevent.mahj.ovh/admin`. If you are not signed in you are
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
made by another scorer appear in your grid within a second, and publish-state
changes show live. If a page ever looks stuck, just refresh the tab — it rejoins
at the current state.

---

## 2. The Scoring page

Open **Scoring** (or just sign in). The page shows one **tab per round**; click a
tab to switch rounds.

> ![Scoring page](screenshots/10-scoring-page.png)<br>
> 📸 **Screenshot — Scoring page: round tabs + table grid for the active round.**
>
> *(The screenshot is from an MCR event; on Riichi the layout is the same minus
> the Table Points column and the Score sheet button.)*

Each round is a grid with **one row per table**:

| Column | Meaning |
|---|---|
| ● (status pip) | Per-row save/validity indicator (see below) |
| **Table N** | Table number |
| **East / South / West / North** | The four seats: player name + Minipoints input |
| **Sum** | Sum of the four Minipoints — must equal **0** |

### Entering a table's scores

For each seat you type the player's **Minipoints (MP)** in the input. (Riichi has
no Table Points, so there's just the one box per seat.)

As soon as all four seats are filled:

- The **Sum** cell turns **green (`0`)** if the four MP add up to zero (a valid
  table), or **red** showing the non-zero total if they don't.
- The row's **status pip** turns amber ("pending, not yet saved") then green once
  the save lands.

> **⚠ IMPORTANT — a non-zero Sum is expected when a game has penalties.** If one
> or more penalties were applied during a game, the four Minipoints will **not**
> add up to 0: the **Sum** stays red and shows the penalty total (e.g. `-10`,
> `-20`). **This is not an error** — leave it. The MP you enter (with the penalty
> already applied) are the official scores; just confirm the red Sum matches the
> total of the penalties applied.

Scores **save automatically**. There is no "Save" button.

> ![Filled row](screenshots/11-filled-row.png)<br>
> 📸 **Screenshot — a filled table row: green sum, four seats filled.**

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

#### Finding a table

A **Filter by table** box sits just above the grid. Type a table number to show
only that table (exact match); clear it to show every table again. The value
carries across round tabs, so the filter sticks when you switch rounds.

- Press <kbd>/</kbd> anywhere on the page — even from a score input — to jump
  straight to the filter; it selects its contents so your next keystrokes
  overwrite it.
- **Tab** runs filter → first seat → … → last seat and then loops back to the
  filter, so you can enter table after table without touching the mouse.

---

## 3. What scorers cannot do

- **Publish rounds.** The *Publish round N* checkbox on each round is **disabled**
  for scorers and labelled *"— staff or publisher only"*. A **publisher** does
  this (see the [head-scorer guide](Riichi_head_scorer.md)).
- **Edit a published round.** Once a round is published its score inputs are
  **locked** (greyed, "unpublish to edit scores"). Ask a publisher to unpublish it
  first. Attempting to save a locked round fails with a red pip.
- **Manage screens, run the ceremony, or export reports** — these are staff /
  display-operator tools.

---

## 4. Quick reference

| Task | Where |
|---|---|
| Enter table MP | Scoring page → seat inputs (auto-saves) |
| Check a table is valid | The **Sum** cell reads green **0** |
| Fix a published round | Ask a **publisher** to unpublish it first |

---

## 5. Practising on the test tenant (optional)

The **test tenant** at `https://test.mahj.ovh/` is a throwaway tournament for
rehearsing without touching a real event. On it, the **Scoring** page shows an
extra dashed amber **fake-data toolbar** (rendered only when the subdomain is
`test`) so you can practise the scoring workflow with realistic data.

> ![Test toolbar](screenshots/41-test-toolbar.png)<br>
> 📸 **Screenshot — the "🧪 Test data" toolbar above the round tabs.**

| Button | What it does (as a scorer) |
|---|---|
| **Fill all rounds — scores** | Fills every table in every round with random Minipoints that sum to zero, and saves. (Publishing is left to a publisher.) |
| **Clear all rounds — scores** | Clears all entered Minipoints. |

- The tenant must first be set up with players/seating — ask a **staff organizer**
  to run **Import from template** before you rehearse.
- Random fills produce **valid** tables (each table sums to zero), so the
  leaderboard maths behaves like the real thing.

> ![Filled test data](screenshots/42-filled-data.png)<br>
> 📸 **Screenshot — round overview after "Fill all rounds — scores": every round filled except the last, which still has 2 tables empty.**
