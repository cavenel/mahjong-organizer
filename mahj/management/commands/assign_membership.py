"""Grant (or update) a user's per-tenant Membership from the command line.

The escape hatch the data migration points at: on a multi-tenant install the
migration can't guess which tenant a global role meant, and an operator seeding
a fresh tenant needs to hand its first admin their access before the in-app
console is reachable. Example:

    manage.py assign_membership alice oemc2026 --roles=tenant_admin
    manage.py assign_membership bob oemc2026 --roles=scorer,display_op
"""
from django.contrib.auth.models import User
from django.core.management.base import BaseCommand, CommandError

from mahj.models import Membership, Tenant

ROLE_FLAGS = ('tenant_admin', 'scorer', 'display_op', 'publisher')


class Command(BaseCommand):
    help = "Create or update a user's Membership (roles) in a tenant."

    def add_arguments(self, parser):
        parser.add_argument('username')
        parser.add_argument('subdomain', help="the tenant's subdomain")
        parser.add_argument(
            '--roles', default='',
            help=f"comma-separated roles: {', '.join(ROLE_FLAGS)} "
                 "(tenant_admin implies every app role)")

    def handle(self, *args, **options):
        try:
            user = User.objects.get(username=options['username'])
        except User.DoesNotExist:
            raise CommandError(f"No user named {options['username']!r}.")
        try:
            tenant = Tenant.objects.get(subdomain=options['subdomain'])
        except Tenant.DoesNotExist:
            raise CommandError(f"No tenant with subdomain {options['subdomain']!r}.")

        wanted = [r.strip() for r in options['roles'].split(',') if r.strip()]
        unknown = [r for r in wanted if r not in ROLE_FLAGS]
        if unknown:
            raise CommandError(
                f"Unknown role(s): {', '.join(unknown)}. Choose from {', '.join(ROLE_FLAGS)}.")

        flags = {f'is_{r}': (r in wanted) for r in ROLE_FLAGS}
        membership, created = Membership.objects.update_or_create(
            user=user, tenant=tenant, defaults=flags)
        verb = 'Created' if created else 'Updated'
        self.stdout.write(self.style.SUCCESS(
            f"{verb} membership: {membership}"))
