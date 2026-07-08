# Admin console — full operator guide (all roles)

**Mahj.OVH** (Mahjong Organizer Virtual Hub) is an all-in-one toolbox for running
a Mahjong tournament: importing players, entering scores, publishing the
leaderboard, driving the projector/TV screens, and running the prize-giving
ceremony.

This guide documents the **admin console** — the back-office web app the
tournament crew uses during an event. Unlike a per-role manual, it is written to
be **read in order**, following the natural timeline of an event: set up access,
prepare the tournament, enter scores, publish rounds, drive the screens, run the
ceremony, and export afterwards.

> **This is the full edition — all roles, both rule sets (MCR & Riichi).** For
> handing to individual crew, shorter editions exist per rule set and role:
> - **MCR:** [MCR_scorers.md](MCR_scorers.md) (scorers) ·
>   [MCR_head_scorer.md](MCR_head_scorer.md) (head scorer: scorer + publisher +
>   display operator).
> - **Riichi:** [Riichi_scorers.md](Riichi_scorers.md) ·
>   [Riichi_head_scorer.md](Riichi_head_scorer.md).
>
> See [README.md](README.md) for a one-line "which file do I hand out?" chart.

> ![Admin dashboard](screenshots/00-welcome-dashboard.png)<br>
> 📸 **Screenshot — admin dashboard (Welcome page):** the landing page after
> logging in as staff.

## How to use this guide

The console has four roles (Scorer, Publisher, Display operator, Staff). Rather
than splitting the guide by role, each section is marked with a **role callout**
telling you who can perform that action:

> 🔑 **Who can do this:** *Publishers and staff.*

If your role isn't listed for a section, you'll usually find that the
corresponding control is **hidden or disabled** for your account — you can read
the section to understand the flow, but a colleague with the right role performs
the action. **Roles combine**, so a staff user (or someone in several groups) can
do everything their roles allow.

---

## 1. Roles at a glance

Roles are **Django auth groups**. A user gets a role by being added to the
matching group, or by being **staff** (which implies all roles).

| Group name (exact) | What they do |
|---|---|
| `Scorer` | Enter & edit scores, fill/validate score sheets, use the scan tool |
| `Publisher` | Publish / unpublish rounds to the public leaderboard |
| `Display_op` | Manage screens, the round timer, display settings, and the ceremony console |
| *(staff flag)* | All of the above **plus** preparation (import, draw, print) and the post-event EMA export |

Roles are **additive and combinable** — e.g. a person who both enters scores and
publishes should be in both `Scorer` and `Publisher`. A pure `Publisher` can open
the Scoring page and toggle publishing but cannot edit score cells (that needs
`Scorer`).

After signing in, each role lands on a **role-appropriate default page**:

- **Staff** → the *Welcome* dashboard.
- **Scorer** → the *Scoring* page.
- **Publisher** → the *Scoring* page (where the publish toggles live).
- **Display operator** → the *Display on screens* page.

---

## 2. Accessing the console

Every tournament lives on its own **subdomain**:

```
https://<tenant>.mahj.ovh/
```

For example `https://myevent.mahj.ovh/` for a real event, or
`https://test.mahj.ovh/` for the test tenant (see [§11](#11-rehearsing-on-the-test-tenant)).

| URL | What it is |
|---|---|
| `https://<tenant>.mahj.ovh/` | The **public desktop** view (live standings, seating, stats). No login. |
| `https://<tenant>.mahj.ovh/admin` | The **admin console** (this guide). Requires login. |
| `https://<tenant>.mahj.ovh/<n>` | A **display screen** (projector output), e.g. `/1`, `/2`. |
| `https://<tenant>.mahj.ovh/admin_db/` | The raw **Django database admin** (staff only — used to create users/roles). |

> ⚠️ Don't confuse `/admin` (the friendly console described here) with
> `/admin_db/` (the low-level Django admin). They are different pages.

### Logging in

Open `https://<tenant>.mahj.ovh/admin`. If you are not signed in you are sent to
the login page; enter your username and password. You then land on your role's
default page (see [§1](#1-roles-at-a-glance)).

> ![Login](screenshots/01-login.png)<br>
> 📸 **Screenshot — login page.**

To **log out**, use the avatar menu in the top-right corner → *Log out* (or visit
`/admin?logout=1`). The avatar menu also shows the signed-in username and — for
**staff** accounts only — a *Database administration* link (`/admin_db/`);
scorers and publishers don't see it.

### Creating users and assigning roles

> 🔑 **Who can do this:** *Staff only.*

1. Open `https://<tenant>.mahj.ovh/admin_db/` (you must be staff).
2. Under **Authentication and Authorization → Users**, add a user and set a
   password.
3. Edit the user → **Groups** → add `Scorer`, `Publisher`, and/or `Display_op`.
   - The group names must match exactly (capital first letter; `Display_op` with
     an underscore).
   - If a group doesn't exist yet, create it under **Groups** first (no specific
     permissions are needed on the group — the app checks group membership by
     name).
4. Leave **Staff status** unchecked for plain role accounts. Only give *Staff
   status* to organizers who need the preparation/print/export tools and database
   access.

> ![Assign role](screenshots/02-assign-role.png)<br>
> 📸 **Screenshot — Django admin: assigning a user to the `Scorer` group.**

---

## 3. Getting around the console

The console is a single shell with a **left sidebar** (sections) and a **top bar**
(page title + avatar menu). On mobile the sidebar collapses behind the ☰ button.

The sidebar only shows the sections **your role can use**:

```
┌─ Welcome                         (staff)
│
│  PREPARATION                     (staff)
│  ├─ Import from template
│  ├─ Randomize players
│  ├─ Team draw (live)
│  └─ To print ▸  (player names, player cards, table positions,
│                  cross positions, cross positions by team, schedule)
│
│  DURING TOURNAMENT
│  ├─ Scoring                      (staff, scorer, publisher)
│  ├─ Display on screens           (staff, display_op)
│  ├─ Ceremony console             (staff, display_op)
│  └─ To print ▸  (scores)         (staff, display_op)
│
│  AFTER TOURNAMENT                (staff)
│  └─ Generate EMA report
└─
```

> ![Sidebar staff](screenshots/03-sidebar-staff.png)<br>
> 📸 **Screenshot — sidebar as seen by a staff user (all sections).**
>
> ![Sidebar scorer](screenshots/04-sidebar-scorer.png)<br>
> 📸 **Screenshot — sidebar as seen by a scorer (Scoring only).**

### The "To print" modal

Print actions open an in-app modal containing an `<iframe>` of the printable page,
with **Close** and **Print** buttons. *Print* sends the iframe to the browser's
print dialog (use "Save as PDF" to export).

> ![Print modal](screenshots/05-print-modal.png)<br>
> 📸 **Screenshot — a print preview modal (e.g. player cards).**

### Tournament rule sets (MCR vs Riichi)

The console adapts to the tournament's rules (set during *Import from template*):

- **MCR** (Mahjong Competition Rules): scoring uses **Table Points (TP)** derived
  from **Minipoints (MP)**, and tables have a **per-hand score sheet** (16 hands).
- **Riichi** and others: only **Minipoints** are entered; there is no TP column
  and no per-hand score sheet button.

Most screenshots in this guide are from an MCR event; the Riichi layout is the
same minus the TP column and the *Score sheet* button.

### Live updates

The console and screens stay in sync over WebSockets — you rarely need to refresh:

- **Scoring pages** sync each other cell-by-cell as scores are typed, and reflect
  publish/validation state changes live.
- **Display screens** react instantly to screen-view changes, timer start/stop,
  display-setting changes, and ceremony steps.
- **The public site** refreshes only when a round is **published/unpublished** (so
  the crowd never sees scores before they're official).

If a screen ever looks stuck, just refresh that browser tab — it rejoins at the
current state.

---

## 4. Before the tournament — preparation

> 🔑 **Who can do this:** *Staff only* (the **Preparation** sidebar section is
> hidden for other roles).

Before scoring can begin, the tournament structure must exist — players, seating
for every round, the schedule, and the tournament options.

1. **Import from template** — download the blank template from the link on that
   page, fill it in, and upload it. This creates the players, the
   seating/positions for every round, the schedule, and the tournament options
   (title, number of rounds, **rules** — MCR or Riichi).
   - ⚠️ Importing **wipes any previous scores** on the tenant.
2. **Randomize players** / **Team draw (live)** — helpers for seeding the draw.
3. **To print** — produce paper outputs for the room: player names, player cards,
   table positions, cross positions, cross positions by team, and the schedule.
   Each opens in the print modal (Close / Print).

---

## 5. During the tournament — entering scores

> 🔑 **Who can do this:** *Scorers and staff.* A **scorer** enters and corrects
> table results, can open a per-table **score sheet** to enter every hand, and can
> **scan a paper score sheet** with a phone camera.
>
> Scorers **cannot** publish rounds (see [§6](#6-during-the-tournament--publishing-rounds))
> and **cannot** manage screens or the ceremony (see
> [§7](#7-during-the-tournament--driving-the-screens)). Those controls are hidden
> or disabled for scorer accounts.

### The Scoring page

Open **Scoring** from the sidebar (or just sign in as a scorer/publisher). The
page shows one **tab per round**; click a tab to switch rounds.

> ![Scoring page](screenshots/10-scoring-page.png)<br>
> 📸 **Screenshot — Scoring page: round tabs + table grid for the active round.**

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

> **⚠ IMPORTANT — a non-zero Sum is expected when a game has penalties.** If one
> or more penalties were applied during a game, the four Minipoints will **not**
> add up to 0: the **Sum** stays red and shows the penalty total (e.g. `-10`,
> `-20`). **This is not an error** — leave it. The MP you enter (with the penalty
> already applied) are the official scores. Record each penalty on the **score
> sheet** (the *Penalties* row) so its after-penalty Total and Table Points match
> what you entered; it is normal for the score sheet's raw hand total to differ
> from the entered MP/TP by exactly the penalties. Penalties recorded on the score
> sheet also show on the public per-table detail view.

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

#### Finding a table

A **Filter by table** box sits between the publish bar and the grid. Type a table
number to show only that table (exact match); clear it to show every table again.
The value is shared across round tabs, so the filter sticks when you switch
rounds.

- Press <kbd>/</kbd> anywhere on the page — even from a score input — to jump
  straight to the filter; it selects its contents so your next keystrokes
  overwrite it.
- **Tab** runs filter → first seat → … → last seat and then loops back to the
  filter (skipping rows hidden by the filter), so you can enter table after table
  from the keyboard alone.

### The per-table score sheet (MCR)

For MCR events each table also has a detailed **score sheet** covering the **16
hands** played at that table. Click the **Score sheet** button on a table row to
open it in a modal.

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

#### Penalties

Just above the **Total** row the sheet has a **Penalties** row — one integer box
per player. Enter a penalty as a whole number, positive or negative (e.g. `-10`).
The **Total** row then shows each player's hand total **plus** their penalty, and
the **Table Points** are ranked on that **after-penalty** total — so the sheet's
totals line up with the (penalised) MP/TP entered on the grid.

Penalties live on the score sheet only: they are persisted on the `Position` row
(`penalty` field) and also surface on the public per-table detail view, but they
are **never** read by the leaderboard/standings code — the player's official
score is the MP/TP entered on the Scoring grid, nothing else.

#### Validating a sheet

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

#### Confidence tint (from scanning)

Hands filled by the **scan** tool (below) start tinted **pink** in proportion to
how *uncertain* the OCR was about that cell. Editing a tinted cell — or simply
confirming it — clears the tint, so you can quickly eyeball which scanned numbers
need a human check.

> ![Scan confidence](screenshots/13-scan-confidence.png)<br>
> 📸 **Screenshot — a scanned score sheet with low-confidence pink cells.**

### Scanning a paper score sheet 📷

Tables hand in **paper** A4 score sheets. Instead of typing all 16 hands, a scorer
can photograph the sheet and let the OCR fill it in.

**Getting to the scan page:**

- **Easiest:** open a table's **Score sheet** (MCR) — it shows a **QR code**
  ("Scan to fill on phone"). Scan it with your phone to open the scan page already
  filled in with that round & table.
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

> Notes for scorers:
> - Scanning **does not auto-validate** — a human still opens the sheet, checks the
>   pink cells, and ticks **Valid**.
> - If a table already has data, the app asks before overwriting.
> - The page handles being offline / a lapsed session gracefully and tells you what
>   to do (reconnect, or reload & sign in again).

### What scorers cannot do

- **Publish rounds.** The *Publish round N* checkbox on each round is **disabled**
  for scorers and labelled *"— staff or publisher only"*. See
  [§6](#6-during-the-tournament--publishing-rounds).
- **Edit a published round.** Once a round is published its score inputs are
  **locked** (greyed, "unpublish to edit scores"). A publisher must unpublish it
  first. Attempting to save a locked round fails with a red pip.
- **Import players, draw teams, manage screens, run the ceremony, or export the
  EMA report** — these are staff / display-operator tools.

---

## 6. During the tournament — publishing rounds

> 🔑 **Who can do this:** *Publishers and staff.* A **publisher** decides **when
> each round becomes official** — i.e. visible on the public website and the
> leaderboard screens. Publishing is deliberately a separate role from scoring:
> scorers keep correcting numbers privately, and only a publisher flips a round to
> "public".
>
> A publisher account that is *not* also a `Scorer` can toggle publishing but
> cannot edit score cells. People who do both should be in both groups (or be
> staff).

Publishers work on the same **Scoring** page as scorers (one tab per round). At
the top of each round's pane is a **publish bar**:

> ![Publish bar](screenshots/30-publish-bar.png)<br>
> 📸 **Screenshot — publish bar on a round (toggle, status, hints).**

- **Publish round N** — the checkbox that publishes/unpublishes the round.
- **Status** — *Published* (green) or *Not published*, on the right.
- Hints appear contextually: *"unpublish to edit scores"* when a round is locked,
  and a special note on the **last round** (see below).

> For **scorer** accounts this toggle is **disabled** and labelled *"— staff or
> publisher only"*. For publishers and staff it is active.

### Publishing a round

Tick **Publish round N**. The console enforces these rules (the same checks run on
the server, so they can't be bypassed):

1. **The round must be complete.** Every seat at every table in the round must have
   both Minipoints and Table Points filled. If any are missing, the toggle stays
   disabled until the round is finished.
2. **Rounds publish in order.** You can't publish round N until rounds 1…N-1 are
   already published — no gaps. (Trying to do so is rejected with an explanatory
   error.)

On success:

- The round's scores become **locked** — score inputs in that round's grid turn
  grey/read-only. Scorers can no longer edit it. (This is the safety property:
  publishing freezes the official numbers.)
- The **public leaderboard updates** and all display screens refresh to show the
  newly official standings.

> ![Published round](screenshots/31-published-round.png)<br>
> 📸 **Screenshot — a published (locked) round: green "Published", grey inputs.**

### Unpublishing a round

To correct a score after publishing, **untick Publish round N**. Because rounds
must stay gap-free, **unpublishing a round also unpublishes every round after it.**

Example: if rounds 1–5 are published and you unpublish round 3, rounds 3, 4 and 5
all become unpublished (and editable again); rounds 1 and 2 stay published.

After unpublishing, the round's inputs unlock and a scorer can fix the numbers;
re-publish when corrected.

### The last round is special (podium suspense)

Publishing the **final round** does **not** reveal the final standings to the
public. Instead it publishes the round with the result **hidden**, preserving
suspense for the prize-giving.

The publish bar reminds you of this on the last round:

> *"(last round: publishing keeps the final standings hidden — run the reveal from
> the Prize-giving console on the Ceremony admin page)"*

So the end-of-event flow is:

1. **Publisher:** publish the final round normally (standings stay hidden).
2. **Display operator:** run the **Ceremony console** (see
   [§8](#8-the-prize-giving-ceremony)) to reveal teams/players place by place, and
   finally press **Publish to everyone & end** — *that* is what makes the complete
   final results public.

> ![Last round hint](screenshots/33-last-round-hint.png)<br>
> 📸 **Screenshot — last-round publish bar with the ceremony hint.**

### Live sync

Publish state is shared live across all open Scoring pages: if another publisher
(or you, on another device) publishes a round, every scorer's grid updates its
toggles, status labels, and lock state within a second.

### Publisher overview

Alongside the per-round publish bars, publishers (and staff) get a dedicated
**Publisher overview** page in the sidebar (under *During tournament*). It gives a
bird's-eye view of the whole tournament — **one row per round** — so you can see
how far each round has progressed and publish without hunting through the round
tabs:

> ![Publisher overview](screenshots/32-publisher-overview.png)<br>
> 📸 **Screenshot — Publisher overview: one row per round with progress counts and a Published toggle.**

| Column | Meaning |
|---|---|
| **Tables scored** | Tables whose four seats all have Minipoints entered, shown as `scored / total`. |
| **Sheets in progress** | Tables whose per-table score sheet has been started but not yet validated *(MCR only)*. |
| **Sheets validated** | Tables whose score sheet has been validated *(MCR only)*. |
| **Published** | A checkbox to publish / unpublish the round. |

The **Published** checkbox obeys the same rules as the publish bar: a round can
only be ticked once **all** its tables are scored, and rounds publish **in order**
(round N needs round N-1 first), so an incomplete or out-of-order round's checkbox
stays disabled.

**Unpublishing pops up a big warning** before anything happens — since
unpublishing a round also unpublishes every later round (no gaps), the
confirmation spells out exactly which rounds will be reopened.

Everything on this page **updates live**: as scorers fill rows and validate
sheets the counts move on their own; and publishing/unpublishing here immediately
updates the Scoring page, the public leaderboard and the display screens — exactly
as the publish bar does (and vice-versa).

---

## 7. During the tournament — driving the screens

> 🔑 **Who can do this:** *Display operators and staff.* A **display operator**
> drives everything the audience sees on the room's **projectors / TV screens**:
> which view each screen shows, the **round timer** (with synchronized start gong),
> the display settings (zoom, message, round length), and the **prize-giving
> ceremony** (see [§8](#8-the-prize-giving-ceremony)).
>
> Display operators use these sidebar items: **Display on screens**, **Ceremony
> console**, and **To print → Scores**.

### Screens, explained

Each physical screen in the room runs a browser pointed at a numbered URL:

```
https://<tenant>.mahj.ovh/1     ← screen 1
https://<tenant>.mahj.ovh/2     ← screen 2
...
```

A **screen** record in the app maps each of these URLs to a **view** (what to
show). You add as many screen records as you have physical displays, point each
display's browser at the matching `/<n>` URL, and then change views centrally from
the console — every screen reacts live.

**Available screen views:**

| View | Shows |
|---|---|
| `black` | Blank black screen (off) |
| `scores p. 1` | Leaderboard, page 1 |
| `scores p. 2` | Leaderboard, page 2 |
| `scores all` | Full leaderboard (all players) |
| `scores all, total only` | Compact totals-only leaderboard (3 columns) |
| `counter` | The **round timer** + a condensed standings strip |
| `schedule` | The tournament schedule |

### Display on screens page

Open **Display on screens** from the sidebar.

> ![Display page](screenshots/20-display-page.png)<br>
> 📸 **Screenshot — Display on screens page (screen cards + settings).**

**Adding / removing screens:**

- **Add screen** — appends a new screen (starts on `black`).
- **Remove screen** — removes the **last** screen.

Add one screen per physical display, then open each display's browser at its
`/<n>` URL (shown on the screen card as a clickable link).

**Setting what each screen shows:**

Each screen card has a **Current output** dropdown — pick a view and it changes on
that physical screen **immediately**. The card also shows the screen's public URL
(`https://<tenant>.mahj.ovh/<n>`) so you can open it on the projector machine.

> ![Screen card](screenshots/21-screen-card.png)<br>
> 📸 **Screenshot — a screen card: URL link + "Current output" dropdown.**

**Display modes (saved presets):**

A **display mode** is a saved snapshot of *all* screens' current views, so you can
flip the whole room between layouts in one click.

- **Save screen configuration as a display mode** — type a name and **Save mode**
  to capture the current views of every screen.
- Saved modes appear as amber tiles. **Click a tile** to apply that mode to all
  screens; click the small **✕** on a tile to delete it.

Modes can also be switched from the mobile companion app.

> ![Display modes](screenshots/22-display-modes.png)<br>
> 📸 **Screenshot — saved display-mode tiles.**

**Screen previews:**

Click **Show preview** to load live thumbnails of every screen (scaled-down
1920×1080 iframes), so you can confirm what's actually on each projector without
walking over to it. Click the **enlarge** icon on a thumbnail to view it full-size
in a modal. Previews are only loaded while shown (to save bandwidth).

> ![Previews](screenshots/23-previews.png)<br>
> 📸 **Screenshot — the screen previews row.**

### Round timer control

The **Timer control** panel runs the per-round countdown shown on any screen set
to the `counter` view.

> ![Timer](screenshots/24-timer.png)<br>
> 📸 **Screenshot — Timer control panel (Start / Running / Reset).**

- **Start timer** — starts the round. There's a brief lead-in and a **3-2-1
  countdown with a start gong** that fires in lockstep on **every** screen, so all
  rooms begin together.
- While running, the panel shows the **time remaining** and a *Running* badge; the
  button becomes **Reset timer**.
- When time is up the badge reads *Done*; **Reset timer** clears it back to
  stopped.
- Resetting a running timer asks for confirmation.

How it works (for your peace of mind): the timer is **server-authoritative** — the
server owns the official start instant and every screen renders from it, so screens
can't drift apart or be reset by a stray reload. The round length comes from the
**Total time of a round** display setting (below).

### Display settings

The **Display settings** panel holds presentation values. Each field
saves on change and pushes to the screens live.

> ![Display settings](screenshots/25-display-settings.png)<br>
> 📸 **Screenshot — Display settings (zoom, score lines, message, total time).**

| Setting | Effect |
|---|---|
| **Screen zoom** (1 = 100%) | Scales the on-screen content up/down to fit your displays |
| **Number of score lines per screen** | How many leaderboard rows fit on one screen page (drives pagination of `scores p. 1/2`) |
| **Counter message** | The "welcome"/message text shown under the timer on the `counter` view |
| **Total time of a round (seconds)** | Round length the timer counts down from (e.g. `6900` = 1 h 55 m) |

### To print → Scores

Under *During tournament → To print*, **Scores** opens a printable full
leaderboard in the print modal (Close / Print). Handy for posting paper standings
between rounds.

---

## 8. The prize-giving ceremony

> 🔑 **Who can do this:** *Display operators and staff.*

The **Ceremony console** takes over **all** display screens with a full-screen
reveal sequence for the awards, while the **public website stays in suspense** until
you publish at the very end.

> A friendly one-page run-sheet also lives at
> [`docs/ceremony-brief.md`](../ceremony-brief.md) — hand that to the person
> announcing. The summary below documents the console controls.

Open **Ceremony console** from the sidebar.

> ![Ceremony console](screenshots/26-ceremony-console.png)<br>
> 📸 **Screenshot — Ceremony console (Teams / Players / Stats panels).**

### Layout

**Top-right buttons** (apply to the whole ceremony):

| Button | Effect |
|---|---|
| **Show intro slide** | A "Prize-giving ceremony" holding slide (logo + title) on every screen |
| **End — back to screens** | Stop the ceremony; screens return to their normal views. **Nothing is published.** |
| **Publish to everyone & end** | Reveal the **full final results** on the public site and all screens, then end. **Do this once, at the very end.** |

The rest of the console:

- **On screens now** — a live status line telling you what the audience currently
  sees.
- **Screen previews** — same live previews as the Display page, so you can watch
  the reveal land.
- Three result panels: **Teams (top 3)**, **Players (top N)**, and **Stat
  highlights**.

### Teams and Players panels

Each has the same controls:

- **Start** — put that section on the screens, empty and ready.
- **Reveal next ▸** — reveal the next place (lowest rank first, working up to 1st).
  Press once per place.
- **◂ Back** — undo the last reveal.

A yellow **"Next to announce"** line always previews the upcoming place, name, and
score — read it to the announcer *before* you press *Reveal next*. The full list is
shown below for reference, greying out places not yet revealed.

- **Teams** shows only the **top 3** (the prize-winners), each with team name, all
  member names, and the team total (TP / MP) — no individual player scores.
- **Players** shows the **top N** (typically 16), each with Table Points (large)
  and Minipoints (small).

> Teams panel only appears if the tournament uses teams.

### Stat highlights

Click any **stat card** (e.g. *Highest score in a round*, *Biggest hand*, *Most
wins*) to show it big on the screens. You can show any number of them, in any
order, or skip them entirely.

### Typical run-through

1. **Show intro slide** while people gather.
2. **Teams → Start**, then **Reveal next** ×3 (3rd → 1st). Hand out team prizes.
3. **Players → Start**, then **Reveal next** until 1st.
4. Optionally show a few **Stat highlights**.
5. **Publish to everyone & end** — results are now public for everyone.

> The "Publish to everyone & end" step is how the **final standings** become
> public: during play the last round is published with its result hidden for
> suspense, and this reveal is the first time complete results leave the building.

### If a screen looks stuck

Each screen recovers on its own after a moment. If one stays blank or stale,
**refresh that screen's browser tab** — it rejoins at the current slide. The
console itself can be reloaded too; it resumes where you left off.

---

## 9. After the tournament — EMA export

> 🔑 **Who can do this:** *Staff only.*

Under **After tournament → Generate EMA report**, staff produce the export needed
for ranking submission once the event is finished and all rounds are published.

---

## 10. Permissions recap

| Action | Scorer | Publisher | Display op | Staff |
|---|:--:|:--:|:--:|:--:|
| Enter / edit scores & score sheets | ✅ |  |  | ✅ |
| Scan paper score sheets | ✅ |  |  | ✅ |
| Publish / unpublish rounds |  | ✅ |  | ✅ |
| Manage screens, timer, display settings |  |  | ✅ | ✅ |
| Run the ceremony / final "publish to everyone" |  |  | ✅ | ✅ |
| Preparation (import, draw, print) & EMA export |  |  |  | ✅ |

(Roles combine: a staff user, or a user in several groups, has the union of these.)

---

## 11. Rehearsing on the test tenant

The **test tenant** is a throwaway tournament for rehearsing the whole system —
training scorers, checking how the leaderboard/screens look with data, and
practising the prize-giving ceremony — **without touching a real event**. It lives
at:

```
https://test.mahj.ovh/
```

Its subdomain is literally `test`. The only functional difference from a real
tenant is a **fake-data toolbar** on the Scoring page that can fill or clear *every
round at once* with random-but-valid results. Everything else (screens, ceremony,
publishing, printing) behaves exactly like production, so it's a faithful
rehearsal.

### Seed players & schedule first

The fake-data buttons fill **scores** for the players and tables that already
exist — they do **not** create players. So first set up a tournament structure,
exactly like a real event: sign in as **staff** and run **Import from template**
(see [§4](#4-before-the-tournament--preparation)). Importing wipes any previous
scores on the tenant — which is exactly what you want on the test tenant.

> ![Import](screenshots/40-import-template.png)<br>
> 📸 **Screenshot — Import from template page.**

### The fake-data toolbar 🧪

On `test.mahj.ovh`, the **Scoring** page shows an extra dashed amber toolbar at the
top (it is rendered **only** when the subdomain is `test`):

> ![Test toolbar](screenshots/41-test-toolbar.png)<br>
> 📸 **Screenshot — the "🧪 Test data" toolbar above the round tabs.**

| Button | What it does |
|---|---|
| **Fill all rounds — scores** | Fills the four seats of every table in **every round** with random Minipoints that sum to zero, auto-computes Table Points, and saves. If you're a publisher/staff it then **publishes rounds in order** too. |
| **Fill all rounds — score sheets** | Generates a full 16-hand **score sheet** for every table in every round (random hand values/winners/discarders) and marks each as **validated**. |
| **Clear all rounds — scores** | Clears all entered Minipoints/Table Points (unpublishing first if needed). |
| **Clear all rounds — score sheets** | Deletes all per-hand score-sheet data (and validation marks). |

> The note *"some last-round scores are left empty on purpose"* is intentional: the
> tool leaves the **last two tables of the final round blank** so you can exercise
> the *incomplete-round / can't-publish* path and the podium-suspense flow.

**What "scores" vs "score sheets" means:**

- **Scores** = the per-seat **Minipoints/Table Points totals** on the Scoring grid
  (this is what the leaderboard and displays use).
- **Score sheets** = the detailed **16 hands** behind each table (the MCR per-hand
  data). Score sheets are MCR-only.

For a quick leaderboard/ceremony rehearsal you usually only need **Fill all rounds
— scores**. Use **Fill all rounds — score sheets** when you also want realistic
per-hand data and validated ✓ badges.

**Publishing during fill:** If you run **Fill all rounds — scores** while signed in
as a **publisher or staff** account, the tool also publishes every completed round
(in order) right after filling them — so the public leaderboard and screens light
up immediately. If you're a plain scorer it just fills the scores and leaves
publishing to a publisher.

> ![Filled test data](screenshots/42-filled-data.png)<br>
> 📸 **Screenshot — publisher round overview: every round published except the last, which still has 2 tables empty.**

> The same fill/clear helpers exist for the *currently visible round only* from the
> browser console (`random_fill_score()`, `clear_score()`, etc.). The toolbar
> buttons simply call them with "all rounds" turned on.

### Rehearsing each part of the system

Once the test tenant has data you can exercise the full operator workflow:

1. **Leaderboard / public site** — open `https://test.mahj.ovh/` to see standings,
   seating, and stats render with the fake data.
2. **Screens** — as a display operator ([§7](#7-during-the-tournament--driving-the-screens)):
   add a screen, open `https://test.mahj.ovh/1`, and try each view (`scores all`,
   `counter`, `schedule`, …). Practise the **timer** and **display modes**.
3. **Publishing** — as a publisher ([§6](#6-during-the-tournament--publishing-rounds)):
   publish/unpublish rounds, see the lock behaviour and the cascade on unpublish.
4. **Ceremony** — as a display operator: run the **Ceremony console**
   ([§8](#8-the-prize-giving-ceremony)) end to end (Teams → Players → Stats →
   *Publish to everyone & end*). The deliberately incomplete last round lets you
   practise the suspense → reveal flow.
5. **Scanning** — print or open a score sheet, try the **QR → scan** path on a
   phone (requires the OCR service to be configured on the host).

**Reset between rehearsals:**

- **Clear all rounds — scores** (and **— score sheets**) wipes results but keeps
  the players/seating.
- Re-running **Import from template** resets the whole tenant (players, seating,
  schedule, options) and clears all scores and published rounds.

### Notes & caveats

- The toolbar is gated purely on the **subdomain being `test`** — it never appears
  on a real tenant, so there's no risk of accidentally generating junk on a live
  event.
- Random fills produce **valid** tables (each table's Minipoints sum to zero) so the
  leaderboard maths and the score-sheet cross-checks behave like the real thing.
- You still need the appropriate **role** to do each action on the test tenant
  (e.g. publishing needs `Publisher`/staff) — the test tenant is a good place to
  confirm a new user's roles are set correctly before the real event.

---

## 12. Quick reference

| Task | Where | Role |
|---|---|---|
| Create users / assign roles | `/admin_db/` → Users → Groups | Staff |
| Set up the tournament | **Import from template** | Staff |
| Print player cards / positions / schedule | Preparation → **To print** | Staff |
| Enter table MP | Scoring page → seat inputs (auto-saves) | Scorer |
| Enter all 16 hands | Scoring row → **Score sheet** | Scorer |
| Confirm a table | Score sheet → **Valid** checkbox | Scorer |
| Read a paper sheet | **Score sheet → QR**, or `/scan` on a phone | Scorer |
| Make round N official | Scoring → round N → tick **Publish round N** (round complete; 1…N-1 published) | Publisher |
| Reopen a round for edits | Untick **Publish round N** (also reopens N+1…) | Publisher |
| Final round | Publish it (stays hidden) → finish via Ceremony console | Publisher + Display op |
| Add a projector | Display page → **Add screen**, open `/<n>` on that machine | Display op |
| Change what a screen shows | Screen card → **Current output** dropdown | Display op |
| Flip the whole room at once | Save a **display mode**, click its tile | Display op |
| Check screens remotely | **Show preview** | Display op |
| Start the round (with gong) | Timer control → **Start timer** | Display op |
| Set round length | Display settings → **Total time of a round** | Display op |
| Run the awards | **Ceremony console** | Display op |
| Make final results public | Ceremony console → **Publish to everyone & end** | Display op |
| Generate the ranking export | **Generate EMA report** | Staff |
| Rehearse with fake data | `test.mahj.ovh` → Scoring → **Fill all rounds** | (any, see §11) |
