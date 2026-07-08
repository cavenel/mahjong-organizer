"""Export the public spectator page as a static site, and optionally upload it.

Renders `/` (the desktop leaderboard) plus its player/team/per-table detail
modals to static HTML using the live views/templates, copies the referenced
static assets, and — unless --no-upload — pushes the result to the configured
SFTP host (see mahj/publish/sftp_upload and the PUBLISH_SFTP_* env vars).

Local use::

    # render only, then serve it with any dumb static server to eyeball it:
    python manage.py export_public --subdomain myevent --no-upload
    python -m http.server -d captures/export/myevent

    # render + upload (needs PUBLISH_SFTP_* configured):
    python manage.py export_public --subdomain myevent

Requires collectstatic to have run (STATIC_ROOT populated); pass --collectstatic
to run it first in a dev checkout.
"""
from pathlib import Path

from django.conf import settings
from django.core.management import call_command
from django.core.management.base import BaseCommand, CommandError

from ...publish.static_export import export_public


class Command(BaseCommand):
    help = "Render the public spectator page to static files and optionally upload it."

    def add_arguments(self, parser):
        parser.add_argument('--subdomain', required=True,
                            help="Tenant subdomain to export.")
        parser.add_argument('--out', default=None,
                            help="Base output dir (default: <captures>/export). "
                                 "Files land in <out>/<subdomain>/.")
        parser.add_argument('--no-upload', action='store_true',
                            help="Render locally only; skip the SFTP upload.")
        parser.add_argument('--collectstatic', action='store_true',
                            help="Run collectstatic first (convenience in dev).")

    def handle(self, *args, **opts):
        subdomain = opts['subdomain']
        if opts['collectstatic']:
            call_command('collectstatic', '--noinput', verbosity=0)

        base = opts['out'] or str(Path(settings.BASE_DIR) / 'captures' / 'export')
        out_dir = Path(base) / subdomain

        try:
            export_public(subdomain, out_dir)
        except Exception as e:
            raise CommandError(f"Export failed: {e}")
        self.stdout.write(self.style.SUCCESS(f"Exported {subdomain} → {out_dir}"))

        if opts['no_upload']:
            self.stdout.write("Skipping upload (--no-upload).")
            return

        # Lazy import so a render-only run never needs paramiko installed.
        from ...publish.sftp_upload import upload_dir, is_configured
        if not is_configured():
            self.stdout.write(self.style.WARNING(
                "PUBLISH_SFTP_HOST not set — nothing uploaded. "
                "Configure PUBLISH_SFTP_* or pass --no-upload."))
            return
        try:
            upload_dir(out_dir, subdomain=subdomain)
        except Exception as e:
            raise CommandError(f"Upload failed: {e}")
        self.stdout.write(self.style.SUCCESS(f"Uploaded {subdomain} to the web host."))
