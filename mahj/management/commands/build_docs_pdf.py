"""Render the admin-console docs (the guide + cheat sheets) to PDFs served from
/static/docs/.

Run at build time (see the Dockerfile builder stage, just before
``collectstatic``). Output lands in ``mahj/static/docs/`` — a build artifact that
is gitignored — and is then picked up by ``collectstatic`` and served by
WhiteNoise/nginx at ``/static/docs/<name>.pdf``.

Local use::

    python manage.py build_docs_pdf            # render everything
    python manage.py build_docs_pdf --only guide

Requires WeasyPrint's native libraries (Pango/Cairo/GDK-PixBuf) and the Symbola
font on the host; the Dockerfile installs both in the builder stage.
"""

from pathlib import Path

import markdown
from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

# Files in the source dir that are repo navigation, not printable documents.
SKIP = {"readme.md"}

# Print stylesheet. DejaVu covers the box-drawing characters used in the sidebar
# diagrams; Noto Color Emoji renders the 📸/🔑/🟢 markers (installed in the
# builder stage). max-width keeps the 1920px screenshots inside the page.
CSS = """
/* WeasyPrint mis-positions Noto Color Emoji's bitmap glyphs (they float to the
   top of the page), so emoji come from Symbola — a monochrome outline font that
   shapes inline like any other glyph. Colour-carrying emoji (the status pips,
   ✓ / !) and the ones Symbola lacks are swapped for coloured glyphs in
   render_html() below; Symbola handles the rest (📸 🔑 ⚠ ☰). Digits stay on
   DejaVu Sans, which is listed first. */
@page { size: A4; margin: 1.8cm 1.6cm; }
html { font-size: 11px; }
body {
  font-family: "DejaVu Sans", "Symbola", sans-serif;
  line-height: 1.45; color: #1a1a1a;
}
h1, h2, h3, h4 { line-height: 1.2; break-after: avoid; }
h1 { font-size: 1.9rem; border-bottom: 2px solid #ddd; padding-bottom: .2em; }
/* The guide is one document in role Parts, each an h1: start each Part on a
   fresh page. First-of-type is the document title, which must not page-break.
   Single-h1 documents (the cheat sheets) are unaffected. */
h1:not(:first-of-type) { break-before: page; }
h2 { font-size: 1.45rem; border-bottom: 1px solid #eee; padding-bottom: .15em;
     margin-top: 1.4em; }
h3 { font-size: 1.2rem; margin-top: 1.2em; }
h4 { font-size: 1.05rem; }
p, li { orphans: 2; widows: 2; }
a { color: #1558b0; text-decoration: none; }
code, pre {
  font-family: "DejaVu Sans Mono", monospace; font-size: .92em;
}
code { background: #f3f3f3; padding: .05em .3em; border-radius: 3px; }
pre {
  background: #f6f8fa; border: 1px solid #e3e3e3; border-radius: 6px;
  padding: .7em .9em; white-space: pre; overflow-wrap: normal;
  break-inside: avoid;
}
pre code { background: none; padding: 0; }
blockquote {
  margin: .8em 0; padding: .1em .9em; color: #444;
  border-left: 4px solid #c9c9c9; background: #fafafa; break-inside: avoid;
}
/* Cap figures at ~1/3 of the A4 page height (29.7cm) so a tall screenshot can't
   dominate a page; max-width + max-height + auto keep the aspect ratio. */
img { max-width: 100%; max-height: 9.5cm; height: auto;
      border: 1px solid #e0e0e0; border-radius: 4px; }
table { border-collapse: collapse; width: 100%; margin: .8em 0; break-inside: avoid; }
th, td { border: 1px solid #d0d0d0; padding: .35em .55em; text-align: left;
         vertical-align: top; }
th { background: #f0f0f0; }
hr { border: none; border-top: 1px solid #e0e0e0; margin: 1.6em 0; }
"""

# Extra rules for the one-page cheat sheets: tighter page margins and a smaller
# base font so the whole recap fits on a single side. Appended after CSS, so the
# later @page / html rules win over the defaults above.
COMPACT_CSS = """
html { font-size: 10px; }
"""

HTML_TEMPLATE = (
    "<!DOCTYPE html><html><head><meta charset='utf-8'>"
    "<style>{css}</style></head><body>{body}</body></html>"
)


# Emoji that carry colour meaning (the status pips) or that Symbola can't render
# are swapped for reliably-rendered coloured glyphs. ● (U+25CF) and ✔ (U+2714)
# are covered by DejaVu Sans, so these render inline and in colour. The remaining
# emoji (📸 🔑 ⚠ ☰ ✓ ✕) are left for Symbola.
EMOJI_REPLACEMENTS = {
    "🟢": '<span style="color:#16a34a">●</span>',  # green
    "🟡": '<span style="color:#d97706">●</span>',  # amber
    "🟠": '<span style="color:#ea580c">●</span>',  # orange
    "🔴": '<span style="color:#dc2626">●</span>',  # red
    "🔵": '<span style="color:#2563eb">●</span>',  # blue
    "⚪": '<span style="color:#9ca3af">●</span>',  # grey
    "✅": '<span style="color:#16a34a">✔</span>',
    "❗": '<span style="color:#dc2626;font-weight:bold">!</span>',
    "🧪": "⚗",  # Symbola lacks the test-tube; use the alembic it does have
}


def render_html(md_text: str, compact: bool = False) -> str:
    """Markdown source -> full HTML document string (no native libs needed)."""
    body = markdown.markdown(
        md_text,
        extensions=["extra", "sane_lists", "toc"],
        output_format="html5",
    )
    for char, repl in EMOJI_REPLACEMENTS.items():
        body = body.replace(char, repl)
    css = CSS + COMPACT_CSS if compact else CSS
    return HTML_TEMPLATE.format(css=css, body=body)


class Command(BaseCommand):
    help = "Render docs/admin-console/*.md to PDFs under mahj/static/docs/."

    def add_arguments(self, parser):
        parser.add_argument(
            "--source",
            default=str(Path(settings.BASE_DIR) / "docs" / "admin-console"),
            help="Directory of Markdown guides to render.",
        )
        parser.add_argument(
            "--out",
            default=str(Path(settings.BASE_DIR) / "mahj" / "static" / "docs"),
            help="Directory to write the PDFs into.",
        )
        parser.add_argument(
            "--only",
            nargs="*",
            metavar="NAME",
            help="Render only these guides (basenames without .md).",
        )

    def handle(self, *args, **opts):
        source = Path(opts["source"])
        out = Path(opts["out"])
        only = {n.removesuffix(".md") for n in (opts.get("only") or [])}

        if not source.is_dir():
            raise CommandError(f"Source directory not found: {source}")

        # Import lazily so `manage.py help` and the HTML path work without the
        # native Pango/Cairo libraries installed.
        try:
            from weasyprint import HTML
        except OSError as exc:  # missing native libs
            raise CommandError(
                "WeasyPrint could not load its native libraries. Install Pango, "
                "Cairo and GDK-PixBuf (Debian: libpango-1.0-0 libpangocairo-1.0-0 "
                "libgdk-pixbuf-2.0-0 libffi-dev shared-mime-info).\n\n"
                f"Original error: {exc}"
            ) from exc

        out.mkdir(parents=True, exist_ok=True)

        guides = [
            p for p in sorted(source.glob("*.md"))
            if p.name.lower() not in SKIP and (not only or p.stem in only)
        ]
        if not guides:
            raise CommandError(f"No matching guides found in {source}")

        for md in guides:
            compact = "cheat_sheet" in md.stem.lower()
            html = render_html(md.read_text(encoding="utf-8"), compact=compact)
            target = out / f"{md.stem}.pdf"
            # base_url = the guide's own dir so relative screenshots/… resolve.
            HTML(string=html, base_url=f"{md.parent}/").write_pdf(target)
            self.stdout.write(self.style.SUCCESS(f"  ✓ {md.name} → {target.relative_to(settings.BASE_DIR)}"))

        self.stdout.write(self.style.SUCCESS(f"Rendered {len(guides)} PDF(s) to {out}"))
