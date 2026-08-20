# Mahj.OVH — Mahjong Organizer Virtual Hub

An all-in-one, self-hostable toolbox for running **Mahjong tournaments** end to
end: import the player list and seating draw, enter and validate scores live, drive
the projector screens (round timer, rotating standings, prize-giving ceremony),
publish results to spectators, and export the official EMA report.

It supports both **MCR** (Mahjong Competition Rules) and **Riichi** scoring, and
is **multi-tenant**: one deployment hosts many tournaments, each on its own
subdomain (`<tenant>.your-domain`).

## Features

- **Player list & draw** — import players, teams and the round-by-round seating
  schedule from a spreadsheet; randomize seating or run a live on-screen team
  draw; correct player metadata inline.
- **Live scoring** — per-table score entry with validation, or scan a
  photographed score sheet with optional AI OCR.
- **Projector displays** — synchronized round timer with a lead-in countdown and
  gong, rotating standings (detailed / totals / teams), schedule, announcements,
  and a scripted prize-giving ceremony that reveals the podium 10→1.
- **Spectator results** — a public leaderboard with per-player / per-team / per-
  table detail modals and tournament statistics; optionally rendered to a static
  site and pushed to any web host over SFTP.
- **Reporting** — one-click EMA tournament report (`.xlsx`) and a downloadable
  per-player statistics workbook.
- **Roles** — staff, plus scoped `Scorer`, `Display_op` and `Publisher` roles so
  volunteers only see what they need.

## Tech stack

- **Django 4.2** (ASGI) with **Channels** for WebSocket-driven live displays
- **PostgreSQL** (via **PgBouncer**) and **Redis** (cache + Channels bus)
- **Alpine.js** + **Tailwind CSS** on the front end (no build step at runtime —
  Tailwind is regenerated on each Docker build)
- **Docker Compose** for the full production stack (web, nginx, db, pgbouncer,
  redis, redis_bus, scan_worker, restore_worker)

## Quick start (Docker)

```bash
git clone <this-repo> mahj && cd mahj
cp .env.example .env
# Edit .env: set DJANGO_SECRET_KEY, BASE_DOMAIN, DB_PASSWORD (see docs/hosting/configuration.md)

docker compose -f docker-compose.yml -f docker-compose.prod.yml up -d --build
# Migrations apply automatically on container start.

# Create the first admin user and a tournament tenant:
docker compose exec web python manage.py createsuperuser
# then, at https://<BASE_DOMAIN>/admin_db/ → add a Tenant (subdomain + name)
```

Then visit `https://<tenant>.<BASE_DOMAIN>/admin`, open **Configuration →
Tournament settings** to name the event, and **Import from template** to load the
player list. The **Dashboard** shows a setup checklist and live tournament progress.

For local development without Docker, `manage.py` defaults to
`apps.settings.dev` (SQLite-free; needs a local Postgres + Redis). Run the test
suite with:

```bash
python -m pytest        # settings module: apps.settings.test
```

## Documentation

`docs/` is organized by audience — see [docs/README.md](docs/README.md) for the map.

| Audience | Where | What it covers |
|----------|-------|----------------|
| Tournament crew | [docs/admin-console/](docs/admin-console/) | The operator guide — one document with a part per role (Scorer, Publisher, Display operator, Admin) — plus printable cheat sheets; also served as PDFs from the app's admin menu |
| Whoever hosts the server | [docs/hosting/](docs/hosting/) | Docker deployment, DNS/TLS, every environment variable, the standalone venue-laptop build |
| | [scripts/](scripts/) | Backup, restore and laptop-failover runbooks, next to the scripts they document |
| Developers | [docs/dev/](docs/dev/) | Data model, access control, accepted risks, click-through fixtures |

## License

[MIT](LICENSE) © 2018-2026 Christophe Avenel.

This application is not affiliated with the European Mahjong Association.
