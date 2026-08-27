# Screenshot pipeline

Regenerates every image in `docs/admin-console/screenshots/` by running the app
locally and driving it with a headless browser, one session per role. No
Docker, Postgres or Redis: the server runs on the **standalone** profile
(sqlite + in-process cache/channels) pinned to the `test` tenant, so the
fixtures toolbar renders and nothing can touch a real event.

## One-time setup

```bash
# 1. Browser + driver (into the repo venv; downloads its own Chromium build)
.venv/bin/pip install playwright pillow
.venv/bin/playwright install chromium-headless-shell

# 2. Chromium's system libraries, without root, via pixi (skip if
#    `ldd ~/.cache/ms-playwright/*/chrome-headless-shell*/chrome-headless-shell`
#    reports nothing missing)
cd scripts/screenshots && pixi install && cd -
export LD_LIBRARY_PATH=$PWD/scripts/screenshots/.pixi/envs/default/lib
```

## Fresh static files

WhiteNoise indexes `staticfiles/` **at server startup**, and the standalone
profile serves only collected files — a stale tree means 404'd JS and an
unstyled console. If the Tailwind build is older than the templates, rebuild it
first (same binary/version as the Dockerfile), then collect:

```bash
export DJANGO_SETTINGS_MODULE=shots_settings PYTHONPATH=$PWD/scripts/screenshots \
       LOCAL_TENANT=test MAHJ_DB_PATH=$PWD/scripts/screenshots/.local/shots.sqlite3
.venv/bin/python manage.py collectstatic --noinput
```

## Run

```bash
# Terminal 1 — the app (same env as above)
.venv/bin/python -m uvicorn apps.asgi:application --host 127.0.0.1 --port 8123

# Terminal 2 — bootstrap once, then capture everything
.venv/bin/python scripts/screenshots/shots.py
```

`shots.py` runs these stages (each can be named alone to re-run it):

| Stage | What it does |
|---|---|
| `bootstrap` | Migrates the throwaway sqlite DB, creates the `test` tenant and one user per role (`anna.admin`, `sam.scorer`, `pia.publisher`, `dana.display`) |
| `seed` | Imports `docs/dev/clickthrough-fixtures/click-through-MCR-16p-3r.xlsx` through the real import view, runs the test toolbar's *Fill all rounds*, then ORM fixups (presentable names for the fixtures' XSS-probe players, green cross-check sheets, confidence tints) |
| `admin` `scorer` `login` `mobile` | The captures, one browser context per role |
| `hero` | The README's hero image — screen `/1` showing the live standings, at projector size — into `docs/screenshots/` |
| `post` | Crops the tall page captures — the PDF caps figure height, so skyscraper shots would render unreadably small |

Shots land directly in `docs/admin-console/screenshots/` under the filenames
`guide.md` references, except the `hero` stage's, which goes to
`docs/screenshots/standings-screen.png` (the README's hero image). Rebuild the
PDFs to eyeball the result: `manage.py build_docs_pdf` (needs WeasyPrint's
native libraries).

## Notes

- `seed` re-imports (wiping the test tenant) and is safe to re-run. The
  parallel *Fill all rounds — score sheets* can trip sqlite's write lock on a
  slow disk; the script detects that and finishes the validation marks via ORM.
- The server must be **restarted after `collectstatic`** (the WhiteNoise index
  again) — and after editing a template: these profiles run with `DEBUG=False`,
  so Django's cached template loader keeps serving the version it started with.
- `.local/` (the throwaway database) is gitignored.

## Demo video (`demo.py`)

`demo.py` records a narrated tour of the app — sign-in, setup, import, scoring,
the display console, the projector screens, the phone app, the prize-giving —
and stitches it into `docs/screenshots/demo.mp4` plus a `demo.gif`. It runs
against the same instance as `shots.py` (same environment, same server), and
starts by wiping the test tournament so the tour opens on a blank one.

```bash
# ffmpeg comes from the same pixi environment as the browser libraries
cd scripts/screenshots && pixi install && cd -
export LD_LIBRARY_PATH=$PWD/scripts/screenshots/.pixi/envs/default/lib

# same env + server as above, then:
.venv/bin/python scripts/screenshots/demo.py                # everything
.venv/bin/python scripts/screenshots/demo.py --lang fr       # French captions
.venv/bin/python scripts/screenshots/demo.py scoring render  # redo one scene
```

Each scene is one browser context recording its own `.webm` into `.local/demo/`;
`render` pads them all onto a 1280×720 canvas and concatenates, which is how the
portrait phone scene sits in the same film as the landscape ones. Because a
headless recording has no pointer, every page gets an injected caption pill and
a synthetic cursor that follows the driven mouse.

Useful to know:

- The stage list is in the module docstring; any scene name can be replayed
  alone, followed by `render`. Replaying a single scene keeps the other
  segments (a full run clears them first).
- Scene order carries the story of a tournament, and the unrecorded `fill`
  stage is what moves it along: round 1 is filled after the `scoring` scene so
  `publish` can be filmed with rounds 2 and 3 still empty; the middle rounds
  follow, so the screens and the phone show a tournament being played; the last
  round lands only after the `phone` scene, because publishing it puts the
  standings behind *waiting for the ceremony* — which the ceremony scenes then
  reveal.
- `DEMO_PACE` re-times the whole film (default `0.8`); `DEMO_TRIM` is the blank
  head dropped from each segment.
- `--no-gif` skips the gif, which is inevitably heavy — link the mp4 where you
  can. `--lang fr` writes `demo-fr.mp4` / `demo-fr.gif`.
- Captions live in the `TEXT` dict at the top of the script, English and French
  side by side. A caption clears itself when the page unloads, so a scene must
  say something again after navigating — `goto(page, path, key=...)` does that.
- The throwaway DB is put in WAL mode by `bootstrap`: without it a polling
  reader blocks the app's writers and the fill's saves 500 with *database is
  locked*, leaving tables unscored. Restart the server after a first bootstrap
  so it picks the mode up.
