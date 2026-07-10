# Data model

The domain is small. The only non-obvious parts are how the seating draw relates
to competitors, and how a hand encodes draws and self-draws.

## Entities

- **Tenant** — one tournament instance (subdomain). Every other row is scoped to
  a tenant.
- **TournamentSettings** — per-tenant configuration (title, rules, number of
  rounds, round timer, logo, …). One row per tenant. Exposed to templates as
  `tournament`.
- **Player** — a human competitor: name, federation id (`EMA_ID`), country,
  email, team. One row per person; the single editable record for a competitor.
- **Seat** — one competitor's place at a table in one round.
- **Hand** — one hand played at a table in a round.
- **ScoreSheet** — score-entry state for one (round, table).
- **PublishedRound** — marks a round's results as published.
- **Schedule / Screen / ScreenMode / CeremonyState** — display/scheduling support.
- **Membership** — one user's access to one tenant (see *Access control* below).

## Access control: User ↔ Tenant (Membership)

Authorization is per-tenant. `Membership` joins Django's `auth.User` to a
`Tenant` and carries the role flags for that tenant:

- `is_tenant_admin` — full admin over this tenant; implies every app role here.
- `is_scorer` / `is_display_op` / `is_publisher` — the three tier-3 roles.

Unlike every other model, `Membership` is **not** tenant-scoped by a default FK —
it *defines* the scope (it names both the user and the tenant). Three tiers:

- **Platform superuser** — Django `is_superuser`. Cross-tenant; bypasses
  Membership entirely (needs no row) and is the only cross-tenant actor. Creates
  tenants and runs the whole-cluster DB restore.
- **Tenant admin** — an `is_tenant_admin` Membership. Manages that tenant's users
  and roles (including co-admins). Can't reach other tenants or platform ops.
- **Tenant role** — `Scorer` / `Display operator` / `Publisher`, scoped to one
  tenant.

Every runtime check is evaluated against the **current subdomain's** tenant
(`current_membership(request)` in `mahj/views/helpers.py`), so a user's access on
one tenant says nothing about another — cross-tenant isolation is just the
membership row's absence. Django `is_staff` is reserved for the Django admin site
and grants no app access. The unique constraint is one Membership per
`(user, tenant)`; a user may hold several (e.g. a federation organiser running
multiple events without being a superuser).

## Seating draw: Player ↔ Seat

The seating chart ("who meets whom") is fixed by the **draw** and comes from the
imported schedule. It is keyed by a **draw number**, independent of which person
is drawn into it. A `Seat` carries:

- `round_nb, table_nb, wind` — where/when the seat is (wind: 1=East … 4=North),
- `draw_number` — the draw slot this seat belongs to (the structural key),
- `minipoints, tablepoints, penalty` — the score for that round.

The **draw is recorded once**, on `Player.draw_number` (unique per tenant, null
until the person is drawn in). The competitor sitting at a seat is the `Player`
holding that seat's `draw_number`. So:

- Editing a name/country/team is a one-row change on `Player`, shown everywhere.
- The **draw** is just each competitor's `Player.draw_number`. Import sets it
  from the entry list when pre-drawn; randomize / team-draw set it otherwise.
  Re-drawing only re-assigns `draw_number`s — the seating chart is never touched
  and the database (a unique constraint) guarantees one competitor per number.
- A `Player` with `draw_number` null is on the roster but not yet in the draw.
- A seat whose `draw_number` no competitor holds is shown as "Player #`draw_number`"
  — there are no placeholder people in the roster.

## Hands: draws and self-draws

Winner and discarder are stored as **seat winds** (1–4). `win_by` carries the
outcome, `win_from` the discarder:

| Situation      | `win_by`      | `win_from`    |
|----------------|---------------|---------------|
| discard win    | winner's wind | dealer's wind |
| self-draw      | winner's wind | **NULL**      |
| draw (no win)  | **0**         | NULL          |
| unplayed slot  | **NULL**      | NULL          |

`points` is the hand value. A **draw** (`win_by` 0) is a played hand nobody won;
a **NULL** `win_by` is an unplayed placeholder row that only exists on a sheet
that hasn't been validated. On entry the scorer types `0` in the "Win" column for
a draw; a mid-sheet blank (a NULL row before a later result) is coerced to a draw
on validation, and trailing NULL rows are pruned. So on a **validated** sheet
there are no NULL rows and **hands played at a table = its `Hand` row count**.

## Score-entry and publish state

- **ScoreSheet** exists once a sheet is opened for a (round, table); `validated`
  marks it human-checked. (Table completion/validation is this row, not a
  sentinel hand.)
- **PublishedRound** marks a round published. `withheld = True` is the special
  end-of-tournament case: the final round is published to prepare the ceremony
  but held back from the public until the podium reveal.

## Ranking (unchanged)

Standings rank only on `Seat.minipoints` / `tablepoints` (MCR ranks on table
points, minipoints break ties; other rules rank on minipoints). `penalty` is a
sheet-balance field already folded into `minipoints` — it is **not** re-added
when ranking. `Hand` rows feed stats, badges and the hand-detail modal, never the
ranking.
