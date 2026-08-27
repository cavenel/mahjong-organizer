# Documentation

## Setting up and running a tournament

[admin-console/guide.md](admin-console/guide.md) is the guide for the crew. It
covers MCR and Riichi events.

The crew also gets this guide as a PDF inside the app, from the avatar menu. It
is built from the Markdown in [admin-console/](admin-console/), so edit the
Markdown and never the PDF.

Two printable one-pagers for the scoring desk at an MCR event:
[scorer](admin-console/mcr-scorer-cheat-sheet.md) (entering scores) and
[head scorer](admin-console/mcr-head-scorer-cheat-sheet.md) (publishing, screens,
ceremony).

## Hosting the app

[hosting/deployment.md](hosting/deployment.md) covers running the server with
Docker: DNS, TLS, the first run, updates and backups.

[hosting/configuration.md](hosting/configuration.md) lists every environment
variable, and what is set per tournament in the admin instead.

[hosting/standalone.md](hosting/standalone.md) covers the single binary for a
venue laptop. No Docker, one sqlite file. It is also the fallback if the server
goes down mid-event.

## Working on the code

[dev/development.md](dev/development.md) covers the settings modules, running the
tests, and running the laptop app from source.

[dev/data-model.md](dev/data-model.md) describes the entities, how the seating
draw relates to competitors, and how a hand encodes draws and self-draws.

[dev/access-control.md](dev/access-control.md) explains how views enforce
per-tenant authorization.

[dev/known-issues.md](dev/known-issues.md) lists the risks the codebase
knowingly carries, and why each was accepted.

[dev/clickthrough-fixtures/](dev/clickthrough-fixtures/) holds small MCR and
Riichi tournaments to import when testing by hand.
