# Standalone app (single-binary, no Docker)

The tournament admin can run two ways from the **same** code:

- **Robust (Docker Compose)** — Postgres, Redis, Channels, workers, nginx. For a
  server or a busy multi-tenant deployment. See `RUNBOOK.md`.
- **Standalone (this doc)** — one process, a local sqlite file, no external
  services, packaged as a per-OS binary a non-technical operator double-clicks on
  a venue laptop. Manual score entry (no camera scan/OCR).

Both serve the live projector screens (`/1`, counter, ceremony, …). The public
spectator website is published separately as static files (see the `PUBLISH_SFTP_*`
settings and the "Publish to web" button).

## Running from source

```
pip install -r requirements/standalone.txt
python -m standalone.run
```

It opens `http://127.0.0.1:8000/options` in your browser. First run creates an
`admin`/`admin` user — **change the password in the app.**

## Configuration (.env)

Config is **not** baked into the binary. On first run the launcher writes an
editable `.env` template into the OS user-data dir and generates a secret key.
Edit it and relaunch. Search order (first found wins):

1. a `.env` next to the binary,
2. the user-data dir (below).

Keys that matter here: `LOCAL_TENANT` (the tournament's subdomain — pins this
laptop to one tenant), `VENUE_TZ`, and the optional `PUBLISH_SFTP_*` / `PUBLISH_TENANT`
block for publishing the public website. (No `DB_*` or Redis vars — those are the
Docker profile's.)

## Where your data lives

| OS       | Data dir |
|----------|----------|
| Windows  | `%APPDATA%\Mahjong\` |
| macOS    | `~/Library/Application Support/Mahjong/` |
| Linux    | `~/.config/mahjong/Mahjong/` |

- `mahj.sqlite3` — the whole tournament database (one file).
- `snapshots/` — automatic backups.
- `.env` — your configuration.

Keep this on **local disk** — never on a network share / Dropbox / OneDrive
folder (that is the main cause of sqlite corruption).

## Backups & recovery

The launcher takes an **online-backup snapshot** into `snapshots/` at startup and
every few minutes while running (safe on a live database, unlike copying the file),
keeping the most recent ones. It also runs an integrity check on startup and
refuses to serve a corrupt database, pointing you at the snapshots.

**To recover:** quit the app, go to the data dir, rename the newest
`snapshots/mahj-YYYYMMDD-HHMMSS.sqlite3` to `mahj.sqlite3` (replacing the bad one),
and relaunch. Last resort with no snapshot:
`sqlite3 mahj.sqlite3 ".recover" | sqlite3 recovered.sqlite3`.

Concurrent writes never corrupt sqlite — it serializes writers, and WAL mode plus
a busy timeout (both set automatically) mean a rare collision waits rather than
errors. The write volume of one operator is far below any contention concern.

## Building the binary (per OS)

PyInstaller builds for the OS it runs on — build on each target platform (or in a
CI matrix). There is no single cross-platform binary.

`tailwind.min.css` is a **generated** artifact: Tailwind's JIT scans the templates
and emits only the classes they use, so it must be rebuilt from the current
templates or the app renders unstyled/partly-styled. The Docker image does this
every build; a manual build must do it too, using the standalone Tailwind CLI
(v3.4.17, no Node — matches the Dockerfile). Order matters:
**Tailwind → collectstatic → PyInstaller.**

The Tailwind CLI is a per-OS native binary (like PyInstaller's output), so pick
the asset for the machine you build on — `tailwindcss-windows-x64.exe`,
`tailwindcss-macos-arm64`, `tailwindcss-macos-x64`, `tailwindcss-linux-x64`, or
`tailwindcss-linux-arm64`.

Linux/macOS (auto-detects OS + arch):
```
pip install -r requirements/standalone.txt
case "$(uname -s)" in Linux) OS=linux;; Darwin) OS=macos;; esac
case "$(uname -m)" in x86_64|amd64) ARCH=x64;; arm64|aarch64) ARCH=arm64;; esac
curl -sLo tailwindcss "https://github.com/tailwindlabs/tailwindcss/releases/download/v3.4.17/tailwindcss-${OS}-${ARCH}" && chmod +x tailwindcss
./tailwindcss -c tailwind.config.js -i mahj/static/css/tailwind.src.css -o mahj/static/css/tailwind.min.css --minify
python manage.py collectstatic --noinput --settings apps.settings.standalone
pyinstaller --noconfirm standalone/mahj.spec
```

Windows (PowerShell):
```
pip install -r requirements/standalone.txt
Invoke-WebRequest https://github.com/tailwindlabs/tailwindcss/releases/download/v3.4.17/tailwindcss-windows-x64.exe -OutFile tailwindcss.exe
.\tailwindcss.exe -c tailwind.config.js -i mahj/static/css/tailwind.src.css -o mahj/static/css/tailwind.min.css --minify
python manage.py collectstatic --noinput --settings apps.settings.standalone
pyinstaller --noconfirm standalone/mahj.spec
```

The app lands in `dist/mahj-admin/`. The regenerated `tailwind.min.css` is a build
artifact — no need to commit it. The scan/OCR stack (OpenCV, the LLM client) and
the Postgres/Redis backends are deliberately excluded to keep the binary small —
the standalone settings never load them.
