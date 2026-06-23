"""Render the admin-console Markdown guides to PDFs served from /static/docs/.

Run at build time (see the Dockerfile builder stage, just before
``collectstatic``). Output lands in ``mahj/static/docs/`` — a build artifact that
is gitignored — and is then picked up by ``collectstatic`` and served by
WhiteNoise/nginx at ``/static/docs/<name>.pdf``.

Local use::

    python manage.py build_docs_pdf            # render every guide
    python manage.py build_docs_pdf --only full_admin MCR_scorers

Requires WeasyPrint's native libraries (Pango/Cairo/GDK-PixBuf) and a colour
emoji font on the host; the Dockerfile installs both in the builder stage.
"""

from pathlib import Path

import markdown
from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

# Files in the source dir that are repo navigation, not standalone guides.
SKIP = {"readme.md"}

# Print stylesheet. DejaVu covers the box-drawing characters used in the sidebar
# diagrams; Noto Color Emoji renders the 📸/🔑/🟢 markers (installed in the
# builder stage). max-width keeps the 1920px screenshots inside the page.
CSS = """
/* Noto Color Emoji also "covers" the ASCII digits 0-9, # and * (they are the
   bases of keycap emoji like 1️⃣). If it is named plainly in font-family, the
   text engine grabs it for bare digits and renders them blank. Gating it behind
   a unicode-range that excludes digits means it is only ever used for real
   emoji/symbols, so digits stay on DejaVu Sans. */
@font-face {
  font-family: "EmojiFallback";
  src: local("Noto Color Emoji");
  unicode-range: U+203C, U+2049, U+20E3, U+2122, U+2139, U+2190-21FF,
    U+2300-23FF, U+2460-24FF, U+25A0-27BF, U+2900-297F, U+2B00-2BFF,
    U+3030, U+303D, U+3297, U+3299, U+FE00-FE0F, U+1F000-1FAFF;
}
@page { size: A4; margin: 1.8cm 1.6cm; }
html { font-size: 11px; }
body {
  font-family: "DejaVu Sans", "Noto Sans", "EmojiFallback", sans-serif;
  line-height: 1.45; color: #1a1a1a;
}
h1, h2, h3, h4 { line-height: 1.2; break-after: avoid; }
h1 { font-size: 1.9rem; border-bottom: 2px solid #ddd; padding-bottom: .2em; }
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

HTML_TEMPLATE = (
    "<!DOCTYPE html><html><head><meta charset='utf-8'>"
    "<style>{css}</style></head><body>{body}</body></html>"
)


def render_html(md_text: str) -> str:
    """Markdown source -> full HTML document string (no native libs needed)."""
    body = markdown.markdown(
        md_text,
        extensions=["extra", "sane_lists", "toc"],
        output_format="html5",
    )
    return HTML_TEMPLATE.format(css=CSS, body=body)


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
            html = render_html(md.read_text(encoding="utf-8"))
            target = out / f"{md.stem}.pdf"
            # base_url = the guide's own dir so relative screenshots/… resolve.
            HTML(string=html, base_url=f"{md.parent}/").write_pdf(target)
            self.stdout.write(self.style.SUCCESS(f"  ✓ {md.name} → {target.relative_to(settings.BASE_DIR)}"))

        self.stdout.write(self.style.SUCCESS(f"Rendered {len(guides)} PDF(s) to {out}"))
