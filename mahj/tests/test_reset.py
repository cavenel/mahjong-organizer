"""Factory-reset flow (admin_reset).

The Danger-zone "Reset tournament" button wipes every tenant row and restores the
settings to defaults, leaving a blank instance. Guards that it clears all data and
config, is staff-only, and refuses non-POST.
"""
import pytest
from django.contrib.auth.models import User
from django.test import Client

from mahj.models import (
    CeremonyState, Hand, Player, PublishTarget, PublishedRound, ScoreSheet,
    Schedule, Screen, ScreenMode, Seat, TournamentSettings,
)
from mahj.tests.conftest import grant


@pytest.fixture
def staff_client(tenant):
    c = Client()
    c.defaults['HTTP_HOST'] = 'test.example.com'  # -> subdomain 'test'
    u = User.objects.create_superuser('staff', password='pw')
    c.force_login(u)
    return c


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
    assert resp.status_code == 400


def test_reset_is_staff_only(scorer_client, tournament):
    tenant = tournament['tenant']
    resp = scorer_client.post('/admin_reset')
    # Authenticated but not a tenant admin -> 403 (the gate no longer bounces
    # logged-in users to login), and nothing is wiped.
    assert resp.status_code == 403
    assert Player.objects.filter(tenant=tenant).exists()
