"""Optimistic-locking tests for update_hand_points.

Simulates two scorers editing the same Hand: first write wins, second sees a 409
with the current state so the client can rebase and retry.
"""
import json

import pytest
from django.contrib.auth.models import User
from django.test import Client

from mahj.models import Hand


@pytest.fixture
def scorer(db):
    return User.objects.create_user('scorer', password='pw', is_staff=True)


@pytest.fixture
def authed_client(scorer, tournament):
    c = Client()
    c.force_login(scorer)
    # get_tenant() reads the subdomain off the Host header — send one that matches the fixture.
    c.defaults['HTTP_HOST'] = 'test.mahj.ovh'
    return c


@pytest.fixture
def hand(tournament):
    # Pick any hand created by the tournament fixture.
    return Hand.objects.filter(tenant=tournament['tenant'], round_nb=1, table_nb=1, hand_nb=1).first()


def _post(client, hand, version, pts=25, win_by=2, win_from=4):
    return client.post('/update_hand_points', {
        'id': hand.id,
        'version': version,
        'pts': pts,
        'by': win_by,
        'from': win_from,
    })


class TestUpdateHandPoints:
    def test_happy_path_increments_version(self, authed_client, hand):
        initial = hand.version
        resp = _post(authed_client, hand, version=initial, pts=30, win_by=1, win_from=3)
        assert resp.status_code == 200
        body = json.loads(resp.content)
        assert body == {'status': 'ok', 'version': initial + 1}

        hand.refresh_from_db()
        assert hand.version == initial + 1
        assert hand.pts == 30
        assert hand.win_by == 1
        assert hand.win_from == 3

    def test_stale_version_rejected_with_current_state(self, authed_client, hand):
        # Scorer A commits first, bumping version 0 → 1.
        first = _post(authed_client, hand, version=hand.version, pts=10, win_by=1, win_from=2)
        assert first.status_code == 200

        # Scorer B still has the old version — must get 409 with current state echoed back.
        second = _post(authed_client, hand, version=0, pts=999, win_by=3, win_from=4)
        assert second.status_code == 409
        body = json.loads(second.content)
        assert body['status'] == 'conflict'
        assert body['current'] == {
            'pts': 10, 'win_by': 1, 'win_from': 2, 'version': 1,
        }

        hand.refresh_from_db()
        assert hand.pts == 10  # B's write must not have landed

    def test_nonexistent_hand_returns_404(self, authed_client, hand, tournament):
        # Valid tenant but no hand with this id.
        resp = authed_client.post('/update_hand_points', {
            'id': 999999, 'version': 0, 'pts': 1, 'by': 1, 'from': 2,
        })
        assert resp.status_code == 404

    def test_retry_after_rebase_succeeds(self, authed_client, hand):
        # A commits.
        _post(authed_client, hand, version=0, pts=7, win_by=1, win_from=3)
        # B rebases (reads the new version) and retries.
        hand.refresh_from_db()
        resp = _post(authed_client, hand, version=hand.version, pts=77, win_by=2, win_from=4)
        assert resp.status_code == 200

        hand.refresh_from_db()
        assert hand.pts == 77
        assert hand.version == 2

    def test_invalid_pts_defaults_to_zero(self, authed_client, hand):
        resp = authed_client.post('/update_hand_points', {
            'id': hand.id, 'version': hand.version, 'pts': 'abc', 'by': 1, 'from': 2,
        })
        assert resp.status_code == 200
        hand.refresh_from_db()
        assert hand.pts == 0
