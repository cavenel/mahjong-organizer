import pytest
from django.contrib.auth.models import User
from django.test import Client

from mahj.models import Tenant
from mahj.tests.conftest import grant


@pytest.fixture
def dotted_tenant(db):
    return Tenant.objects.create(name='A under test', subdomain='a.test')


@pytest.fixture
def admin_client(dotted_tenant):
    u = User.objects.create_user('boss', password='pw')
    grant(u, dotted_tenant, admin=True)
    c = Client()
    c.force_login(u)
    c.defaults['HTTP_HOST'] = 'a.test.example.com'
    return c


def test_users_page_dotted(admin_client):
    resp = admin_client.get('/admin', {'page': 'users'})
    assert resp.status_code == 200, resp.content[:2000]


def test_users_page_superuser_no_tenant(db):
    """Superuser hits a subdomain that has no Tenant row (get_tenant -> None).
    Mirrors the live a.test.mahj.ovh 500 if the tenant was never created."""
    su = User.objects.create_superuser('root', password='pw')
    c = Client()
    c.force_login(su)
    c.defaults['HTTP_HOST'] = 'a.test.example.com'
    resp = c.get('/admin', {'page': 'users'})
    assert resp.status_code == 200, resp.content[:3000]
