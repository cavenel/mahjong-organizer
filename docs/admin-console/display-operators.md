# Display operator guide

← back to [admin console overview](README.md)

A **display operator** drives everything the audience sees on the room's
**projectors / TV screens**: which view each screen shows, the **round timer**
(with synchronized start gong), the display settings (zoom, message, round
length), and the **prize-giving ceremony**.

**Who is a display operator?** Any user in the `Display_op` group, or any staff
user. When a display operator (non-staff) opens `/admin`, they land directly on
the *Display on screens* page.

Display operators use three sidebar items: **Display on screens**, **Ceremony
console**, and **To print → Scores**.

---

## Screens, explained

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

### Available screen views

| View | Shows |
|---|---|
| `black` | Blank black screen (off) |
| `scores p. 1` | Leaderboard, page 1 |
| `scores p. 2` | Leaderboard, page 2 |
| `scores all` | Full leaderboard (all players) |
| `scores all, total only` | Compact totals-only leaderboard (3 columns) |
| `counter` | The **round timer** + a condensed standings strip |
| `schedule` | The tournament schedule |

---

## 1. Display on screens page

Open **Display on screens** from the sidebar.

> 📸 **Screenshot — Display on screens page (screen cards + settings).**
> `![Display page](screenshots/20-display-page.png)`

### Adding / removing screens

- **Add screen** — appends a new screen (starts on `black`).
- **Remove screen** — removes the **last** screen.

Add one screen per physical display, then open each display's browser at its
`/<n>` URL (shown on the screen card as a clickable link).

### Setting what each screen shows

Each screen card has a **Current output** dropdown — pick a view and it changes on
that physical screen **immediately**. The card also shows the screen's public URL
(`https://<tenant>.mahj.ovh/<n>`) so you can open it on the projector machine.

> 📸 **Screenshot — a screen card: URL link + "Current output" dropdown.**
> `![Screen card](screenshots/21-screen-card.png)`

### Display modes (saved presets)

A **display mode** is a saved snapshot of *all* screens' current views, so you can
flip the whole room between layouts in one click.

- **Save screen configuration as a display mode** — type a name and **Save mode**
  to capture the current views of every screen.
- Saved modes appear as amber tiles. **Click a tile** to apply that mode to all
  screens; click the small **✕** on a tile to delete it.

Modes can also be switched from the mobile companion app.

> 📸 **Screenshot — saved display-mode tiles.**
> `![Display modes](screenshots/22-display-modes.png)`

### Screen previews

Click **Show preview** to load live thumbnails of every screen (scaled-down
1920×1080 iframes), so you can confirm what's actually on each projector without
walking over to it. Click the **enlarge** icon on a thumbnail to view it full-size
in a modal. Previews are only loaded while shown (to save bandwidth).

> 📸 **Screenshot — screen previews row, with one enlarged.**
> `![Previews](screenshots/23-previews.png)`

---

## 2. Round timer control

The **Timer control** panel runs the per-round countdown shown on any screen set
to the `counter` view.

> 📸 **Screenshot — Timer control panel (Start / Running / Reset).**
> `![Timer](screenshots/24-timer.png)`

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

> Only display operators (and staff) can start/stop the timer.

---

## 3. Display settings

The **Display settings** panel holds tenant-wide presentation values. Each field
saves on change and pushes to the screens live.

> 📸 **Screenshot — Display settings (zoom, score lines, message, total time).**
> `![Display settings](screenshots/25-display-settings.png)`

| Setting | Effect |
|---|---|
| **Screen zoom** (1 = 100%) | Scales the on-screen content up/down to fit your displays |
| **Number of score lines per screen** | How many leaderboard rows fit on one screen page (drives pagination of `scores p. 1/2`) |
| **Counter message** | The "welcome"/message text shown under the timer on the `counter` view |
| **Total time of a round (seconds)** | Round length the timer counts down from (e.g. `6900` = 1 h 55 m) |

---

## 4. Prize-giving ceremony console

The **Ceremony console** takes over **all** display screens with a full-screen
reveal sequence for the awards, while the **public website stays in suspense** until
you publish at the very end.

> A friendly one-page run-sheet also lives at
> [`docs/ceremony-brief.md`](../ceremony-brief.md) — hand that to the person
> announcing. The summary below documents the console controls.

Open **Ceremony console** from the sidebar.

> 📸 **Screenshot — Ceremony console (Teams / Players / Stats panels).**
> `![Ceremony console](screenshots/26-ceremony-console.png)`

### Layout

- **Top-right buttons** (apply to the whole ceremony):
  | Button | Effect |
  |---|---|
  | **Blank screens** | A clean holding slide (logo / title) on every screen |
  | **End — back to screens** | Stop the ceremony; screens return to their normal views. **Nothing is published.** |
  | **Publish to everyone & end** | Reveal the **full final results** on the public site and all screens, then end. **Do this once, at the very end.** |
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

1. **Blank screens** while people gather.
2. **Teams → Start**, then **Reveal next** ×3 (3rd → 1st). Hand out team prizes.
3. **Players → Start**, then **Reveal next** until 1st.
4. Optionally show a few **Stat highlights**.
5. **Publish to everyone & end** — results are now public for everyone.

> The "Publish to everyone & end" step is how the **final standings** become
> public: during play the last round is published with its result hidden for
> suspense, and this reveal is the first time complete results leave the building
> (it also pushes the final standings to any configured webhook).

### If a screen looks stuck

Each screen recovers on its own after a moment. If one stays blank or stale,
**refresh that screen's browser tab** — it rejoins at the current slide. The
console itself can be reloaded too; it resumes where you left off.

---

## 5. To print → Scores

Under *During tournament → To print*, **Scores** opens a printable full
leaderboard in the print modal (Close / Print). Handy for posting paper standings
between rounds.

---

## Quick reference

| Task | Where |
|---|---|
| Add a projector | Display page → **Add screen**, open `/<n>` on that machine |
| Change what a screen shows | Screen card → **Current output** dropdown |
| Flip the whole room at once | Save a **display mode**, click its tile |
| Check screens remotely | **Show preview** |
| Start the round (with gong) | Timer control → **Start timer** |
| Set round length | Display settings → **Total time of a round** |
| Run the awards | **Ceremony console** |
| Make final results public | Ceremony console → **Publish to everyone & end** |
