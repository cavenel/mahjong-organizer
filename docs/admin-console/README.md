# Admin console — operator documentation

**Mahj.OVH** (Mahjong Organizer Virtual Hub) is the back-office web app the
tournament crew uses to run an event: entering scores, publishing the
leaderboard, driving the projector/TV screens, and running the prize-giving
ceremony.

Everything is one document with a **part per role** — read
[**guide.md**](guide.md) and start from its *Find your part* table (Scorer,
Publisher, Display operator, Tournament admin). MCR-only features (Table
Points, score sheets, scanning) are marked inline, so the same guide serves MCR
and Riichi events.

Two printable one-page props accompany it, for taping to the scoring desk at an
MCR event:

- [MCR_scorer_cheat_sheet.md](MCR_scorer_cheat_sheet.md) — score-entry steps.
- [MCR_head_scorer_cheat_sheet.md](MCR_head_scorer_cheat_sheet.md) — publishing,
  screens and ceremony.

## The app serves these as PDFs

The Docker build renders every Markdown file here to a PDF
(`manage.py build_docs_pdf` → `/static/docs/<name>.pdf`), and the console's
avatar menu links them for every signed-in role. Edit the Markdown — the PDFs
are build artifacts, never edited directly.

## Screenshots

The guide's images live under [screenshots/](screenshots/). See
[screenshots/README.md](screenshots/README.md) for where each is captured and
which ones still need (re)capturing.
