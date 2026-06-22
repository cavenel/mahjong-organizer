# Admin console — operator guide

**Mahj.OVH** (Mahjong Organizer Virtual Hub) is an all-in-one toolbox for running
a Mahjong tournament: importing players, entering scores, publishing the
leaderboard, driving the projector/TV screens, and running the prize-giving
ceremony.

This guide documents the **admin console** — the back-office web app the
tournament crew uses during an event. It is written per role:

| Role | What they do | Guide |
|---|---|---|
| **Scorer** | Enter table scores, fill/validate score sheets, scan paper sheets | [scorers.md](scorers.md) |
| **Display operator** | Manage projector screens, the round timer, and the prize-giving ceremony | [display-operators.md](display-operators.md) |
| **Publisher** | Publish/unpublish rounds to the public leaderboard | [publishers.md](publishers.md) |
| **Staff (organizer)** | Everything above + preparation (import, draw, print) and post-event export | all of the above |

There is also a dedicated guide for the **test tenant** (`test.mahj.ovh`), used to
rehearse the whole flow with fake data:

- [test-tenant.md](test-tenant.md) — generating fake scores, exercising the
  leaderboard / displays / ceremony before a real event.

> 📸 **Screenshot — admin dashboard (Welcome page):** the landing page after
> logging in as staff.
> `![Admin dashboard](screenshots/00-welcome-dashboard.png)`

---

## Accessing the console

Every tournament lives on its own **subdomain**:

```
https://<tenant>.mahj.ovh/
```

For example `https://oemc2026.mahj.ovh/` for a real event, or
`https://test.mahj.ovh/` for the test tenant.

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
the login page; enter your username and password.

> 📸 **Screenshot — login page.**
> `![Login](screenshots/01-login.png)`

After signing in you land on a **role-appropriate default page**:

- **Staff** → the *Welcome* dashboard.
- **Scorer** → the *Scoring* page.
- **Display operator** → the *Display on screens* page.
- **Publisher** → the *Scoring* page (where the publish toggles live).

To **log out**, use the avatar menu in the top-right corner → *Log out* (or visit
`/admin?logout=1`).

---

## Roles & permissions

Roles are **Django auth groups**. A user gets a role by being added to the
matching group, or by being **staff** (which implies all roles).

| Group name (exact) | Grants |
|---|---|
| `Scorer` | Enter & edit scores, fill/validate score sheets, use the scan tool |
| `Display_op` | Manage screens, timer, display settings, and the ceremony console |
| `Publisher` | Publish / unpublish rounds |
| *(staff flag)* | All of the above **plus** preparation tools, printing, and the EMA export |

Roles are **additive and combinable** — e.g. a person who both enters scores and
publishes should be in both `Scorer` and `Publisher`. A pure `Publisher` can open
the scoring page and toggle publishing but cannot edit score cells (that needs
`Scorer`).

### Creating users and assigning roles (staff)

1. Open `https://<tenant>.mahj.ovh/admin_db/` (you must be staff).
2. Under **Authentication and Authorization → Users**, add a user and set a
   password.
3. Edit the user → **Groups** → add `Scorer`, `Display_op`, and/or `Publisher`.
   - The group names must match exactly (capital first letter; `Display_op` with
     an underscore).
   - If a group doesn't exist yet, create it under **Groups** first (no specific
     permissions are needed on the group — the app checks group membership by
     name).
4. Leave **Staff status** unchecked for plain role accounts. Only give *Staff
   status* to organizers who need the preparation/print/export tools and database
   access.

> 📸 **Screenshot — Django admin: assigning a user to the `Scorer` group.**
> `![Assign role](screenshots/02-assign-role.png)`

---

## Navigation & layout

The console is a single shell with a **left sidebar** (sections) and a **top bar**
(page title + avatar menu). On mobile the sidebar collapses behind the ☰ button.

The sidebar only shows the sections a role can use:

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

The **avatar menu** (top-right) shows the signed-in username, a *Database
administration* link (`/admin_db/`, useful only to staff), and *Log out*.

> 📸 **Screenshot — sidebar as seen by a staff user (all sections).**
> `![Sidebar staff](screenshots/03-sidebar-staff.png)`
>
> 📸 **Screenshot — sidebar as seen by a scorer (Scoring only).**
> `![Sidebar scorer](screenshots/04-sidebar-scorer.png)`

### The "To print" modal

Print actions open an in-app modal containing an `<iframe>` of the printable page,
with **Close** and **Print** buttons. *Print* sends the iframe to the browser's
print dialog (use "Save as PDF" to export).

> 📸 **Screenshot — a print preview modal (e.g. player cards).**
> `![Print modal](screenshots/05-print-modal.png)`

---

## Tournament rule sets (MCR vs Riichi)

The console adapts to the tournament's rules (set during *Import from template*):

- **MCR** (Mahjong Competition Rules): scoring uses **Table Points (TP)** derived
  from **Minipoints (MP)**, and tables have a **per-hand score sheet** (16 hands).
- **Riichi** and others: only **Minipoints** are entered; there is no TP column
  and no per-hand score sheet button.

Most screenshots in this guide are from an MCR event; the Riichi layout is the
same minus the TP column and the *Score sheet* button.

---

## Live updates

The console and screens stay in sync over WebSockets — you rarely need to refresh:

- **Scorer pages** sync each other cell-by-cell as scores are typed, and reflect
  publish/validation state changes live.
- **Display screens** react instantly to screen-view changes, timer start/stop,
  display-setting changes, and ceremony steps.
- **The public site** refreshes only when a round is **published/unpublished** (so
  the crowd never sees scores before they're official).

If a screen ever looks stuck, just refresh that browser tab — it rejoins at the
current state.
