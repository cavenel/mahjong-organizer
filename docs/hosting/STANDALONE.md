# Standalone app (single-binary, no Docker)

The tournament admin can run two ways from the **same** code:

- **Robust (Docker Compose)** — Postgres, Redis, Channels, workers, nginx. For a
  server or a busy multi-tenant deployment. See
  [deployment.md](deployment.md).
- **Standalone (this doc)** — one process, a local sqlite file, no external
  services, packaged as a per-OS binary a non-technical operator double-clicks on
  a venue laptop. Manual score entry (no camera scan/OCR).

Both serve the live projector screens (`/1`, counter, ceremony, …). The public
spectator website is published separately as static files (configure a target in
the admin console, Administration → Publish target, and use "Publish to web").

## Running from source

```
pip install -r requirements/standalone.txt
python -m standalone.run
```

It opens `http://127.0.0.1:8000/options` in your browser. First run creates an
`admin` user with a **randomly generated password**, printed to the console and
saved to `first-login.txt` in the app's data directory. Log in, change the
password (Administration → Users), then delete that file.

The password is generated rather than fixed because the app binds to all
interfaces (see below): a known default would hand full admin to anyone on the
venue network, including a guest wifi it happens to share.

## Opening screens on other devices (LAN)

The app binds to all interfaces, so projector/scorer devices on the same network
can reach it by the laptop's LAN IP. The admin **Display** page
(`/admin?page=display`) lists the exact URLs — loopback for this machine, the LAN
IP for other devices, and the public IP (only if you port-forward the router).
Open one on the projector machine and add the screen number (`/1`, `/2`, …).

Because the app is now reachable from the LAN, the (login-gated) admin console is
too — so **changing the default `admin` password matters** on an untrusted network.

## Configuration (.env)

Config is **not** baked into the binary. On first run the launcher writes an
editable `.env` template into the OS user-data dir and generates a secret key.
Edit it and relaunch. Search order (first found wins):

1. a `.env` next to the binary,
2. the user-data dir (below).

Keys that matter here: `LOCAL_TENANT` (the tournament's subdomain — pins this
laptop to one tenant) and `VENUE_TZ`. (No `DB_*` or Redis vars — those are the
Docker profile's. Publishing the public website is configured in the admin, not
via env — see Administration → Publish target.)

## Where your data lives

| OS       | Data dir |
|----------|----------|
| Windows  | `%APPDATA%\Mahjong\` |
| macOS    | `~/Library/Application Support/Mahjong/` |
| Linux    | `~/.config/Mahjong/` |

- `mahj.sqlite3` — the whole tournament database (one file).
- `.env` — your configuration.

Keep this on **local disk** — never on a network share / Dropbox / OneDrive
folder (that is the main cause of sqlite corruption).

## Backups & recovery

There is one backup mechanism, the same one the server build uses: a **tournament
dump**, downloaded from *Administration → Backup & restore*. Download one at
each break — it is the only copy of the event that exists off this machine.

The launcher runs an integrity check on the database at startup and refuses to
serve a corrupt one rather than starting on a file that half-works and renders
wrong standings. If that happens: quit, delete `mahj.sqlite3` from the data dir,
and relaunch. A fresh database is created and a new admin password is written to
`first-login.txt`; sign in and restore your most recent dump under **Backup &
restore**.

## Running the tournament from this laptop (server fallback)

A tournament dump is portable between installs, so this build doubles as the
fallback when the server is unreachable mid-event:

1. Get the latest dump for the tournament — from the server's publish target
   (the `mahj-backups/` folder in the SFTP user's **login directory** — not under
   the site directory — or wherever its **Backup directory** points), or from a
   copy downloaded earlier on
   *Administration → Backup & restore*.
2. In this app, create/select the tournament's tenant (`LOCAL_TENANT`), open
   **Backup & restore**, upload the dump and retype the subdomain.
3. Everything comes back: settings, players, seating, entered scores, published
   rounds, the schedule and the round timer. Publish target and user accounts are
   *not* in the dump — re-enter the target here if you want to keep publishing the
   spectator site.
4. Afterwards, download a dump from this laptop and restore it onto the server the
   same way, so the server has the rest of the event.

Both installs must be running the **same app version** — a dump records the
schema it was made on and refuses to restore onto a different one rather than
loading half of itself.

**If the database is damaged and you have no dump**, it is worth one attempt
before starting over: quit, and from the data dir run
`sqlite3 mahj.sqlite3 ".recover" | sqlite3 recovered.sqlite3`, then rename
`recovered.sqlite3` to `mahj.sqlite3`. Delete `mahj.sqlite3-wal` and
`mahj.sqlite3-shm` alongside it — stale sidecars can replay onto the file you
just recovered — and relaunch.

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
