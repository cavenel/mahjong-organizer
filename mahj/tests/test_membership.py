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
