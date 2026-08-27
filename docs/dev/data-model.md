# Data model

## Entities

- **Tenant** is one tournament instance, identified by subdomain. Every other row
  is scoped to a tenant.
- **TournamentSettings** holds per-tenant configuration: title, rules, number of
  rounds, round timer, logo, the `is_test` rehearsal flag and more. One row per
  tenant. Templates see it as `tournament`.
- **Player** is a human competitor: name, federation id (`EMA_ID`), country,
  team. One row per person, and the only editable record for a competitor. No
  contact details are stored.
- **Seat** is one competitor's place at a table in one round.
- **Hand** is one hand played at a table in a round.
- **ScoreSheet** is the score-entry state for one (round, table).
- **PublishedRound** marks a round's results as published.
- **PublishTarget** is the tenant's SFTP publish destination: host, path,
  encrypted credentials, spectator URL. Excluded from tenant dumps.
- **ScanConfig** is the tenant's score-sheet scanning setup. It holds the
  encrypted OCR API key, the picture of the tenant's blank sheet, and the
  score-column crop box that photos are aligned against. Excluded from tenant
  dumps.
- **Schedule**, **Screen**, **ScreenMode** and **CeremonyState** support display
  and scheduling.
- **Membership** is one user's access to one tenant. See below.

## Access control: User ↔ Tenant (Membership)

Authorization is per-tenant. `Membership` joins Django's `auth.User` to a
`Tenant` and carries the role flags for that tenant:

- `is_tenant_admin` gives full admin over this tenant. It implies every app role
  here.
- `is_scorer`, `is_display_op` and `is_publisher` are the three tier-3 roles.

Every other model is tenant-scoped by a default FK. `Membership` instead defines
the scope, because it names both the user and the tenant. There are three tiers:

- **Platform superuser** is Django `is_superuser`. It works across tenants,
  bypasses Membership entirely (it needs no row), and is the only cross-tenant
  actor. It creates tenants.
- **Tenant admin** is a Membership with `is_tenant_admin`. It manages that
  tenant's users and roles, including co-admins. It cannot reach other tenants
  or platform ops.
- **Tenant role** is `Scorer`, `Display operator` or `Publisher`, scoped to one
  tenant.

Every runtime check is evaluated against the **current subdomain's** tenant
(`current_membership(request)` in `mahj/views/helpers.py`). A user's access on
one tenant says nothing about another. Cross-tenant isolation comes from the
absence of a membership row. Django `is_staff` grants no access at all. The
Django admin site requires `is_superuser` (see `mahj/admin_site.py`). A unique
constraint allows one Membership per `(user, tenant)`. A user may hold several,
for example a federation organiser running multiple events without being a
superuser.

## Seating draw: Player ↔ Seat

The seating chart ("who meets whom") is fixed by the **draw** and comes from the
imported template or the in-app generator. It is keyed by a **draw number** and
does not depend on which person is drawn into it. A `Seat` carries:

- `round_nb, table_nb, wind`, where and when the seat is (wind: 1=East to
  4=North),
- `draw_number`, the draw slot this seat belongs to, which is the structural key,
- `minipoints, tablepoints, penalty`, the score for that round.

The **draw is recorded once**, on `Player.draw_number`. That field is unique per
tenant and null until the person is drawn in. The competitor sitting at a seat is
the `Player` holding that seat's `draw_number`. So:

- Editing a name, country or team is a one-row change on `Player`, shown
  everywhere.
- The **draw** is each competitor's `Player.draw_number`. Import sets it from the
  entry list when the entry list is pre-drawn. Randomize and team-draw set it
  otherwise. Re-drawing only re-assigns `draw_number`s. The seating chart is
  never touched, and a unique constraint in the database guarantees one
  competitor per number.
- A `Player` with `draw_number` null is on the player list but not yet in the draw.
- A seat whose `draw_number` no competitor holds is shown as "Player
  #`draw_number`". There are no placeholder people in the player list.

## Hands: draws and self-draws

Winner and discarder are stored as **seat winds** (1 to 4). `win_by` carries the
outcome and `win_from` the discarder.

| Situation      | `win_by`      | `win_from`    |
|----------------|---------------|---------------|
| discard win    | winner's wind | dealer's wind |
| self-draw      | winner's wind | **NULL**      |
| draw (no win)  | **0**         | NULL          |
| unplayed slot  | **NULL**      | NULL          |

`points` is the hand value. A **draw** (`win_by` 0) is a played hand nobody won.
A **NULL** `win_by` is an unplayed placeholder row, and those rows only exist on
a sheet that hasn't been validated. On entry the scorer types `0` in the "Win"
column for a draw. A mid-sheet blank (a NULL row before a later result) is
coerced to a draw on validation, and trailing NULL rows are pruned. So a
**validated** sheet has no NULL rows, and **hands played at a table = its `Hand`
row count**.

## Score-entry and publish state

- **ScoreSheet** exists once a sheet is opened for a (round, table). `validated`
  marks it human-checked. Table completion and validation are recorded on this
  row. There is no sentinel hand.
- **PublishedRound** marks a round published. `withheld = True` is the
  end-of-tournament case: the final round is published to prepare the ceremony
  but held back from the public until the podium reveal.

## Ranking

Standings rank only on `Seat.minipoints` and `tablepoints`. MCR ranks on table
points, with minipoints breaking ties. Other rules rank on minipoints. `penalty`
is a sheet-balance field already folded into `minipoints`, so it is **not**
re-added when ranking. `Hand` rows feed stats, badges and the hand-detail modal,
never the ranking.

## Hand arithmetic is client-side and has no automated tests

The score sheet's JavaScript turns a hand (value, winner, discarder) into
per-seat points. The server stores whatever `mp` / `tp` the sheet sends
(`views/score_entry.py`), which keeps the server tolerant of rule variants. The
Python test suite covers *ranking, ties, badges and stats* over stored points. It
never computes hand points. There is no JS test harness, so a change to the
sheet's arithmetic is verified by hand in the browser.

## Dumps and wipes both work from TENANT_MODELS

`mahj/tenant_dump.py` names the per-tenant model set once, in `TENANT_MODELS`.
The dump/restore pair and the reset page's wipe (`wipe_tenant`) both work from
it. Add a tenant-scoped model and it belongs in that tuple. Otherwise it
silently survives a wipe and vanishes from every backup.

A dump is gzipped JSON. Each row carries its concrete fields minus `id` and
`tenant_id`, which are reassigned on restore. That works only because **no model
has a foreign key to another**. Seating references players by `draw_number`
*value* rather than by FK (see above), so there is nothing to remap. Restore is
`bulk_create` in `TENANT_MODELS` order, inside one transaction, after a wipe in
the same transaction. `Schedule` order is preserved because fresh ids ascend in
dump order, and its ordering is semantic.

Three things stay **outside** a dump: `PublishTarget` (deployment config, whose
secrets are Fernet ciphertext under one install's `SECRET_KEY`), `ScanConfig`
(same reasoning, for the API key it holds) and `Membership`/`User` (global
accounts). So a dump carries no secrets and can be restored into any tenant on
any install at the same migration. A dump stamps and checks that migration.

`ScanConfig` is the awkward one. Its *other* half, the sheet image and crop box,
is genuinely tournament data and would restore usefully onto a fresh install. It
stays out because it shares a row with the ciphertext, and splitting the model to
dump half of it costs more than it buys. If that ever changes, the template moves
into a model of its own rather than into the dump list.
