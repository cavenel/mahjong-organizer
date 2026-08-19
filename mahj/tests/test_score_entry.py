"""Optimistic-locking tests for update_hand_points.

Simulates two scorers editing the same Hand: first write wins, second sees a 409
with the current state so the client can rebase and retry.
"""
import json

import pytest
from django.contrib.auth.models import AnonymousUser, User
from django.test import Client, RequestFactory

from mahj.models import Hand, Player, PublishedRound, Seat
from mahj.tests.conftest import grant
from mahj.views.scoring import scores_per_player_rows


def _public_request():
    """An anonymous request on the fixture's host, for reading the standings the
    way a spectator does (so the publish gate is the thing under test)."""
    req = RequestFactory().get('/', HTTP_HOST='test.example.com')
    req.user = AnonymousUser()
    return req


@pytest.fixture
def scorer(tournament):
    u = User.objects.create_user('scorer', password='pw')
    grant(u, tournament['tenant'], admin=True)
    return u


@pytest.fixture
def authed_client(scorer, tournament):
    c = Client()
    c.force_login(scorer)
    # get_tenant() reads the subdomain off the Host header — send one that matches the fixture.
    c.defaults['HTTP_HOST'] = 'test.example.com'
    return c


@pytest.fixture
def hand(tournament):
    # Pick any hand created by the tournament fixture.
    return Hand.objects.filter(tenant=tournament['tenant'], round_nb=1, table_nb=1, hand_nb=1).first()


def _post(client, hand, version, pts=25, win_by=2, win_from=4):
    return client.post('/update_hand_points', {
        'id': hand.id,
        'version': version,
        'points': pts,
        'by': win_by,
        'from': win_from,
    })


class TestUpdateHandPoints:
    def test_happy_path_increments_version(self, authed_client, hand):
        initial = hand.version
        resp = _post(authed_client, hand, version=initial, pts=30, win_by=1, win_from=3)
        assert resp.status_code == 200
        body = json.loads(resp.content)
        assert body['status'] == 'ok'
        assert body['version'] == initial + 1
        # The response echoes what was stored, so the cell can show the server's
        # version of a lossy entry rather than what the scorer typed.
        assert body['stored'] == {'points': 30, 'win_by': 1, 'win_from': 3}

        hand.refresh_from_db()
        assert hand.version == initial + 1
        assert hand.points == 30
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
            'points': 10, 'win_by': 1, 'win_from': 2, 'version': 1,
        }

        hand.refresh_from_db()
        assert hand.points == 10  # B's write must not have landed

    def test_nonexistent_hand_returns_404(self, authed_client, hand, tournament):
        # Valid tenant but no hand with this id.
        resp = authed_client.post('/update_hand_points', {
            'id': 999999, 'version': 0, 'points': 1, 'by': 1, 'from': 2,
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
        assert hand.points == 77
        assert hand.version == 2

    def test_unparseable_points_is_rejected(self, authed_client, hand):
        """A cell a person typed is never coerced: storing 0 would read as a hand
        that was played for nothing, and the scorer would never know."""
        before = hand.points
        resp = authed_client.post('/update_hand_points', {
            'id': hand.id, 'version': hand.version, 'points': 'abc', 'by': 1, 'from': 2,
        })
        assert resp.status_code == 400
        hand.refresh_from_db()
        assert hand.points == before  # nothing written

    def test_blank_points_is_still_an_unplayed_row(self, authed_client, hand):
        """Blank stays legitimate — that's how an unplayed row is entered."""
        resp = authed_client.post('/update_hand_points', {
            'id': hand.id, 'version': hand.version, 'points': '', 'by': '', 'from': '',
        })
        assert resp.status_code == 200
        hand.refresh_from_db()
        assert (hand.points, hand.win_by, hand.win_from) == (0, None, None)

    def test_out_of_range_seat_is_rejected(self, authed_client, hand):
        """`by` outside 0-4 used to fall through to an unplayed row, which the
        round-completeness check then read as "still in progress"."""
        before = hand.win_by
        resp = authed_client.post('/update_hand_points', {
            'id': hand.id, 'version': hand.version, 'points': 25, 'by': 7, 'from': 2,
        })
        assert resp.status_code == 400
        hand.refresh_from_db()
        assert hand.win_by == before

    def test_non_numeric_id_is_a_400_not_a_500(self, authed_client):
        resp = authed_client.post('/update_hand_points', {
            'id': 'abc', 'version': 0, 'points': 1, 'by': 1, 'from': 2,
        })
        assert resp.status_code == 400


class TestCreateHandPoints:
    """Bulk save must keep the optimistic-lock version monotonic so it can't
    silently clobber a concurrent per-cell update_hand_points (I-C)."""

    def _bulk(self, client, round_nb=1, table_nb=1):
        data = {'round_nb': round_nb, 'table_nb': table_nb}
        for i in range(1, 17):
            data[f'points_{i}'] = 8
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
        assert hand.points == 8

    def test_writes_all_sixteen_hands(self, authed_client, tournament):
        resp = self._bulk(authed_client, round_nb=3, table_nb=2)  # unplayed in fixture
        assert resp.status_code == 200
        written = Hand.objects.filter(
            tenant=tournament['tenant'], round_nb=3, table_nb=2, points=8,
        ).count()
        assert written == 16


class TestUpdateSeatPenalty:
    """Per-player penalty: an integer minipoint adjustment saved from the score
    sheet. It is a score-sheet-only figure (the leaderboard never reads it), so
    unlike the MP/TP edits it stays editable after the round is published."""

    def _seat(self, tournament, round_nb=3, table_nb=1):
        # Round 3 is not published in the fixture, so it stays editable.
        return Seat.objects.filter(
            tenant=tournament['tenant'], round_nb=round_nb, table_nb=table_nb,
        ).order_by('wind').first()

    def test_sets_penalty(self, authed_client, tournament):
        seat = self._seat(tournament)
        resp = authed_client.post('/update_seat_penalty', {'id': seat.id, 'penalty': -10})
        assert resp.status_code == 200
        assert json.loads(resp.content)['status'] == 'ok'
        seat.refresh_from_db()
        assert seat.penalty == -10

    def test_invalid_penalty_defaults_to_zero(self, authed_client, tournament):
        seat = self._seat(tournament)
        seat.penalty = 5
        seat.save(update_fields=['penalty'])
        resp = authed_client.post('/update_seat_penalty', {'id': seat.id, 'penalty': 'abc'})
        assert resp.status_code == 200
        seat.refresh_from_db()
        assert seat.penalty == 0

    def test_penalty_does_not_touch_minipoints(self, authed_client, tournament):
        seat = self._seat(tournament)
        before_mp, before_tp = seat.minipoints, seat.tablepoints
        authed_client.post('/update_seat_penalty', {'id': seat.id, 'penalty': -20})
        seat.refresh_from_db()
        assert seat.minipoints == before_mp
        assert seat.tablepoints == before_tp

    def test_allowed_on_published_round(self, authed_client, tournament):
        # The penalty is a sheet-balance figure the standings never read, so it
        # can still be reconciled after the round is published — unlike MP/TP.
        seat = self._seat(tournament, round_nb=1)  # published in fixture
        resp = authed_client.post('/update_seat_penalty', {'id': seat.id, 'penalty': -10})
        assert resp.status_code == 200
        assert json.loads(resp.content)['status'] == 'ok'
        seat.refresh_from_db()
        assert seat.penalty == -10

    def test_nonexistent_seat_returns_404(self, authed_client):
        resp = authed_client.post('/update_seat_penalty', {'id': 999999, 'penalty': -10})
        assert resp.status_code == 404

    def test_score_sheet_renders_penalty_inputs(self, authed_client, tournament):
        seat = self._seat(tournament, round_nb=1, table_nb=1)
        seat.penalty = -10
        seat.save(update_fields=['penalty'])
        body = authed_client.get('/scores_per_hand_1_1').content.decode()
        # One editable penalty box per seat, pre-filled with the saved value.
        assert 'class="penalty-input"' in body
        assert 'id=\'pen_1\'' in body or 'id="pen_1"' in body
        assert 'update_seat_penalty' in body  # the persistence endpoint
        assert '-10' in body


class TestPublishedRoundLock:
    """A published round is read-only: scores can only change after it is
    explicitly unpublished. Rounds 1 & 2 are published in the fixture; round 3
    is not."""

    def _row(self, tournament, round_nb, table_nb=1):
        return list(
            Seat.objects.filter(
                tenant=tournament['tenant'], round_nb=round_nb, table_nb=table_nb,
            ).order_by('wind')
        )

    def _bulk_edit(self, client, seats, mp):
        return client.post(
            '/update_seats_bulk',
            data=json.dumps({'seats': [
                {'id': s.id, 'mp': mp, 'tp': s.tablepoints} for s in seats
            ]}),
            content_type='application/json',
        )

    def test_bulk_edit_rejected_on_published_round(self, authed_client, tournament):
        seats = self._row(tournament, round_nb=1)
        original = seats[0].minipoints

        resp = self._bulk_edit(authed_client, seats, mp=original + 7)
        assert resp.status_code == 409
        assert json.loads(resp.content)['status'] == 'locked'

        seats[0].refresh_from_db()
        assert seats[0].minipoints == original  # write must not have landed

    def test_locked_response_echoes_server_row(self, authed_client, tournament):
        # The 409 carries the current server values so the client can revert the
        # row it tried to change instead of leaving an unsaved score on screen.
        seats = self._row(tournament, round_nb=1)
        original = seats[0].minipoints
        resp = self._bulk_edit(authed_client, seats, mp=original + 7)
        body = json.loads(resp.content)
        assert body['row']['round_nb'] == 1
        assert body['row']['table_nb'] == 1
        assert body['row']['seats'][0]['mp'] == original

    def test_published_set_unchanged_after_rejected_edit(self, authed_client, tournament):
        before = set(PublishedRound.objects.filter(tenant=tournament['tenant'])
                     .values_list('round_nb', flat=True))
        self._bulk_edit(authed_client, self._row(tournament, round_nb=1), mp=1)
        after = set(PublishedRound.objects.filter(tenant=tournament['tenant'])
                    .values_list('round_nb', flat=True))
        assert before == after  # no silent unpublish

    def test_bulk_edit_allowed_on_unpublished_round(self, authed_client, tournament):
        seats = self._row(tournament, round_nb=3)  # not published in fixture
        resp = self._bulk_edit(authed_client, seats, mp=42)
        assert resp.status_code == 200
        seats[0].refresh_from_db()
        assert seats[0].minipoints == 42

    def test_edit_allowed_after_unpublishing(self, authed_client, tournament):
        tenant = tournament['tenant']
        seats = self._row(tournament, round_nb=2, table_nb=1)
        original = seats[0].minipoints

        # Locked while published.
        assert self._bulk_edit(authed_client, seats, mp=original + 3).status_code == 409

        # Unpublish round 2, then the edit is accepted.
        unpub = authed_client.post(
            '/set_round_published',
            data=json.dumps({'round_nb': 2, 'published': False}),
            content_type='application/json',
        )
        assert unpub.status_code == 200
        resp = self._bulk_edit(authed_client, seats, mp=original + 3)
        assert resp.status_code == 200
        seats[0].refresh_from_db()
        assert seats[0].minipoints == original + 3



class TestRiichiRoundTrip:
    """The Riichi save/publish path, which had clearly never been run end to end.

    Riichi ranks on minipoints alone: the grid has no table-point column, so its
    save payload carries no `tp` at all and Seat.tablepoints stays NULL. Two bugs
    fell straight out of that — the save raised KeyError: 'tp' (F7), and the
    publish completeness check required non-NULL tablepoints, so a Riichi round
    could never be published (F8).
    """

    @pytest.fixture
    def riichi_client(self, riichi_tournament):
        u = User.objects.create_user('riichi_scorer', password='pw')
        grant(u, riichi_tournament['tenant'], admin=True)
        c = Client()
        c.force_login(u)
        c.defaults['HTTP_HOST'] = 'test.example.com'
        return c

    def _seats(self, tenant, round_nb, table_nb):
        return list(Seat.objects.filter(
            tenant=tenant, round_nb=round_nb, table_nb=table_nb).order_by('wind'))

    def _save_minipoints(self, client, seats, values):
        """Save a row the way the Riichi grid does: minipoints only, no `tp` key."""
        return client.post(
            '/update_seats_bulk',
            data=json.dumps({'seats': [
                {'id': s.id, 'mp': mp} for s, mp in zip(seats, values)
            ]}),
            content_type='application/json',
        )

    def test_save_without_tablepoints_succeeds(self, riichi_client, riichi_tournament):
        tenant = riichi_tournament['tenant']
        seats = self._seats(tenant, round_nb=3, table_nb=1)  # unpublished in fixture
        resp = self._save_minipoints(riichi_client, seats, [40, 10, -10, -40])
        assert resp.status_code == 200
        for seat, expected in zip(seats, [40, 10, -10, -40]):
            seat.refresh_from_db()
            assert seat.minipoints == expected
            assert seat.tablepoints is None  # Riichi never fills these

    def test_round_publishes_with_null_tablepoints(self, riichi_client, riichi_tournament):
        tenant = riichi_tournament['tenant']
        # Fill round 3 (the fixture leaves it seated but unscored) the Riichi way.
        for table_nb in range(1, 5):
            seats = self._seats(tenant, round_nb=3, table_nb=table_nb)
            assert self._save_minipoints(
                riichi_client, seats, [40, 10, -10, -40]).status_code == 200
        Seat.objects.filter(tenant=tenant).update(tablepoints=None)

        resp = riichi_client.post(
            '/set_round_published',
            data=json.dumps({'round_nb': 3, 'published': True}),
            content_type='application/json',
        )
        assert resp.status_code == 200
        assert 3 in resp.json()['published_rounds']
        assert PublishedRound.objects.filter(tenant=tenant, round_nb=3).exists()

    def _fill_and_publish_round_three(self, client, tenant):
        """Score round 3 the Riichi way and publish it. Table 1 gets a known
        spread so a specific winner and loser can be checked."""
        for table_nb in range(1, 5):
            seats = self._seats(tenant, round_nb=3, table_nb=table_nb)
            assert self._save_minipoints(
                client, seats, [40, 10, -10, -40]).status_code == 200
        # Riichi never writes table points; the shared fixture pre-fills them from
        # the MCR seed, so clear them to get the real Riichi shape.
        Seat.objects.filter(tenant=tenant).update(tablepoints=None)
        return client.post(
            '/set_round_published',
            data=json.dumps({'round_nb': 3, 'published': True}),
            content_type='application/json',
        )

    def test_scores_with_null_tablepoints_reach_the_standings(
            self, riichi_client, riichi_tournament):
        """Publishing is only half the job — the scores have to count.

        `player_standings` treated a NULL tablepoints as "seat not scored yet", so
        under Riichi no round was ever complete and every total sat at 0. Read with
        full_view=True: round 3 is the final round, so publishing it withholds it
        from the public until the ceremony.
        """
        tenant = riichi_tournament['tenant']
        assert self._fill_and_publish_round_three(riichi_client, tenant).status_code == 200

        rows = scores_per_player_rows(_public_request(), full_view=True)
        assert rows, 'no standings rows for a published Riichi tournament'
        assert any(r['total']['mp'] for r in rows), 'Riichi totals all zero'

        # Round 3 must appear in each player's row at the value that was saved, and
        # be folded into their total. (Totals themselves aren't comparable across
        # players here — the fixture seeds rounds 1-2 with a wide spread.)
        by_id = {r['player_id']: r for r in rows}
        for seat, saved in zip(self._seats(tenant, round_nb=3, table_nb=1),
                               [40, 10, -10, -40]):
            player = Player.objects.get(tenant=tenant, draw_number=seat.draw_number)
            row = by_id[player.id]
            round_three = [sc for sc in row['scores'] if sc['round_nb'] == 3]
            assert round_three, f'round 3 missing from the row for {player.full_name}'
            assert round_three[0]['mp'] == saved
            assert row['total']['mp'] == sum(sc['mp'] for sc in row['scores'])

    def test_final_round_is_withheld_from_the_public(self, riichi_client, riichi_tournament):
        """The Riichi fix must not leak the final round: publishing it still
        withholds it until the ceremony, exactly as under MCR."""
        tenant = riichi_tournament['tenant']
        self._fill_and_publish_round_three(riichi_client, tenant)
        assert PublishedRound.objects.get(tenant=tenant, round_nb=3).withheld is True

        public = scores_per_player_rows(_public_request(), full_view=False)
        full = scores_per_player_rows(_public_request(), full_view=True)
        # The public sees two rounds' worth of scores, the ceremony all three.
        assert max(len(r['scores']) for r in public) == 2
        assert max(len(r['scores']) for r in full) == 3

    def test_riichi_round_counts_as_complete(self, riichi_client, riichi_tournament):
        """`_last_complete_round` shared the same NULL-tablepoints rule, so a
        Riichi tournament reported zero complete rounds however much was scored —
        which is what the admin's publish hints read."""
        from mahj.scoring import _last_complete_round
        tenant = riichi_tournament['tenant']
        self._fill_and_publish_round_three(riichi_client, tenant)
        assert _last_complete_round(tenant, riichi_tournament['settings']) == 3

    def test_mcr_round_still_needs_tablepoints(self, authed_client, tournament):
        """The F8 fix must not weaken MCR: there, missing table points really do
        mean the round is incomplete."""
        tenant = tournament['tenant']
        Seat.objects.filter(tenant=tenant, round_nb=3).update(minipoints=25, tablepoints=None)
        resp = authed_client.post(
            '/set_round_published',
            data=json.dumps({'round_nb': 3, 'published': True}),
            content_type='application/json',
        )
        assert resp.status_code == 400
        assert json.loads(resp.content)['error'] == 'round is incomplete'
        assert not PublishedRound.objects.filter(tenant=tenant, round_nb=3).exists()


class TestBulkPayloadIntegrity:
    """update_seats_bulk applies one table row per request. It used to read the
    round and table off the *first* seat only, so anything else in the payload
    rode in unchecked."""

    def _post(self, client, seats_and_mp):
        return client.post(
            '/update_seats_bulk',
            data=json.dumps({'seats': [
                {'id': s.id, 'mp': mp, 'tp': 0} for s, mp in seats_and_mp
            ]}),
            content_type='application/json',
        )

    def test_seats_from_two_rounds_are_rejected(self, authed_client, tournament):
        """Round 1 is published (locked), round 3 isn't. Leading with a round-3
        seat used to get the whole payload past the publish check — including the
        round-1 seats behind it."""
        tenant = tournament['tenant']
        open_seat = Seat.objects.filter(tenant=tenant, round_nb=3, table_nb=1).first()
        locked_seat = Seat.objects.filter(tenant=tenant, round_nb=1, table_nb=1).first()
        before = locked_seat.minipoints

        resp = self._post(authed_client, [(open_seat, 11), (locked_seat, 999)])
        assert resp.status_code == 400
        locked_seat.refresh_from_db()
        assert locked_seat.minipoints == before  # the published score is untouched

    def test_seats_from_two_tables_are_rejected(self, authed_client, tournament):
        tenant = tournament['tenant']
        a = Seat.objects.filter(tenant=tenant, round_nb=3, table_nb=1).first()
        b = Seat.objects.filter(tenant=tenant, round_nb=3, table_nb=2).first()
        assert self._post(authed_client, [(a, 5), (b, 5)]).status_code == 400

    def test_unparseable_minipoints_is_rejected(self, authed_client, tournament):
        """It used to be stored as NULL, which reads downstream as "not played"
        while the scorer's grid still showed their number."""
        tenant = tournament['tenant']
        seat = Seat.objects.filter(tenant=tenant, round_nb=3, table_nb=1).first()
        resp = authed_client.post(
            '/update_seats_bulk',
            data=json.dumps({'seats': [{'id': seat.id, 'mp': 'abc', 'tp': 0}]}),
            content_type='application/json',
        )
        assert resp.status_code == 400
        seat.refresh_from_db()
        assert seat.minipoints is None  # fixture leaves round 3 unscored

    def test_blank_minipoints_clears_the_cell(self, authed_client, tournament):
        """Blank stays a real instruction: it clears the score back to NULL."""
        tenant = tournament['tenant']
        seat = Seat.objects.filter(tenant=tenant, round_nb=3, table_nb=1).first()
        Seat.objects.filter(id=seat.id).update(minipoints=50)
        resp = authed_client.post(
            '/update_seats_bulk',
            data=json.dumps({'seats': [{'id': seat.id, 'mp': '', 'tp': ''}]}),
            content_type='application/json',
        )
        assert resp.status_code == 200
        seat.refresh_from_db()
        assert seat.minipoints is None

    def test_non_numeric_seat_id_is_a_400_not_a_500(self, authed_client, tournament):
        resp = authed_client.post(
            '/update_seats_bulk',
            data=json.dumps({'seats': [{'id': 'abc', 'mp': 1}]}),
            content_type='application/json',
        )
        assert resp.status_code == 400


class TestPruneBumpsVersion:
    """Validating a sheet coerces blank rows before the last played hand into
    draws. That rewrites rows a second device may still hold at the old version,
    so the version has to move or that device's next save lands silently."""

    def test_stale_save_after_prune_gets_a_409(self, authed_client, tournament):
        tenant = tournament['tenant']
        # A sheet with a gap: hand 1 played, hand 2 blank, hand 3 played.
        Hand.objects.filter(tenant=tenant, round_nb=3, table_nb=1).delete()
        for hand_nb, win_by in ((1, 1), (2, None), (3, 2)):
            Hand.objects.create(
                tenant=tenant, round_nb=3, table_nb=1, hand_nb=hand_nb,
                points=8 if win_by else 0, win_by=win_by, win_from=None,
            )
        blank = Hand.objects.get(tenant=tenant, round_nb=3, table_nb=1, hand_nb=2)
        stale_version = blank.version

        resp = authed_client.post(
            '/validate_score_sheet', {'round_nb': 3, 'table_nb': 1, 'validated': '1'})
        assert resp.status_code == 200

        blank.refresh_from_db()
        assert blank.win_by == 0                    # coerced to a draw
        assert blank.version == stale_version + 1   # and the version moved

        # The second device, still holding the pre-prune version, must be told to
        # rebase rather than quietly overwriting the coerced draw.
        conflict = authed_client.post('/update_hand_points', {
            'id': blank.id, 'version': stale_version, 'points': 30, 'by': 3, 'from': 1,
        })
        assert conflict.status_code == 409
        blank.refresh_from_db()
        assert blank.win_by == 0


class TestPhantomScoreSheet:
    """Opening a sheet records it so the round-completeness badges can see it. A
    (round, table) that isn't in the seating chart must not get that far, or a
    hand-typed URL marks a round "open" with nothing in the UI listing it."""

    def test_unknown_table_is_404_and_creates_no_sheet(self, authed_client, tournament):
        from mahj.models import ScoreSheet
        resp = authed_client.get('/scores_per_hand_3_999')
        assert resp.status_code == 404
        assert not ScoreSheet.objects.filter(
            tenant=tournament['tenant'], round_nb=3, table_nb=999).exists()

    def test_real_table_still_opens(self, authed_client, tournament):
        from mahj.models import ScoreSheet
        assert authed_client.get('/scores_per_hand_3_1').status_code == 200
        assert ScoreSheet.objects.filter(
            tenant=tournament['tenant'], round_nb=3, table_nb=1).exists()
