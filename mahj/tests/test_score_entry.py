"""Optimistic-locking tests for update_hand_points.

Simulates two scorers editing the same Hand: first write wins, second sees a 409
with the current state so the client can rebase and retry.
"""
import json

import pytest
from django.contrib.auth.models import User
from django.test import Client

from mahj.models import Hand, Position, PublishedRound


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


class TestCreateHandPoints:
    """Bulk save must keep the optimistic-lock version monotonic so it can't
    silently clobber a concurrent per-cell update_hand_points (I-C)."""

    def _bulk(self, client, round_nb=1, table_nb=1):
        data = {'round_nb': round_nb, 'table_nb': table_nb}
        for i in range(1, 17):
            data[f'pts_{i}'] = 8
            data[f'by_{i}'] = 1
            data[f'from_{i}'] = 2
        return client.post('/create_hand_points', data)

    def test_bumps_version_instead_of_rewinding(self, authed_client, hand):
        # Drive the hand's version up via the per-cell path, as a live scorer would.
        _post(authed_client, hand, version=0)
        _post(authed_client, hand, version=1)
        hand.refresh_from_db()
        assert hand.version == 2

        # A bulk save must advance the version, never write a stale value back.
        resp = self._bulk(authed_client)
        assert resp.status_code == 200
        hand.refresh_from_db()
        assert hand.version == 3
        assert hand.pts == 8

    def test_writes_all_sixteen_hands(self, authed_client, tournament):
        resp = self._bulk(authed_client, round_nb=3, table_nb=2)  # unplayed in fixture
        assert resp.status_code == 200
        written = Hand.objects.filter(
            tenant=tournament['tenant'], round_nb=3, table_nb=2, pts=8,
        ).exclude(hand_nb=17).count()
        assert written == 16


class TestUpdatePositionPenalty:
    """Per-player penalty: an integer minipoint adjustment saved from the score
    sheet. It is a score-sheet-only figure (the leaderboard never reads it) and,
    like the other position edits, is rejected on a published (locked) round."""

    def _pos(self, tournament, round_nb=3, table_nb=1):
        # Round 3 is not published in the fixture, so it stays editable.
        return Position.objects.filter(
            tenant=tournament['tenant'], round_nb=round_nb, table_nb=table_nb,
        ).order_by('position').first()

    def test_sets_penalty(self, authed_client, tournament):
        pos = self._pos(tournament)
        resp = authed_client.post('/update_position_penalty', {'id': pos.id, 'penalty': -10})
        assert resp.status_code == 200
        assert json.loads(resp.content)['status'] == 'ok'
        pos.refresh_from_db()
        assert pos.penalty == -10

    def test_invalid_penalty_defaults_to_zero(self, authed_client, tournament):
        pos = self._pos(tournament)
        pos.penalty = 5
        pos.save(update_fields=['penalty'])
        resp = authed_client.post('/update_position_penalty', {'id': pos.id, 'penalty': 'abc'})
        assert resp.status_code == 200
        pos.refresh_from_db()
        assert pos.penalty == 0

    def test_penalty_does_not_touch_minipoints(self, authed_client, tournament):
        pos = self._pos(tournament)
        before_mp, before_tp = pos.minipoints, pos.tablepoints
        authed_client.post('/update_position_penalty', {'id': pos.id, 'penalty': -20})
        pos.refresh_from_db()
        assert pos.minipoints == before_mp
        assert pos.tablepoints == before_tp

    def test_rejected_on_published_round(self, authed_client, tournament):
        pos = self._pos(tournament, round_nb=1)  # published in fixture
        resp = authed_client.post('/update_position_penalty', {'id': pos.id, 'penalty': -10})
        assert resp.status_code == 409
        assert json.loads(resp.content)['status'] == 'locked'
        pos.refresh_from_db()
        assert pos.penalty == 0  # write must not have landed

    def test_nonexistent_position_returns_404(self, authed_client):
        resp = authed_client.post('/update_position_penalty', {'id': 999999, 'penalty': -10})
        assert resp.status_code == 404

    def test_score_sheet_renders_penalty_inputs(self, authed_client, tournament):
        pos = self._pos(tournament, round_nb=1, table_nb=1)
        pos.penalty = -10
        pos.save(update_fields=['penalty'])
        body = authed_client.get('/scores_per_hand_1_1').content.decode()
        # One editable penalty box per seat, pre-filled with the saved value.
        assert 'class="penalty-input"' in body
        assert 'id=\'pen_1\'' in body or 'id="pen_1"' in body
        assert 'update_position_penalty' in body  # the persistence endpoint
        assert '-10' in body


class TestPublishedRoundLock:
    """A published round is read-only: scores can only change after it is
    explicitly unpublished. Rounds 1 & 2 are published in the fixture; round 3
    is not."""

    def _row(self, tournament, round_nb, table_nb=1):
        return list(
            Position.objects.filter(
                tenant=tournament['tenant'], round_nb=round_nb, table_nb=table_nb,
            ).order_by('position')
        )

    def _bulk_edit(self, client, positions, mp):
        return client.post(
            '/update_positions_bulk',
            data=json.dumps({'positions': [
                {'id': p.id, 'mp': mp, 'tp': p.tablepoints} for p in positions
            ]}),
            content_type='application/json',
        )

    def test_bulk_edit_rejected_on_published_round(self, authed_client, tournament):
        positions = self._row(tournament, round_nb=1)
        original = positions[0].minipoints

        resp = self._bulk_edit(authed_client, positions, mp=original + 7)
        assert resp.status_code == 409
        assert json.loads(resp.content)['status'] == 'locked'

        positions[0].refresh_from_db()
        assert positions[0].minipoints == original  # write must not have landed

    def test_published_set_unchanged_after_rejected_edit(self, authed_client, tournament):
        before = set(PublishedRound.objects.filter(tenant=tournament['tenant'])
                     .values_list('round_nb', flat=True))
        self._bulk_edit(authed_client, self._row(tournament, round_nb=1), mp=1)
        after = set(PublishedRound.objects.filter(tenant=tournament['tenant'])
                    .values_list('round_nb', flat=True))
        assert before == after  # no silent unpublish

    def test_bulk_edit_allowed_on_unpublished_round(self, authed_client, tournament):
        positions = self._row(tournament, round_nb=3)  # not published in fixture
        resp = self._bulk_edit(authed_client, positions, mp=42)
        assert resp.status_code == 200
        positions[0].refresh_from_db()
        assert positions[0].minipoints == 42

    def test_edit_allowed_after_unpublishing(self, authed_client, tournament):
        tenant = tournament['tenant']
        positions = self._row(tournament, round_nb=2, table_nb=1)
        original = positions[0].minipoints

        # Locked while published.
        assert self._bulk_edit(authed_client, positions, mp=original + 3).status_code == 409

        # Unpublish round 2, then the edit is accepted.
        unpub = authed_client.post(
            '/set_round_published',
            data=json.dumps({'round_nb': 2, 'published': False}),
            content_type='application/json',
        )
        assert unpub.status_code == 200
        resp = self._bulk_edit(authed_client, positions, mp=original + 3)
        assert resp.status_code == 200
        positions[0].refresh_from_db()
        assert positions[0].minipoints == original + 3

