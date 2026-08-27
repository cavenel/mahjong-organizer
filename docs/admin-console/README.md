# Crew documentation

[guide.md](guide.md) is the guide for the tournament crew. It covers entering
scores, publishing the leaderboard, driving the screens in the room, and the
prize-giving ceremony. It has one part per role, so start from its *Find your
part* table. Features that only exist for MCR are marked, so the same guide works
for Riichi.

Two one-page sheets to print and tape to the scoring desk at an MCR event:

- [mcr-scorer-cheat-sheet.md](mcr-scorer-cheat-sheet.md) for entering scores.
- [mcr-head-scorer-cheat-sheet.md](mcr-head-scorer-cheat-sheet.md) for
  publishing, the screens and the ceremony.

## The app serves these files as PDFs

The Docker build renders every Markdown file in this folder to a PDF with
`manage.py build_docs_pdf`, and serves them at `/static/docs/<name>.pdf`. The
console's avatar menu links to them for every signed-in role. Edit the Markdown.
The PDFs are build artifacts.

Two consequences for anything written here. Collapsible `<details>` blocks do not
work, because the PDF renderer does not support them. And renaming a file changes
its PDF name, so update the avatar-menu links in
`mahj/templates/mahj/admin.html`.

## Screenshots

The guide's images are in [screenshots/](screenshots/). Its
[README](screenshots/README.md) says where each one is captured and how to
recapture them.
