# Publisher guide

← back to [admin console overview](README.md)

A **publisher** decides **when each round becomes official** — i.e. visible on the
public website and the leaderboard screens. Publishing is deliberately a separate
role from scoring: scorers can keep correcting numbers privately, and only a
publisher flips a round to "public".

**Who is a publisher?** Any user in the `Publisher` group, or any staff user. When
a publisher (non-staff) opens `/admin`, they land on the **Scoring** page, where
the publish controls live.

> A publisher account that is *not* also a `Scorer` can toggle publishing but
> cannot edit score cells. People who do both should be in both groups (or be
> staff).

---

## The publish bar

The publisher works on the same **Scoring** page as scorers (one tab per round).
At the top of each round's pane is a **publish bar**:

> 📸 **Screenshot — publish bar on a round (toggle, status, hints).**
> `![Publish bar](screenshots/30-publish-bar.png)`

- **Publish round N** — the checkbox that publishes/unpublishes the round.
- **Status** — *Published* (green) or *Not published*, on the right.
- Hints appear contextually: *"unpublish to edit scores"* when a round is locked,
  and a special note on the **last round** (see below).

For **scorer** accounts this toggle is **disabled** and labelled *"— staff or
publisher only"*. For publishers and staff it is active.

---

## Publishing a round

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
- If a webhook is configured, a `round_published` event is sent.

> 📸 **Screenshot — a published (locked) round: green "Published", grey inputs.**
> `![Published round](screenshots/31-published-round.png)`

---

## Unpublishing a round

To correct a score after publishing, **untick Publish round N**. Because rounds
must stay gap-free, **unpublishing a round also unpublishes every round after it.**

Example: if rounds 1–5 are published and you unpublish round 3, rounds 3, 4 and 5
all become unpublished (and editable again); rounds 1 and 2 stay published.

After unpublishing, the round's inputs unlock and a scorer can fix the numbers;
re-publish when corrected.

> 📸 **Screenshot — confirmation/result after unpublishing round 3 (cascade to 4–5).**
> `![Unpublish cascade](screenshots/32-unpublish-cascade.png)`

---

## The last round is special (podium suspense)

Publishing the **final round** does **not** reveal the final standings to the
public. Instead it publishes the round with the result **hidden**, preserving
suspense for the prize-giving.

The publish bar reminds you of this on the last round:

> *"(last round: publishing keeps the final standings hidden — run the reveal from
> the Prize-giving console on the Ceremony admin page)"*

So the end-of-event flow is:

1. **Publisher:** publish the final round normally (standings stay hidden).
2. **Display operator:** run the **[Ceremony console](display-operators.md#4-prize-giving-ceremony-console)**
   to reveal teams/players place by place, and finally press **Publish to everyone
   & end** — *that* is what makes the complete final results public.

> 📸 **Screenshot — last-round publish bar with the ceremony hint.**
> `![Last round hint](screenshots/33-last-round-hint.png)`

---

## Live sync

Publish state is shared live across all open Scoring pages: if another publisher
(or you, on another device) publishes a round, every scorer's grid updates its
toggles, status labels, and lock state within a second.

---

## Quick reference

| Task | How |
|---|---|
| Make round N official | Scoring → round N → tick **Publish round N** (round must be complete; 1…N-1 already published) |
| Reopen a round for edits | Untick **Publish round N** (also reopens N+1…) |
| Final round | Publish it (stays hidden) → finish via **Ceremony console** |
| Why is the toggle disabled? | Round incomplete, or you're a scorer (not publisher/staff) |

---

## Permissions recap

| Action | Scorer | Publisher | Display op | Staff |
|---|:--:|:--:|:--:|:--:|
| Edit scores / score sheets | ✅ | — | — | ✅ |
| Publish / unpublish rounds | — | ✅ | — | ✅ |
| Final "publish to everyone" (ceremony) | — | — | ✅ | ✅ |

(Roles combine: a staff user, or a user in several groups, has the union of these.)
