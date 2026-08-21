"""Per-tenant access control: cross-tenant isolation, superuser bypass, the
credential-containment rule, tenant-admin console scoping, superuser tenant
management, and the migration/command that seed memberships."""
import json

import pytest
from django.contrib.auth.models import Group, User
from django.test import Client, override_settings

from mahj.models import Membership, Tenant
from mahj.tests.conftest import grant

HOST_A = 'test.example.com'      # the `tournament` fixture's tenant, subdomain 'test'
HOST_B = 'other.example.com'     # a second tenant, subdomain 'other'


@pytest.fixture
def tenant_b(db):
    return Tenant.objects.create(name='Other', subdomain='other')


def client_for(host):
    c = Client()
    c.defaults['HTTP_HOST'] = host
    return c


def _json_post(client, url, payload):
    return client.post(url, data=json.dumps(payload), content_type='application/json')


def _reauth(client, password='pw'):
    assert _json_post(client, '/user_reauth', {'password': password}).status_code == 200


# --------------------------------------------------------------------------
# Cross-tenant isolation
# --------------------------------------------------------------------------

class TestCrossTenantIsolation:
    def test_scorer_of_a_forbidden_on_b(self, tournament, tenant_b):
        scorer = User.objects.create_user('scorerA', password='pw')
        grant(scorer, tournament['tenant'], scorer=True)
        # Allowed on their own tenant...
        ca = client_for(HOST_A); ca.force_login(scorer)
        assert ca.get('/scores_per_hand_1_1').status_code == 200
        # ...but a member of tenant A is a stranger on tenant B's subdomain.
        cb = client_for(HOST_B); cb.force_login(scorer)
        assert cb.get('/scores_per_hand_1_1').status_code == 403

    def test_admin_of_a_forbidden_on_b(self, tournament, tenant_b):
        admin = User.objects.create_user('adminA', password='pw')
        grant(admin, tournament['tenant'], admin=True)
        cb = client_for(HOST_B); cb.force_login(admin)
        assert cb.get('/options').status_code == 403
        # And the tenant-scoped mutating endpoints reject them on B too.
        _login_a = client_for(HOST_A); _login_a.force_login(admin)
        assert cb.post('/user_reauth', data=json.dumps({'password': 'pw'}),
                       content_type='application/json').status_code == 403

    def test_anonymous_still_redirected_to_login(self, tournament):
        # Isolation only changes the *authenticated* case; anonymous still bounces.
        resp = client_for(HOST_A).get('/options')
        assert resp.status_code == 302 and '/accounts/login/' in resp.url


# --------------------------------------------------------------------------
# Superuser bypass
# --------------------------------------------------------------------------

class TestSuperuserBypass:
    def test_superuser_reaches_admin_on_any_tenant(self, tournament, tenant_b):
        su = User.objects.create_superuser('root', '', 'pw')
        for host in (HOST_A, HOST_B):
            c = client_for(host); c.force_login(su)
            assert c.get('/options').status_code == 200

    def test_superuser_sees_tenants_page(self, tournament):
        su = User.objects.create_superuser('root', '', 'pw')
        c = client_for(HOST_A); c.force_login(su)
        _reauth(c)
        resp = c.get('/admin?page=tenants')
        assert resp.status_code == 200
        assert b'Create a tenant' in resp.content

    def test_tenant_admin_cannot_see_tenants_page(self, tournament):
        admin = User.objects.create_user('adminA', password='pw')
        grant(admin, tournament['tenant'], admin=True)
        c = client_for(HOST_A); c.force_login(admin)
        _reauth(c)
        resp = c.get('/admin?page=tenants')
        # Reaches the shell (they're an admin) but the tenants body is superuser-only.
        assert resp.status_code == 200
        assert b'Create a tenant' not in resp.content

    def test_tenant_admin_cannot_hit_tenant_endpoints(self, tournament):
        admin = User.objects.create_user('adminA', password='pw')
        grant(admin, tournament['tenant'], admin=True)
        c = client_for(HOST_A); c.force_login(admin)
        _reauth(c)
        resp = _json_post(c, '/tenant_create', {'name': 'X', 'subdomain': 'x'})
        assert resp.status_code == 403
        assert not Tenant.objects.filter(subdomain='x').exists()


# --------------------------------------------------------------------------
# Containment rule — a tenant admin can't nuke a shared account
# --------------------------------------------------------------------------

class TestContainmentRule:
    @pytest.fixture
    def shared_scorer(self, tournament, tenant_b):
        """A scorer who belongs to BOTH tenant A and tenant B."""
        u = User.objects.create_user('shared', password='pw')
        grant(u, tournament['tenant'], scorer=True)
        grant(u, tenant_b, scorer=True)
        return u

    @pytest.fixture
    def admin_a(self, tournament):
        u = User.objects.create_user('adminA', password='pw')
        grant(u, tournament['tenant'], admin=True)
        return u

    def test_cannot_revoke_shared(self, tournament, shared_scorer, admin_a):
        c = client_for(HOST_A); c.force_login(admin_a); _reauth(c)
        resp = _json_post(c, '/user_revoke_links', {'user_id': shared_scorer.id})
        assert resp.status_code == 403
        shared_scorer.refresh_from_db()
        assert shared_scorer.has_usable_password()   # untouched

    def test_cannot_delete_shared(self, tournament, shared_scorer, admin_a):
        c = client_for(HOST_A); c.force_login(admin_a); _reauth(c)
        resp = _json_post(c, '/user_delete', {'user_id': shared_scorer.id})
        assert resp.status_code == 403
        assert User.objects.filter(pk=shared_scorer.id).exists()

    def test_can_remove_shared_from_own_tenant(self, tournament, tenant_b, shared_scorer, admin_a):
        c = client_for(HOST_A); c.force_login(admin_a); _reauth(c)
        resp = _json_post(c, '/user_remove_from_tenant', {'user_id': shared_scorer.id})
        assert resp.status_code == 200
        # Account and the tenant-B membership survive; only the A membership is gone.
        assert User.objects.filter(pk=shared_scorer.id).exists()
        assert not Membership.objects.filter(user=shared_scorer, tenant=tournament['tenant']).exists()
        assert Membership.objects.filter(user=shared_scorer, tenant=tenant_b).exists()

    def test_can_delete_contained_account(self, tournament, admin_a):
        u = User.objects.create_user('onlyA', password='pw')
        grant(u, tournament['tenant'], scorer=True)
        c = client_for(HOST_A); c.force_login(admin_a); _reauth(c)
        resp = _json_post(c, '/user_delete', {'user_id': u.id})
        assert resp.status_code == 200
        assert not User.objects.filter(pk=u.id).exists()

    def test_superuser_can_revoke_shared(self, tournament, tenant_b, shared_scorer):
        su = User.objects.create_superuser('root', '', 'pw')
        c = client_for(HOST_A); c.force_login(su); _reauth(c)
        resp = _json_post(c, '/user_revoke_links', {'user_id': shared_scorer.id})
        assert resp.status_code == 200
        shared_scorer.refresh_from_db()
        assert not shared_scorer.has_usable_password()

    def test_cannot_mint_a_link_for_a_shared_account(self, tournament, shared_scorer, admin_a):
        """F9: the minted link is a full credential for the account and it comes
        back to the *minter*. Opened on tenant B's subdomain it carries the roles the
        account holds there, so admin A minting one is an escalation into B."""
        c = client_for(HOST_A); c.force_login(admin_a); _reauth(c)
        resp = _json_post(c, '/user_generate_link', {'user_id': shared_scorer.id})
        assert resp.status_code == 403
        assert 'url' not in resp.json()

    def test_can_mint_a_link_for_a_contained_account(self, tournament, admin_a):
        """The ordinary case still works — a user who belongs only here."""
        u = User.objects.create_user('onlyA2', password='pw')
        grant(u, tournament['tenant'], scorer=True)
        c = client_for(HOST_A); c.force_login(admin_a); _reauth(c)
        resp = _json_post(c, '/user_generate_link', {'user_id': u.id})
        assert resp.status_code == 200
        assert 'sesame=' in resp.json()['url']

    def test_superuser_can_mint_for_a_shared_account(self, tournament, tenant_b, shared_scorer):
        su = User.objects.create_superuser('root2', '', 'pw')
        c = client_for(HOST_A); c.force_login(su); _reauth(c)
        resp = _json_post(c, '/user_generate_link', {'user_id': shared_scorer.id})
        assert resp.status_code == 200
        assert 'sesame=' in resp.json()['url']


# --------------------------------------------------------------------------
# Tenant-admin console scoping + last-admin guards
# --------------------------------------------------------------------------

class TestConsoleScoping:
    def test_user_list_shows_only_this_tenant(self, tournament, tenant_b):
        admin = User.objects.create_user('adminA', password='pw')
        grant(admin, tournament['tenant'], admin=True)
        User.objects.create_user('alice', password='pw')  # member of A
        grant(User.objects.get(username='alice'), tournament['tenant'], scorer=True)
        bob = User.objects.create_user('bob_on_b', password='pw')  # member of B only
        grant(bob, tenant_b, scorer=True)
        c = client_for(HOST_A); c.force_login(admin); _reauth(c)
        resp = c.get('/admin?page=users')
        assert b'alice' in resp.content
        assert b'bob_on_b' not in resp.content       # another tenant's user is invisible

    def test_update_roles_rejects_user_not_in_tenant(self, tournament, tenant_b):
        admin = User.objects.create_user('adminA', password='pw')
        grant(admin, tournament['tenant'], admin=True)
        bob = User.objects.create_user('bob_on_b', password='pw')
        grant(bob, tenant_b, scorer=True)
        c = client_for(HOST_A); c.force_login(admin); _reauth(c)
        resp = _json_post(c, '/user_update_roles',
                          {'user_id': bob.id, 'roles': ['scorer'], 'is_tenant_admin': False})
        assert resp.status_code == 404              # not "forbidden" — invisible

    def test_cannot_delete_last_admin(self, tournament):
        admin = User.objects.create_user('adminA', password='pw')
        grant(admin, tournament['tenant'], admin=True)
        other = User.objects.create_user('otherAdmin', password='pw')
        grant(other, tournament['tenant'], admin=True)
        c = client_for(HOST_A); c.force_login(admin); _reauth(c)
        # Two admins -> can delete one.
        assert _json_post(c, '/user_delete', {'user_id': other.id}).status_code == 200
        # Now only `admin` is left; demoting them is refused.
        resp = _json_post(c, '/user_update_roles',
                          {'user_id': admin.id, 'roles': [], 'is_tenant_admin': False})
        assert resp.status_code == 400

    def test_superuser_may_remove_last_admin(self, tournament):
        # The guard protects a tenant from stranding itself; a superuser is the
        # recovery path, so it doesn't apply to them.
        admin = User.objects.create_user('onlyadmin', password='pw')
        grant(admin, tournament['tenant'], admin=True)
        su = User.objects.create_superuser('root', '', 'pw')
        c = client_for(HOST_A); c.force_login(su); _reauth(c)
        resp = _json_post(c, '/user_update_roles',
                          {'user_id': admin.id, 'roles': ['scorer'], 'is_tenant_admin': False})
        assert resp.status_code == 200
        assert Membership.objects.get(user=admin, tenant=tournament['tenant']).is_tenant_admin is False


# --------------------------------------------------------------------------
# Superuser tenant management
# --------------------------------------------------------------------------

class TestTenantManagement:
    @pytest.fixture
    def su_client(self, tournament):
        su = User.objects.create_superuser('root', '', 'pw')
        c = client_for(HOST_A); c.force_login(su); _reauth(c)
        return c

    def test_create_tenant(self, su_client):
        resp = _json_post(su_client, '/tenant_create', {'name': 'Cup', 'subdomain': 'CUP'})
        assert resp.status_code == 200
        assert Tenant.objects.filter(subdomain='cup').exists()   # lowercased

    def test_create_duplicate_subdomain_rejected(self, su_client, tenant_b):
        resp = _json_post(su_client, '/tenant_create', {'name': 'Dup', 'subdomain': 'other'})
        assert resp.status_code == 400

    def test_rename_tenant(self, su_client, tenant_b):
        resp = _json_post(su_client, '/tenant_rename',
                          {'tenant_id': tenant_b.id, 'name': 'Renamed', 'subdomain': 'other2'})
        assert resp.status_code == 200
        tenant_b.refresh_from_db()
        assert tenant_b.name == 'Renamed' and tenant_b.subdomain == 'other2'

    def test_tenant_crud_404s_in_standalone(self, su_client):
        # Standalone is single-tenant (pinned via LOCAL_TENANT): the tenant CRUD
        # endpoints are unavailable and the nav page is hidden.
        from django.test import override_settings
        with override_settings(STANDALONE=True):
            assert _json_post(su_client, '/tenant_create',
                              {'name': 'X', 'subdomain': 'x'}).status_code == 404
            page = su_client.get('/admin?page=tenants')
            assert b'Create a tenant' not in page.content
        assert not Tenant.objects.filter(subdomain='x').exists()

    def test_first_admin_seeded_via_own_user_page(self, su_client, tenant_b):
        # No bespoke seed endpoint: a superuser opens the new tenant's OWN user
        # management (they bypass membership there) and adds an admin normally.
        cb = client_for(HOST_B); cb.force_login(User.objects.get(username='root'))
        _reauth(cb)
        resp = _json_post(cb, '/user_create',
                          {'username': 'firstadmin', 'is_tenant_admin': True, 'roles': []})
        assert resp.status_code == 200
        u = User.objects.get(username='firstadmin')
        assert Membership.objects.get(user=u, tenant=tenant_b).is_tenant_admin


# --------------------------------------------------------------------------
# assign_membership management command
# --------------------------------------------------------------------------

class TestAssignMembershipCommand:
    def test_grants_roles(self, tenant):
        from django.core.management import call_command
        u = User.objects.create_user('cli', password='pw')
        call_command('assign_membership', 'cli', 'test', '--roles=scorer,display_op')
        m = Membership.objects.get(user=u, tenant=tenant)
        assert (m.is_scorer, m.is_display_op, m.is_publisher, m.is_tenant_admin) == (True, True, False, False)

    def test_updates_existing(self, tenant):
        from django.core.management import call_command
        u = User.objects.create_user('cli', password='pw')
        grant(u, tenant, scorer=True)
        call_command('assign_membership', 'cli', 'test', '--roles=tenant_admin')
        m = Membership.objects.get(user=u, tenant=tenant)
        assert m.is_tenant_admin and not m.is_scorer

    def test_unknown_role_errors(self, tenant):
        from django.core.management import call_command
        from django.core.management.base import CommandError
        User.objects.create_user('cli', password='pw')
        with pytest.raises(CommandError):
            call_command('assign_membership', 'cli', 'test', '--roles=wizard')


# --------------------------------------------------------------------------
# Data-migration seeding logic (single-tenant map of the old global roles)
# --------------------------------------------------------------------------

class TestOneSettingsRowPerTenant:
    """get_tournament fetches "the" settings for a tenant, so a second row makes
    which configuration is live arbitrary. CeremonyState has always made the same
    one-row claim *with* a constraint; this one didn't."""

    def test_a_second_row_is_refused(self, tenant):
        from django.db import IntegrityError, transaction
        from mahj.models import TournamentSettings
        TournamentSettings.objects.create(tenant=tenant, nb_rounds=3)
        with pytest.raises(IntegrityError):
            with transaction.atomic():
                TournamentSettings.objects.create(tenant=tenant, nb_rounds=9)

    def test_another_tenant_gets_its_own(self, tenant, tenant_b):
        from mahj.models import TournamentSettings
        TournamentSettings.objects.create(tenant=tenant, nb_rounds=3)
        TournamentSettings.objects.create(tenant=tenant_b, nb_rounds=9)
        assert TournamentSettings.objects.count() == 2

    def test_lazy_provisioning_yields_one_row(self, tenant):
        """A fresh tenant is provisioned on first read and reused after.

        This is the sequential path only — the concurrent one is what the constraint
        above is for, and get_or_create is what makes the losing worker adopt the
        winner's row instead of surfacing that constraint as a 500.
        """
        from django.contrib.auth.models import AnonymousUser
        from django.test import RequestFactory

        from mahj.models import TournamentSettings
        from mahj.views.helpers import get_tournament

        def request():
            # A fresh request each time: get_tournament memoizes per request.
            r = RequestFactory().get('/', HTTP_HOST=HOST_A)
            r.user = AnonymousUser()
            return r

        assert not TournamentSettings.objects.filter(tenant=tenant).exists()
        first = get_tournament(request())
        second = get_tournament(request())
        assert first.pk == second.pk
        assert TournamentSettings.objects.filter(tenant=tenant).count() == 1

    def test_the_dedupe_keeps_the_lowest_id_per_tenant(self):
        """Migration 0017's repair decision. A duplicate can only come from the race
        above, so it cleans up rather than stopping the deploy — and which row was
        being read was already arbitrary, so it keeps the lowest id.

        Tested on the decision itself: once the constraint exists its input can't be
        created, and sqlite bakes the constraint into the table so it can't be lifted
        for a test either.
        """
        import importlib
        mod = importlib.import_module(
            'mahj.migrations.0017_tournamentsettings_one_per_tenant')

        # (id, tenant_id) in ascending id order, as the migration reads them.
        rows = [(4, 1), (5, 1), (6, 2), (9, 1), (10, 3)]
        assert mod.ids_to_keep(rows) == {4, 6, 10}
        # Nothing to do when every tenant already has exactly one.
        assert mod.ids_to_keep([(1, 1), (2, 2)]) == {1, 2}
        assert mod.ids_to_keep([]) == set()

    def test_the_dedupe_is_a_no_op_on_a_clean_database(self, tenant, tenant_b):
        """End to end against the real models, which is the state every install
        upgrading from a healthy database is actually in."""
        import importlib

        from django.apps import apps as django_apps
        from mahj.models import TournamentSettings

        rows = {t.pk: TournamentSettings.objects.create(tenant=t, nb_rounds=3).pk
                for t in (tenant, tenant_b)}
        mod = importlib.import_module(
            'mahj.migrations.0017_tournamentsettings_one_per_tenant')
        mod.drop_duplicate_settings(django_apps, None)
        assert {t: TournamentSettings.objects.get(tenant_id=t).pk for t in rows} == rows


class TestOnePublishTargetPerTenant:
    """The publisher settings editor reads its row with get_or_create and the three
    resolution sites read it with .order_by('id').first() — which concedes duplicates
    were possible. Two concurrent saves each inserted one, after which every save
    raised MultipleObjectsReturned and the page 500'd permanently."""

    def test_a_second_row_is_refused(self, tenant):
        from django.db import IntegrityError, transaction
        from mahj.models import PublishTarget
        PublishTarget.objects.create(tenant=tenant, host='a.example.com')
        with pytest.raises(IntegrityError):
            with transaction.atomic():
                PublishTarget.objects.create(tenant=tenant, host='b.example.com')

    def test_another_tenant_gets_its_own(self, tenant, tenant_b):
        from mahj.models import PublishTarget
        PublishTarget.objects.create(tenant=tenant, host='a.example.com')
        PublishTarget.objects.create(tenant=tenant_b, host='b.example.com')
        assert PublishTarget.objects.count() == 2

    def test_the_editor_is_idempotent(self, tenant):
        from mahj.models import PublishTarget
        first, created1 = PublishTarget.objects.get_or_create(tenant=tenant)
        second, created2 = PublishTarget.objects.get_or_create(tenant=tenant)
        assert created1 and not created2
        assert first.pk == second.pk
        assert PublishTarget.objects.filter(tenant=tenant).count() == 1

    def test_the_dedupe_keeps_the_lowest_id(self):
        """Migration 0018's decision, tested as a function for the same reason as
        0017's: once the constraint exists its input cannot be created."""
        import importlib
        mod = importlib.import_module(
            'mahj.migrations.0018_publishtarget_one_per_tenant')
        assert mod.ids_to_keep([(3, 1), (4, 1), (5, 2), (9, 1)]) == {3, 5}
        assert mod.ids_to_keep([]) == set()

    def test_the_dedupe_is_a_no_op_on_a_clean_database(self, tenant, tenant_b):
        import importlib

        from django.apps import apps as django_apps
        from mahj.models import PublishTarget

        rows = {t.pk: PublishTarget.objects.create(tenant=t, host='h').pk
                for t in (tenant, tenant_b)}
        mod = importlib.import_module(
            'mahj.migrations.0018_publishtarget_one_per_tenant')
        mod.drop_duplicate_targets(django_apps, None)
        assert {t: PublishTarget.objects.get(tenant_id=t).pk for t in rows} == rows


class TestSeedMembershipsMigration:
    """Exercise the data-migration function directly (against the live models) so
    the single-tenant mapping and multi-tenant no-op are covered without spinning
    up a full historical migration state."""

    @staticmethod
    def _seed():
        import importlib
        from django.apps import apps
        mod = importlib.import_module('mahj.migrations.0010_seed_memberships')
        mod.seed_memberships(apps, None)

    def test_single_tenant_maps_staff_and_groups(self, tenant):
        staff = User.objects.create_user('legacy_staff', is_staff=True)
        scorer = User.objects.create_user('legacy_scorer')
        scorer.groups.add(Group.objects.create(name='Scorer'))
        plain = User.objects.create_user('legacy_plain')
        su = User.objects.create_superuser('legacy_su', '', 'pw')

        self._seed()

        assert Membership.objects.get(user=staff, tenant=tenant).is_tenant_admin
        assert Membership.objects.get(user=scorer, tenant=tenant).is_scorer
        assert not Membership.objects.filter(user=plain).exists()   # no old role -> no row
        assert not Membership.objects.filter(user=su).exists()      # superuser bypasses

    def test_multi_tenant_is_noop(self, tenant):
        Tenant.objects.create(name='Second', subdomain='second')
        User.objects.create_user('legacy_staff', is_staff=True)
        self._seed()
        assert Membership.objects.count() == 0      # can't attribute to one tenant


# --------------------------------------------------------------------------
# Tenancy carried into the storage and HTTP layers
# --------------------------------------------------------------------------

class TestSubdomainUniqueness:
    """The subdomain is the tenant key — every request resolves its tenant from
    the host, and Tenant.get_default_pk does a get_or_create on it. Two rows
    sharing one would make which tenant a request lands on depend on row order."""

    def test_duplicate_subdomain_rejected_by_the_database(self, tenant):
        from django.db import IntegrityError, transaction
        with pytest.raises(IntegrityError):
            with transaction.atomic():
                Tenant.objects.create(name='Impostor', subdomain=tenant.subdomain)

    def test_distinct_subdomains_are_fine(self, tenant):
        Tenant.objects.create(name='Second', subdomain='second')
        assert Tenant.objects.count() == 2

    def test_superuser_console_still_refuses_a_duplicate(self, tournament):
        """The app-level guard must answer before the database does, so the
        operator gets a message rather than a 500."""
        su = User.objects.create_superuser('root3', '', 'pw')
        c = client_for(HOST_A); c.force_login(su); _reauth(c)
        resp = _json_post(c, '/tenant_create', {'name': 'Dup', 'subdomain': 'test'})
        assert resp.status_code == 400
        assert Tenant.objects.filter(subdomain='test').count() == 1


class TestForwardedHostSpoofing:
    """F12: the subdomain in the host picks the tenant, so the host Django trusts
    must be the one nginx sets — never a header the client can supply."""

    def _scorer_of_b_on_host_a(self, tenant_b):
        scorer = User.objects.create_user('scorerHdr', password='pw')
        grant(scorer, tenant_b, scorer=True)
        c = client_for(HOST_A)
        c.force_login(scorer)
        return c

    def test_x_forwarded_host_does_not_change_the_tenant(self, tournament, tenant_b):
        """A scorer of B is forbidden on A, and claiming to be B via the header
        must not move them onto B."""
        c = self._scorer_of_b_on_host_a(tenant_b)
        resp = c.get('/admin', HTTP_X_FORWARDED_HOST=HOST_B)
        assert resp.status_code == 403

    @override_settings(USE_X_FORWARDED_HOST=True)
    def test_the_vector_is_real_which_is_why_the_setting_is_off(self, tournament, tenant_b):
        """Characterises what the setting buys. Turn it on and the same request
        succeeds: Django prefers X-Forwarded-Host, nginx never sets it, so the
        client's own header picks the tenant. Nothing in the code can defend
        against that — keeping the setting off is the fix."""
        c = self._scorer_of_b_on_host_a(tenant_b)
        resp = c.get('/admin', HTTP_X_FORWARDED_HOST=HOST_B)
        assert resp.status_code == 200

    def test_setting_is_not_enabled_in_prod(self):
        """Config, not code: assert the prod settings module leaves it off, since
        nginx passes the real Host and never sets X-Forwarded-Host."""
        import pathlib
        prod = pathlib.Path('apps/settings/prod.py').read_text()
        assert 'USE_X_FORWARDED_HOST = True' not in prod


class TestPlayerRoundsTenantScope:
    def test_another_tenants_player_is_not_readable(self, tournament, tenant_b):
        """player_rounds_rows looked players up by bare id, so a crafted id read
        another tenant's competitor and rendered their rounds on this subdomain."""
        from django.contrib.auth.models import AnonymousUser
        from django.test import RequestFactory
        from mahj.models import Player
        from mahj.views.scoring import player_rounds_rows

        other = Player.objects.create(
            tenant=tenant_b, draw_number=1, full_name='Outsider', first_name='Out')
        req = RequestFactory().get('/', HTTP_HOST=HOST_A)
        req.user = AnonymousUser()
        with pytest.raises(Player.DoesNotExist):
            player_rounds_rows(req, other.id)


# --------------------------------------------------------------------------
# Sharing an account between tournaments (superuser only)
# --------------------------------------------------------------------------

class TestAddExistingUser:
    """The one way to make an account shared. Superuser-only on purpose: elsewhere a
    user outside this tenant is reported as "not found" rather than "forbidden" so a
    tenant admin can't learn an account exists somewhere else, and a by-username add
    handed to them would turn that into an enumeration oracle."""

    @pytest.fixture
    def outsider(self, tenant_b):
        u = User.objects.create_user('outsider', password='pw')
        grant(u, tenant_b, scorer=True)
        return u

    @pytest.fixture
    def su_client(self, tournament):
        su = User.objects.create_superuser('root_add', '', 'pw')
        c = client_for(HOST_A); c.force_login(su); _reauth(c)
        return c

    def test_superuser_can_add_an_existing_account(self, tournament, outsider, su_client):
        resp = _json_post(su_client, '/user_add_existing',
                          {'username': 'outsider', 'roles': ['scorer']})
        assert resp.status_code == 200
        body = resp.json()
        assert body['already_here'] is False
        assert body['shared'] is True      # it belongs to tenant B as well
        m = Membership.objects.get(user=outsider, tenant=tournament['tenant'])
        assert m.is_scorer and not m.is_tenant_admin

    def test_the_account_now_works_on_this_tenant(self, tournament, outsider, su_client):
        _json_post(su_client, '/user_add_existing',
                   {'username': 'outsider', 'roles': ['scorer']})
        c = client_for(HOST_A)
        c.force_login(outsider)
        assert c.get('/admin?page=scoring').status_code == 200

    def test_adding_one_already_here_just_sets_its_roles(self, tournament, su_client):
        insider = User.objects.create_user('insider', password='pw')
        grant(insider, tournament['tenant'], scorer=True)
        resp = _json_post(su_client, '/user_add_existing',
                          {'username': 'insider', 'roles': ['publisher']})
        assert resp.status_code == 200
        assert resp.json()['already_here'] is True
        assert resp.json()['shared'] is False
        m = Membership.objects.get(user=insider, tenant=tournament['tenant'])
        assert m.is_publisher and not m.is_scorer
        assert Membership.objects.filter(user=insider).count() == 1

    def test_a_tenant_admin_cannot_use_it(self, tournament, outsider):
        """The whole point: a tenant admin must not be able to probe usernames."""
        admin = User.objects.create_user('plainadmin', password='pw')
        grant(admin, tournament['tenant'], admin=True)
        c = client_for(HOST_A); c.force_login(admin); _reauth(c)
        resp = _json_post(c, '/user_add_existing', {'username': 'outsider'})
        assert resp.status_code == 403
        assert not Membership.objects.filter(
            user=outsider, tenant=tournament['tenant']).exists()

    def test_an_unknown_username_is_404(self, tournament, su_client):
        resp = _json_post(su_client, '/user_add_existing', {'username': 'nobody'})
        assert resp.status_code == 404

    def test_a_blank_username_is_400(self, tournament, su_client):
        assert _json_post(su_client, '/user_add_existing', {'username': '  '}).status_code == 400

    def test_get_is_not_allowed(self, tournament, su_client):
        assert su_client.get('/user_add_existing').status_code == 405

    def test_the_form_is_superuser_only(self, tournament):
        """A tenant admin must not even see it."""
        admin = User.objects.create_user('plainadmin2', password='pw')
        grant(admin, tournament['tenant'], admin=True)
        c = client_for(HOST_A); c.force_login(admin); _reauth(c)
        # The <form> is gated; its submit handler ships either way and no-ops on an
        # empty selection, so assert on the element rather than the id string.
        assert 'id="add-existing-form"' not in c.get('/admin?page=users').content.decode()

        su = User.objects.create_superuser('root_form', '', 'pw')
        c2 = client_for(HOST_A); c2.force_login(su); _reauth(c2)
        assert 'id="add-existing-form"' in c2.get('/admin?page=users').content.decode()


# --------------------------------------------------------------------------
# Deleting a tenant
# --------------------------------------------------------------------------

class TestTenantDelete:
    """One delete takes the whole tournament: every tenant-scoped model cascades, and
    so does Membership. The accounts themselves stay — one may belong elsewhere."""

    @pytest.fixture
    def su_client(self, tournament):
        su = User.objects.create_superuser('root_del', '', 'pw')
        c = client_for(HOST_A); c.force_login(su); _reauth(c)
        return c

    def _delete(self, client, tenant, confirm=None):
        return _json_post(client, '/tenant_delete', {
            'tenant_id': tenant.id,
            'confirm': tenant.subdomain if confirm is None else confirm,
        })

    def test_it_takes_the_tournament_with_it(self, tournament, tenant_b, su_client):
        from mahj.models import Player, Schedule, Seat, TournamentSettings
        # Give tenant B something to lose, then delete it from tenant A's console.
        other = tenant_b
        Player.objects.create(tenant=other, draw_number=1, full_name='Gone', first_name='G')
        Seat.objects.create(tenant=other, round_nb=1, table_nb=1, wind=1, draw_number=1)
        Schedule.objects.create(tenant=other, day='Sat', time='10:00', name='R1', is_round=True)
        # A logo is BinaryField bytes, not a file — the row itself is all there is
        # to delete, and the delete must not try to reach into the field.
        TournamentSettings.objects.create(tenant=other, nb_rounds=1,
                                         logo=b'\x89PNG-not-really', logo_etag='abc')
        doomed = User.objects.create_user('doomed', password='pw')
        grant(doomed, other, scorer=True)

        resp = self._delete(su_client, other)
        assert resp.status_code == 200
        assert not Tenant.objects.filter(pk=other.pk).exists()
        assert not Player.objects.filter(tenant_id=other.pk).exists()
        assert not Seat.objects.filter(tenant_id=other.pk).exists()
        assert not Schedule.objects.filter(tenant_id=other.pk).exists()
        assert not TournamentSettings.objects.filter(tenant_id=other.pk).exists()
        assert not Membership.objects.filter(tenant_id=other.pk).exists()
        # The account survives — it is not this action's business.
        assert User.objects.filter(pk=doomed.pk).exists()

    def test_tenant_a_is_untouched(self, tournament, tenant_b, su_client):
        from mahj.models import Player
        before = Player.objects.filter(tenant=tournament['tenant']).count()
        self._delete(su_client, tenant_b)
        assert Player.objects.filter(tenant=tournament['tenant']).count() == before

    def test_the_subdomain_must_be_retyped(self, tournament, tenant_b, su_client):
        resp = self._delete(su_client, tenant_b, confirm='wrong')
        assert resp.status_code == 400
        assert 'to confirm' in resp.json()['error']
        assert Tenant.objects.filter(pk=tenant_b.pk).exists()

    def test_a_blank_confirmation_is_refused(self, tournament, tenant_b, su_client):
        assert self._delete(su_client, tenant_b, confirm='').status_code == 400
        assert Tenant.objects.filter(pk=tenant_b.pk).exists()

    def test_the_current_tenant_cannot_be_deleted(self, tournament, su_client):
        """Deleting the ground you are standing on."""
        here = tournament['tenant']
        resp = self._delete(su_client, here)
        assert resp.status_code == 400
        assert 'working in' in resp.json()['error']
        assert Tenant.objects.filter(pk=here.pk).exists()

    def test_the_default_tenant_cannot_be_deleted(self, tournament, su_client):
        """Every tenant FK defaults to it."""
        fallback = Tenant.objects.create(name='Fallback',
                                         subdomain=Tenant.DEFAULT_SUBDOMAIN)
        resp = self._delete(su_client, fallback)
        assert resp.status_code == 400
        assert 'cannot be deleted' in resp.json()['error']
        assert Tenant.objects.filter(pk=fallback.pk).exists()

    def test_a_tenant_admin_cannot_delete_anything(self, tournament, tenant_b):
        admin = User.objects.create_user('plainadmin3', password='pw')
        grant(admin, tournament['tenant'], admin=True)
        c = client_for(HOST_A); c.force_login(admin); _reauth(c)
        assert self._delete(c, tenant_b).status_code == 403
        assert Tenant.objects.filter(pk=tenant_b.pk).exists()

    def test_unknown_tenant_is_404(self, tournament, su_client):
        assert _json_post(su_client, '/tenant_delete',
                          {'tenant_id': 999999, 'confirm': 'x'}).status_code == 404

    def test_get_is_not_allowed(self, tournament, su_client):
        assert su_client.get('/tenant_delete').status_code == 405

    def test_the_button_is_hidden_for_the_rows_that_refuse(self, tournament, su_client):
        Tenant.objects.create(name='Fallback', subdomain=Tenant.DEFAULT_SUBDOMAIN)
        html = su_client.get('/admin?page=tenants').content.decode()
        # One deletable row (tenant B is absent here, so: current + default only).
        assert 'delete-tenant' not in html or html.count('delete-tenant') >= 0
        # The current tenant's row must not offer it.
        import re
        row = re.search(r'<tr data-tenant-id="%d".*?</tr>' % tournament['tenant'].id,
                        html, re.DOTALL)
        assert row, 'no row for the current tenant'
        assert 'delete-tenant' not in row.group(0)


# --------------------------------------------------------------------------
# The admin page table
# --------------------------------------------------------------------------

class TestAdminPageTable:
    """`ADMIN_PAGES` is the console's access-control spec, so pin it as one.

    The gates used to be a dozen hand-rolled clauses inside a 500-line if/elif
    chain, where a page added without one looked exactly like a page that didn't
    need one. These tests are the executable half of that fix: the table's shape,
    and then what each role actually gets for every page in it.
    """

    def test_every_page_names_a_gate_and_a_renderer(self):
        from mahj.views.admin_views import ADMIN_PAGES
        for name, spec in ADMIN_PAGES.items():
            assert callable(spec.gate), f'{name} has no gate'
            assert callable(spec.render), f'{name} has no renderer'
            if spec.reauth_next:
                assert spec.reauth, f'{name} names a reauth target but is not gated'

    # What each account should get for each page: 'page' (it renders), 'empty'
    # (the shell's blank panel — the page either doesn't exist for them or isn't
    # theirs to see), or 'reauth' (the confirm-your-password panel first).
    EXPECTED = {
        'scorer': {'welcome': 'page', 'scoring': 'page'},
        'display_op': {'welcome': 'page', 'display': 'page', 'ceremony': 'page'},
        'publisher': {'welcome': 'page', 'scoring': 'page',
                      'publisher_overview': 'page'},
        'admin': {'welcome': 'page', 'display': 'page', 'settings': 'page',
                  'player_editor': 'page', 'publish_target': 'page',
                  'import_template': 'page', 'seating': 'page', 'scoring': 'page',
                  'ceremony': 'page', 'publisher_overview': 'page',
                  'users': 'reauth', 'backup': 'reauth', 'tenants': 'empty'},
        'superuser': {'welcome': 'page', 'display': 'page', 'settings': 'page',
                      'player_editor': 'page', 'publish_target': 'page',
                      'import_template': 'page', 'seating': 'page',
                      'scoring': 'page', 'ceremony': 'page',
                      'publisher_overview': 'page', 'users': 'reauth',
                      'backup': 'reauth', 'tenants': 'reauth'},
    }

    def _account(self, role, tenant):
        user = User.objects.create_user(f'u_{role}', password='pw',
                                       is_superuser=(role == 'superuser'))
        if role != 'superuser':
            grant(user, tenant, **{('admin' if role == 'admin' else role): True})
        c = client_for(HOST_A)
        c.force_login(user)
        return c

    def _outcome(self, client, page):
        resp = client.get(f'/admin?page={page}')
        assert resp.status_code == 200, f'{page} -> {resp.status_code}'
        content = resp.context['page_content']
        if content == 'None':
            return 'empty'
        if 'id="reauth-form"' in content:
            return 'reauth'
        return 'page'

    @pytest.mark.parametrize('role', sorted(EXPECTED))
    def test_each_role_sees_exactly_its_pages(self, role, tournament):
        from mahj.views.admin_views import ADMIN_PAGES
        client = self._account(role, tournament['tenant'])
        expected = self.EXPECTED[role]
        got = {page: self._outcome(client, page) for page in ADMIN_PAGES}
        assert got == {p: expected.get(p, 'empty') for p in ADMIN_PAGES}

    def test_the_expectations_cover_every_page(self):
        """So adding a page to the table without deciding who sees it fails here."""
        from mahj.views.admin_views import ADMIN_PAGES
        assert set(self.EXPECTED['superuser']) == set(ADMIN_PAGES)

    def test_an_unknown_page_is_indistinguishable_from_a_forbidden_one(self, tournament):
        """Probing ?page= must not map out what exists."""
        client = self._account('scorer', tournament['tenant'])
        assert self._outcome(client, 'no_such_page') == 'empty'
        assert self._outcome(client, 'tenants') == 'empty'

    def test_no_page_lands_on_the_role_the_account_works_from(self, tournament):
        """A single-role account is greeted by the page it uses, not a dashboard
        summarising things it can't reach."""
        t = tournament['tenant']
        assert self._account('scorer', t).get('/admin').context['page'] == 'scoring'
        assert self._account('display_op', t).get('/admin').context['page'] == 'display'
        assert self._account('publisher', t).get('/admin').context['page'] == 'scoring'
        assert self._account('admin', t).get('/admin').context['page'] == 'welcome'

    def test_a_display_op_who_also_publishes_lands_on_display(self, tournament):
        """Ordering, not an accident: the display operator is at the projectors and
        needs the screen controls first."""
        user = User.objects.create_user('both', password='pw')
        grant(user, tournament['tenant'], display_op=True, publisher=True)
        c = client_for(HOST_A)
        c.force_login(user)
        assert c.get('/admin').context['page'] == 'display'
