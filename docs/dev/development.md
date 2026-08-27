# Development setup

## Get the code

Needs Python 3.12 and git.

```bash
git clone https://github.com/cavenel/mahjong-organizer.git mahj && cd mahj
python -m venv .venv && . .venv/bin/activate   # .venv\Scripts\activate on Windows
pip install -r requirements/dev.txt
```

## Settings modules

| Module | Used by |
|--------|---------|
| `apps.settings.dev` | `manage.py` default. Needs a local Postgres and Redis. |
| `apps.settings.test` | the test suite. |
| `apps.settings.standalone` | the laptop build. sqlite, no Redis, no Postgres. |
| `apps.settings.prod` | the Docker server. |

## Tests

```bash
python -m pytest
```

About 1200 tests in roughly two minutes. They run on every push
([`ci.yml`](../../.github/workflows/ci.yml)).

Coverage:

```bash
python -m coverage run --source=mahj,apps \
    --omit='*/migrations/*,*/tests/*' -m pytest -q
coverage report --sort=cover
```

`mahj/tests/test_invariants.py` works differently from the rest. It runs static
checks over the source and the configuration, with no database and no client. It
is where properties about something being *absent* are tested.

## Running the app without Postgres or Redis

The standalone profile runs the whole app on a local sqlite file, which is the
quickest way to click through a change.

```bash
pip install -r requirements/standalone.txt
python -m standalone.run
```

It uses the same data directory and `.env` as the released binary, so it opens a
tournament database that is already on this machine. See
[../hosting/standalone.md](../hosting/standalone.md) for where those live.
