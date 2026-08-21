# Documentation

One folder per audience. Start from the row that describes you.

| You are… | Start here |
|----------|------------|
| **Tournament crew** — scoring, publishing, screens, the ceremony | [admin-console/guide.md](admin-console/guide.md) — one guide with a part per role (Scorer, Publisher, Display operator, Admin), plus two printable [cheat sheets](admin-console/README.md) |
| **Hosting an instance** — the server behind the tournament | [hosting/deployment.md](hosting/deployment.md) (Docker, DNS/TLS, first run), [hosting/configuration.md](hosting/configuration.md) (every environment variable), [hosting/STANDALONE.md](hosting/STANDALONE.md) (the venue-laptop failover build) |
| **Working on the code** | [dev/data-model.md](dev/data-model.md), [dev/access-control.md](dev/access-control.md), [dev/known-issues.md](dev/known-issues.md) (accepted risks + invariants), [dev/clickthrough-fixtures/](dev/clickthrough-fixtures/) |

One thing that lives elsewhere on purpose:

- The crew guide doubles as the app's built-in help: the Docker build renders
  `admin-console/*.md` to PDFs (`manage.py build_docs_pdf`) served at
  `/static/docs/<name>.pdf` and linked from the admin console's user menu. Edit
  the Markdown here — the PDFs are build artifacts, never edited directly.
