# Mahj.OVH, Mahjong Organizer Virtual Hub

One app to run a mahjong tournament. It handles the player list, the seating,
the scores, the screens in the room, the results for spectators, and the
official EMA report. It works with MCR and Riichi rules.

You can run it on a laptop for one tournament, or on a server for many
tournaments. The app is the same in both cases.

You do not have to host it yourself. An instance is online at
[mahj.ovh](https://mahj.ovh). Open an issue on this repository to ask for a
subdomain for your event, like `mytournament.mahj.ovh`.

![Standings on the screen in the room](docs/screenshots/standings-screen.png)

## What it does

- Players and seating. Import the players, the teams and the seating plan from a
  spreadsheet. You can also randomize the seating, or run the team draw live on
  screen.
- Scoring. Enter the scores table by table. The app checks them. You can also
  take a photo of a score sheet and let AI read it.
- Screens in the room. A round timer with a countdown and a gong. Standings that
  rotate between views. The schedule, announcements, and a prize-giving ceremony
  that shows the podium from 10th place to 1st.
- Results for spectators. A public score table with tournament statistics. Click
  a player, a team or a table to see the details.
- Publishing. Send the results website to any web host over SFTP.
- Reports. The official EMA report as an Excel file, in one click. Also a
  statistics file per player.
- Roles. One person is the tournament admin. Volunteers get only what they need:
  Scorer, Display operator or Publisher. Access is given per tournament, so an
  account from one event cannot see another.

## Two ways to run it

|  | **Standalone** | **Docker** |
|--|----------------|------------|
| **Best for** | one tournament, run from a laptop | many tournaments, on a real server |
| **You need** | a laptop | a server, a domain name, TLS |
| **What runs** | one program, one file on disk | 10 containers: web, nginx, Postgres, PgBouncer, 2× Redis, 4× scan worker, plus certbot on demand |
| **Tournaments** | one per install | many, one per subdomain |
| **Spectators see** | the website you publish over SFTP | the server itself, or SFTP |
| **Read score sheets by photo** | no, enter scores by hand | yes |

## Run it on a laptop (standalone)

You only need a laptop. No server, no Docker, no domain name.

1. **Download** the file for your computer from the
   [latest release](../../releases/latest): Windows x64, macOS (Apple Silicon or
   Intel), or Linux x64.
2. **Unpack it and start `mahj-organizer`** (`mahj-organizer.exe` on Windows). A black
   console window opens. Leave it open, because closing it stops the app. Your
   browser opens on `http://127.0.0.1:8000/options`.
3. **Log in as `admin`.** The password is random. You will find it in the console
   window, and in the file `first-login.txt` next to your data. Change it under
   **Setup > Administration > User management**.

Other devices on the same wifi can also use the app. Open **Run > Display on
screens**. It shows the address to type on each tablet or projector machine. This
also means other people on that network reach the login page, so change the
password.

Your tournament is one file on disk. The app does not back it up for you.
Download a backup at every break from **Setup > Administration > Backup &
restore**. Keep the file on the laptop. Do not put it in a synced folder such as
Dropbox or OneDrive. Sync is what usually corrupts these files.

See [docs/hosting/STANDALONE.md](docs/hosting/STANDALONE.md) for the settings,
where your files are, how to restore a backup, and how to build the program
yourself.

## Run it on a server (Docker)

Use this for a permanent installation with several tournaments. Each tournament
gets its own subdomain.

```bash
git clone https://github.com/cavenel/mahjong-organizer.git mahj && cd mahj
cp .env.example .env
# In .env, set DJANGO_SECRET_KEY, BASE_DOMAIN and DB_PASSWORD.
# See docs/hosting/configuration.md.

# Once: the database and TLS-certificate volumes. They are declared external so
# that no `docker compose down -v` can ever delete them.
docker volume create mahj_postgres_data
docker volume create mahj_letsencrypt

docker compose -f docker-compose.yml -f docker-compose.prod.yml up -d --build
# The database is updated automatically when the containers start.

# Create the main admin account:
docker compose exec web python manage.py createsuperuser
# Then go to https://<BASE_DOMAIN>/admin_db/ and add a Tenant (subdomain + name).
```

Now open `https://<tenant>.<BASE_DOMAIN>/admin`. Give the event a name under
**Setup > Tournament settings**. Add the players under **Setup > Edit players**,
or load them with **Setup > Excel import / export**. Then generate a seating under
**Setup > Seating**. The **Setup checklist** shows what is still missing, and the
progress of the tournament.

See [docs/hosting/deployment.md](docs/hosting/deployment.md) for DNS, TLS, the
first start, and daily operation.

## Publish the results

The app can turn the public results page into simple files. It uploads them to a
web host over SFTP. Any cheap web host works. You do not need to open your own
server to the internet.

This is useful with the laptop version. The app stays on your laptop, and
spectators read the results on a normal website. The address is shown as a QR
code on the screens in the room and on the printed player cards.

It uploads again after every round you publish, or when you click **Publish to
web**. It also puts a full backup of the tournament next to the website.

You set this up per tournament under **Setup > Administration > Publish
target**: host, user, folder, and a password or a private key. The app encrypts
them.

## Documentation

**For the tournament crew, on the day**

- [admin-console/guide.md](docs/admin-console/guide.md) is the main guide, with
  one part per role. Start with the *Find your part* table (Scorer, Publisher,
  Display operator, Tournament admin). Features that only exist for MCR are
  marked, so the same guide works for Riichi.
- [MCR_scorer_cheat_sheet.md](docs/admin-console/MCR_scorer_cheat_sheet.md) and
  [MCR_head_scorer_cheat_sheet.md](docs/admin-console/MCR_head_scorer_cheat_sheet.md)
  are two one-page sheets to print and tape to the scoring desk.

**To install and run it**

- [hosting/STANDALONE.md](docs/hosting/STANDALONE.md), the laptop version.
- [hosting/deployment.md](docs/hosting/deployment.md), the Docker server.
- [hosting/configuration.md](docs/hosting/configuration.md), every setting.

**To work on the code**

- [dev/data-model.md](docs/dev/data-model.md), the database, and how a tournament
  is stored.
- [dev/access-control.md](docs/dev/access-control.md), accounts, roles, and the
  rules you cannot guess from the code.
- [dev/known-issues.md](docs/dev/known-issues.md), known limits and things not to
  break.
- [dev/clickthrough-fixtures/](docs/dev/clickthrough-fixtures/), test data to
  click through the whole app.

The crew guide is also inside the app. The Docker build turns every Markdown file
in `docs/admin-console/` into a PDF (`manage.py build_docs_pdf`, served at
`/static/docs/<name>.pdf`). The user menu links to them. Edit the Markdown files,
because the PDFs are built from them.

## Development

- **Django 5.2** (ASGI), with **Channels** for the live screens (WebSockets)
- **PostgreSQL** (through **PgBouncer**) and **Redis** on the server. Plain
  **sqlite** and no Redis in the laptop version.
- **Alpine.js** and **Tailwind CSS** in the browser. Tailwind is rebuilt on every
  Docker build and every release build.
- **Docker Compose** for the server, **PyInstaller** for the laptop program,
  built for each OS by
  [`.github/workflows/release.yml`](.github/workflows/release.yml)

```bash
python -m pytest                                  # settings: apps.settings.test

python -m coverage run --source=mahj,apps \
    --omit='*/migrations/*,*/tests/*' -m pytest -q   # then: coverage report --sort=cover

pip install -r requirements/standalone.txt        # run the laptop version from source
python -m standalone.run
```

The suite takes about 85 seconds and runs on every push
([`ci.yml`](.github/workflows/ci.yml)). `mahj/tests/test_invariants.py` works
differently from the rest. It runs static checks on the source and the
configuration, with no database and no client, for the properties that are about
something being absent.

`manage.py` uses `apps.settings.dev` by default. That needs a local Postgres and
Redis. The standalone settings need neither.

## License

[MIT](LICENSE) © 2018-2026 Christophe Avenel.

This application is not affiliated with the European Mahjong Association.
