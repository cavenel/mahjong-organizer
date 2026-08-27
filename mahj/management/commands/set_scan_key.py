"""Set a tenant's score-sheet scanning API key from the command line.

Scanning is strictly bring-your-own-key — there is no ANTHROPIC_API_KEY fallback,
in production or in development — so this is how an operator seeds an existing
tenant during cutover, and how a developer gives a local tenant a key at all.

    echo -n "$KEY" | python manage.py set_scan_key <subdomain>

The key is read from **stdin, never from argv**: a command line lands in shell
history and is visible in `ps` to every user on the box.
"""
import sys

from django.core.management.base import BaseCommand, CommandError

from mahj import scan_key
from mahj.models import ScanConfig, Tenant


class Command(BaseCommand):
    help = "Set a tenant's scanning API key, read from stdin."

    def add_arguments(self, parser):
        parser.add_argument('subdomain')
        parser.add_argument('--clear', action='store_true',
                            help="Remove the stored key instead of setting one.")

    def handle(self, *args, **options):
        subdomain = options['subdomain']
        try:
            tenant = Tenant.objects.get(subdomain=subdomain)
        except Tenant.DoesNotExist:
            raise CommandError(f"No tournament with subdomain {subdomain!r}.")

        cfg, _ = ScanConfig.objects.get_or_create(tenant=tenant)
        if options['clear']:
            cfg.api_key_enc = None
            cfg.key_tail = ''
            cfg.save()
            self.stdout.write(self.style.SUCCESS(f"Scanning key cleared for {subdomain}."))
            return

        key = sys.stdin.read().strip()
        if not key:
            raise CommandError("No key on stdin. Pipe one in: echo -n \"$KEY\" | ...")
        cfg.api_key_enc = scan_key.encrypt(key)
        cfg.key_tail = key[-4:]
        cfg.last_error = ''
        cfg.last_error_at = None
        cfg.save()
        # The tail only — never the key, not even to a terminal the operator owns.
        self.stdout.write(self.style.SUCCESS(
            f"Scanning key set for {subdomain} (ends …{key[-4:]})."))
