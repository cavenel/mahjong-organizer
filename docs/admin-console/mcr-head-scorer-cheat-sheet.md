# MCR head scorer cheat sheet

## A. Publish rounds

On the **Scoring** page each round has a **Publish round N** toggle. The
**Publisher overview** page has one row per round.

1. A round only publishes when **complete** (every seat has MP **and** TP) and
   **in order** (rounds 1…N-1 published first, no gaps).
2. Tick **Publish round N** → that round's scores **lock**, and the public
   leaderboard + all screens update.
3. To fix a published score → **untick** it. This also unpublishes **every later
   round** (no gaps). Re-publish once corrected.
4. ⚠ **Last round:** publishing it keeps the **final standings hidden**. You
   reveal them from the **Ceremony console** (section C).

> **Publisher overview** shows, per round: *Tables scored* · *Sheets in progress*
> · *Sheets validated* · **Published** toggle.

---

## B. Drive the screens

Each projector runs `…/<n>` in a browser. Open **Display on screens**.

- **Add screen** / **Remove screen** (removes the last one), then open each
  screen's `…/<n>` URL on its machine.
- Each card's **Current output** dropdown sets that screen's view **live**:
  `black` · `scores p.1` · `scores p.2` · `scores all` · `scores all, total only`
  · `counter` (timer) · `schedule`.
- **Display modes.** **Save mode** snapshots all screens' views under a name.
  Click a tile to apply it to the whole room at once (✕ to delete).
- **Show preview.** Live thumbnails of every screen, so you can check them
  without walking over.

### Timer
- **Start timer** → a 3-2-1 countdown **+ start gong** fires in sync on every
  `counter` screen.
- While running it shows the time left. The button becomes **Reset timer**, which
  confirms if the timer is still running.

### Display settings *(each field saves live)*
**Screen zoom** · **score lines per screen** · **counter message** · **total time
of a round (seconds)**.

### To print → Scores
Opens a printable full leaderboard (**Close** / **Print** → "Save as PDF").

---

## C. Prize-giving ceremony

Open **Ceremony console**. It takes over **all** screens. The public site stays
in suspense until the very end.

1. **Show intro slide** while people gather.
2. **Teams → Start**, then **Reveal next ▸** ×3 (3rd → 1st). Read the yellow
   **"Next to announce"** line to the announcer *before* each reveal.
3. **Players → Start**, then **Reveal next** up to 1st.
4. *(Optional)* click **Stat highlights** cards to show them big.
5. **Publish to everyone & end** makes the **final results public**. Do this
   **once, at the very end**.
