# Standalone app (single binary, no Docker)

The same code runs two ways:

- **Docker Compose**, for a server, or several tournaments on one instance. See
  [deployment.md](deployment.md).
- **Standalone**, this doc. One process, one sqlite file, no other services. One
  binary per operating system. Double-click it on a venue laptop. Score entry is
  manual, with no camera scan.

Both give you the admin console and the projector screens (`/1`, counter,
ceremony). The spectator website is separate: set a target under Administration →
Publish target, then use "Publish to web".

## Start the app

1. Download the file for your system from the
   [latest release](https://github.com/cavenel/mahjong-organizer/releases/latest).
   Windows x64, macOS on Apple Silicon or Intel, and Linux x64.
2. Unpack it and start `mahj-organizer`, or `mahj-organizer.exe` on Windows. A
   console window opens. Leave it open, because closing it stops the app. Your
   browser opens on `http://127.0.0.1:8000/options`.

Keep the unpacked folder together. The program needs the files next to it, so
move the whole folder and never `mahj-organizer` on its own.

The first run creates an `admin` user with a random password. The password is
printed in the console and saved to `first-login.txt` in the data dir. Log in,
change the password under Administration → Users, then delete that file. The
password is random because the app listens on the whole network, so a fixed one
would let anyone at the venue in.

Both systems block downloaded apps the first time:

- **Windows**: SmartScreen says "unknown publisher". Click *More info > Run
  anyway*.
- **macOS**: you get a dialog saying `Python.framework` "is damaged". It is not.
  See below.

<details>
<summary><b>macOS says the app is damaged</b></summary>

Your browser marks the download with a quarantine flag. Our binaries are not
notarised by Apple, so macOS refuses to load them while that flag is set.

The dialog says "damaged". The console says `library load disallowed by system
policy`. Both mean the same thing.

Clear the flag once, on the unpacked **folder**:

```
xattr -dr com.apple.quarantine /path/to/mahj-organizer
```

Then start the app. You only repeat this after downloading a new release.

- Use the folder, not the launcher inside it. `-r` also clears `_internal/`, and
  the files that fail the check are in there.
- If you clicked "Move to Bin", get the file back out of the Bin or unpack the
  archive again, then clear the flag.
- Unpacking in Terminal (`tar xzf mahj-organizer-macos-x64.tar.gz`) skips the
  problem. Finder's Archive Utility is what copies the flag onto every file.
- Fixing this for good needs a paid Apple Developer account, to sign and notarise
  each release.

</details>

## Other devices on the network

The app listens on all network interfaces, so projector and scorer devices on the
same network can reach it at the laptop's LAN IP.

Open the admin **Display** page (`/admin?page=display`). It lists the URLs: this
machine, the LAN IP for other devices, and the public IP if you forward a port on
the router. Open one on the projector machine and add the screen number, `/1`,
`/2`, and so on.

The admin console is on the network too. It asks for a login, but change the
`admin` password before you use an untrusted wifi.

## Your data

| OS       | Data dir |
|----------|----------|
| Windows  | `%APPDATA%\Mahjong\` |
| macOS    | `~/Library/Application Support/Mahjong/` |
| Linux    | `~/.config/Mahjong/` |

Two files matter:

- `mahj.sqlite3` holds the whole tournament.
- `.env` holds the timezone and the secret key that unlocks your saved publish
  password. Keep it with the database. See below.

Keep both on **local disk**. Do not use a network share, Dropbox or OneDrive.
That is the main cause of sqlite corruption.

<details>
<summary><b>Settings in .env</b></summary>

Config is not built into the binary. The first run writes a `.env` template into
the data dir and generates a secret key. Edit the file and restart the app. The
launcher uses the first `.env` it finds: next to the binary, then in the data dir.

There are two settings:

- `VENUE_TZ` is the venue's timezone, for the projector clock.
- `DJANGO_SECRET_KEY` is generated for you. Do not edit it.

There are no database or Redis settings. Those belong to Docker. Publishing is
set up in the admin, not here.

**Back up the `.env` with the database, and copy both when you move an install to
another machine.** Without the file the app still starts on a new secret key, but
your saved publish password or private key can no longer be decrypted, and you
have to enter the publish target again. Logins are also reset.

</details>

## Backups

Download a tournament dump from *Administration → Backup & restore* at every
break.

If you have a publish target set up, each "Publish to web" also uploads a dump to
the `backup/` folder on that host. With no target, or with no network, the dump
you download by hand is the only copy of the event outside this laptop. That is
the normal situation after restoring onto the laptop mid-event, because a dump
does not carry the publish target.

To restore one, use the same page: upload the file and type `local` to confirm.

<details>
<summary><b>What a dump holds</b></summary>

Settings, players, seating, entered scores, published rounds, the schedule and
the round timer.

It does not hold the publish target or the user accounts. Enter the target again
after a restore if you want to keep publishing.

A dump records the app version it was made on and will not restore onto a
different one.

</details>

<details>
<summary><b>If the database is corrupt</b></summary>

The launcher checks the database at startup and stops instead of serving a broken
one.

Quit, delete `mahj.sqlite3` from the data dir, and start the app again. You get a
fresh database and a new password in `first-login.txt`. Sign in and restore your
last dump.

With no dump, try this from the data dir first:

```
sqlite3 mahj.sqlite3 ".recover" | sqlite3 recovered.sqlite3
```

Rename `recovered.sqlite3` to `mahj.sqlite3`. Delete `mahj.sqlite3-wal` and
`mahj.sqlite3-shm` next to it, or they replay onto the file you just recovered.
Then start the app.

One operator does not write enough to strain sqlite. The app turns on WAL mode
and a busy timeout, so two writes at once wait rather than fail.

</details>

## If the server dies mid-event

A dump restores into any install of the same app version, so you can move the
tournament onto this laptop.

1. Get the server's latest dump. Look in `mahj-backups/` in the SFTP user's login
   directory, or wherever the publish target's **Backup directory** points. A copy
   you downloaded earlier works too.
2. Restore the dump here. Type `local` in the confirmation box. A dump made on
   the server restores fine under a different name.
3. Run the rest of the event from the laptop, publishing from here.
4. When the server is back, dump from the laptop and restore it there.

<details>
<summary><b>Building the binary</b></summary>

PyInstaller builds for the OS it runs on. Build on each platform, or in a CI
matrix. There is no cross-platform binary. The released binaries are built by
[`release.yml`](../../.github/workflows/release.yml).

`tailwind.min.css` is generated. Tailwind reads the templates and writes out only
the classes they use, so it has to be rebuilt from the current templates or the
app renders unstyled. Run the steps in this order: **Tailwind → collectstatic →
PyInstaller**.

The Tailwind CLI is also one binary per OS. Version 3.4.17 needs no Node and
matches the Dockerfile.

Linux and macOS, detecting OS and architecture:

```
pip install -r requirements/standalone.txt
case "$(uname -s)" in Linux) OS=linux;; Darwin) OS=macos;; esac
case "$(uname -m)" in x86_64|amd64) ARCH=x64;; arm64|aarch64) ARCH=arm64;; esac
curl -sLo tailwindcss "https://github.com/tailwindlabs/tailwindcss/releases/download/v3.4.17/tailwindcss-${OS}-${ARCH}" && chmod +x tailwindcss
./tailwindcss -c tailwind.config.js -i mahj/static/css/tailwind.src.css -o mahj/static/css/tailwind.min.css --minify
python manage.py collectstatic --noinput --settings apps.settings.standalone
pyinstaller --noconfirm standalone/mahj.spec
```

Windows, in PowerShell:

```
pip install -r requirements/standalone.txt
Invoke-WebRequest https://github.com/tailwindlabs/tailwindcss/releases/download/v3.4.17/tailwindcss-windows-x64.exe -OutFile tailwindcss.exe
.\tailwindcss.exe -c tailwind.config.js -i mahj/static/css/tailwind.src.css -o mahj/static/css/tailwind.min.css --minify
python manage.py collectstatic --noinput --settings apps.settings.standalone
pyinstaller --noconfirm standalone/mahj.spec
```

The app lands in `dist/mahj-organizer/`. The rebuilt CSS is a build artifact and
does not need committing. The build leaves out the scan and OCR stack (OpenCV,
the LLM client) and the Postgres and Redis backends, which the standalone
settings never load.

</details>
