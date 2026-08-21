"""Factory-reset flow (admin_reset).

The Danger-zone "Reset tournament" button wipes every tenant row and restores the
settings to defaults, leaving a blank instance. Guards that it clears all data and
config, is admin-only, refuses non-POST, and requires a recent password re-auth.
"""
import json

import pytest
from django.contrib.auth.models import User
from django.test import Client

from mahj.models import (
    CeremonyState, Hand, Player, PublishTarget, PublishedRound, ScoreSheet,
    Schedule, Screen, ScreenMode, Seat, TournamentSettings,
)
from mahj.tests.conftest import grant


def reauth(client, password='pw'):
    """Stamp the session's "sudo mode" so re-auth-gated endpoints let it through."""
    resp = client.post('/user_reauth', data=json.dumps({'password': password}),
                        content_type='application/json')
    assert resp.status_code == 200, resp.content
    return client


@pytest.fixture
def staff_client(tenant):
    c = Client()
    c.defaults['HTTP_HOST'] = 'test.example.com'  # -> subdomain 'test'
    u = User.objects.create_superuser('staff', password='pw')
    c.force_login(u)
    return reauth(c)


@pytest.fixture
def scorer_client(tenant, django_user_model):
    c = Client()
    c.defaults['HTTP_HOST'] = 'test.example.com'
    u = User.objects.create_user('scorer', password='pw')
    grant(u, tenant, scorer=True)
    c.force_login(u)
    return c


def test_reset_wipes_everything(staff_client, tournament):
    tenant = tournament['tenant']
    # Extras the base fixture doesn't create, so the reset must clear these too.
    Screen.objects.create(tenant=tenant, name='S1', view='black')
    ScreenMode.objects.create(tenant=tenant, name='M1', views=['black'])
    CeremonyState.objects.create(tenant=tenant, phase='teams', step=1)
    PublishTarget.objects.create(tenant=tenant, host='h', username='u')

    resp = staff_client.post('/admin_reset')
    assert resp.status_code == 200

    for model in (Player, Seat, Hand, ScoreSheet, PublishedRound, Schedule,
                  Screen, ScreenMode, CeremonyState, PublishTarget,
                  TournamentSettings):
        assert not model.objects.filter(tenant=tenant).exists(), model.__name__


def test_reset_requires_post(staff_client):
    resp = staff_client.get('/admin_reset')
    assert resp.status_code == 405
    assert resp.json()['error'] == 'POST required'


def test_reset_is_staff_only(scorer_client, tournament):
    tenant = tournament['tenant']
    resp = scorer_client.post('/admin_reset')
    # Authenticated but not a tenant admin -> 403 (the gate no longer bounces
    # logged-in users to login), and nothing is wiped.
    assert resp.status_code == 403
    assert Player.objects.filter(tenant=tenant).exists()


def test_reset_requires_reauth(tenant, tournament):
    """An admin session that hasn't re-confirmed its password can't drive the wipe:
    the destructive endpoint is re-auth gated just like user/tenant management."""
    c = Client()
    c.defaults['HTTP_HOST'] = 'test.example.com'
    u = User.objects.create_superuser('staff', password='pw')
    c.force_login(u)  # logged in as admin, but no /user_reauth yet

    resp = c.post('/admin_reset')
    assert resp.status_code == 403
    assert resp.json()['status'] == 'reauth_required'
    assert Player.objects.filter(tenant=tournament['tenant']).exists()

    # After confirming the password, the same session goes through.
    reauth(c)
    assert c.post('/admin_reset').status_code == 200
