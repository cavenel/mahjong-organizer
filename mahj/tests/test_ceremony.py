"""Prize-giving ceremony: state transitions, screen takeover, and publish."""
import json
import types

import pytest
from django.contrib.auth.models import AnonymousUser, User
from django.test import Client, RequestFactory

from mahj.models import CeremonyState, PublishedRound, Screen, Seat
from mahj.views import ceremony


@pytest.fixture
def teamed(tournament):
    """Assign the 16 players to 4 teams of 4."""
    players = tournament['players']
    for i, p in enumerate(players):
        p.team = f'Team {chr(ord("A") + i % 4)}'
        p.save()
    return tournament


@pytest.fixture
def teamed_riichi(teamed, riichi_tournament):
    """The teamed fixture re-ruled as Riichi (ranking on minipoints)."""
    return teamed


@pytest.fixture
def tied_players(teamed):
    """Force players[0] and players[1] into an exact (mp, tp) tie across the
    counted rounds, so they share a `pos` in the standings.

    This guards the ceremony console's hard assumption that `pos` is NOT a
    unique key: tied players share it, so the reveal list must key on
    `player_id` and track reveals by index, not by `pos`.
    """
    tenant = teamed['tenant']
    a, b = teamed['players'][0], teamed['players'][1]
    for rn in (1, 2):  # the two completed (counted) rounds
        Seat.objects.filter(
            tenant=tenant, round_nb=rn,
            draw_number__in=[a.draw_number, b.draw_number]).update(
            minipoints=120, tablepoints=2.0)
    return teamed, a, b


def _request(host='test.example.com'):
    rf = RequestFactory()
    req = rf.get('/', HTTP_HOST=host)
    req.user = AnonymousUser()
    return req


@pytest.fixture
def op_client(teamed):
    c = Client()
    c.force_login(User.objects.create_user('op', password='pw', is_staff=True))
    c.defaults['HTTP_HOST'] = 'test.example.com'
    return c


class TestMasterData:
    def test_only_top_three_teams_with_members_and_both_totals(self, teamed):
        master = ceremony._ceremony_master(_request())
        assert master['uses_teams'] is True
        assert len(master['teams']) == 3                # prize-winning teams only
        assert [t['pos'] for t in master['teams']] == [1, 2, 3]
        for t in master['teams']:
            assert set(t.keys()) == {'pos', 'name', 'flag', 'tp', 'mp', 'players'}
            assert t['players']                         # member names listed
            assert all(isinstance(n, str) for n in t['players'])

    def test_players_top_sixteen_carry_tp_and_mp(self, teamed):
        master = ceremony._ceremony_master(_request())
        assert len(master['players']) == 16
        for p in master['players']:
            assert set(p.keys()) == {'pos', 'player_id', 'name', 'flag', 'total', 'mp'}

    def test_player_display_total_is_minipoints_for_riichi(self, teamed_riichi):
        # MCR shows table points as the headline total; Riichi has none, so the
        # displayed total must be the minipoints score.
        master = ceremony._ceremony_master(_request())
        assert master['rules'] == 'Riichi'
        assert master['players']
        for p in master['players']:
            assert p['total'] == p['mp']


class TestTieHandling:
    """A tie makes `pos` non-unique. The console keys its reveal list on
    `player_id` and tracks reveals by index for exactly this reason."""

    def test_tied_players_share_pos_but_keep_a_unique_key(self, tied_players):
        teamed, a, b = tied_players
        master = ceremony._ceremony_master(_request())
        by_id = {p['player_id']: p for p in master['players']}

        # The tie is genuine: the two players share a rank...
        assert a.id in by_id and b.id in by_id
        assert by_id[a.id]['pos'] == by_id[b.id]['pos']

        positions = [p['pos'] for p in master['players']]
        ids = [p['player_id'] for p in master['players']]
        # ...so `pos` collides (unsafe as an Alpine :key), but player_id stays unique.
        assert len(set(positions)) < len(positions)
        assert len(set(ids)) == len(ids)

    def test_reveal_advances_one_entry_at_a_time_across_a_tie(self, tied_players):
        master = ceremony._ceremony_master(_request())
        n = len(master['players'])
        # Each step reveals exactly one more entry (by index), even where two
        # entries share a pos — never both tied entries in a single step.
        for step in range(n + 1):
            state = types.SimpleNamespace(phase='players', step=step, stat_key='')
            payload = ceremony._slide_payload(master, state)
            assert len(payload['entries']) == step


class TestSlidePayload:
    def test_teams_reveal_counts_from_the_bottom(self, teamed):
        master = ceremony._ceremony_master(_request())

        state = types.SimpleNamespace(phase='teams', step=1, stat_key='')
        payload = ceremony._slide_payload(master, state)
        # step 1 reveals only the worst of the top 3 (pos 3)
        assert [e['pos'] for e in payload['entries']] == [3]
        assert payload['current']['pos'] == 3
        assert payload['done'] is False

        state.step = 3
        payload = ceremony._slide_payload(master, state)
        assert [e['pos'] for e in payload['entries']] == [1, 2, 3]  # sorted 1st-first
        assert payload['current']['pos'] == 1
        assert payload['done'] is True


class TestStatRoundLabel:
    def test_uniform_round_and_table_gives_a_label(self):
        winners = [{'round_nb': 3, 'table_nb': 5}, {'round_nb': 3, 'table_nb': 5}]
        assert ceremony._round_label(winners) == 'Round 3 · Table 5'

    def test_winners_from_different_spots_have_no_label(self):
        winners = [{'round_nb': 3, 'table_nb': 5}, {'round_nb': 4, 'table_nb': 1}]
        assert ceremony._round_label(winners) == ''

    def test_overall_stat_without_a_round_has_no_label(self):
        assert ceremony._round_label([{'round_nb': None, 'table_nb': None}]) == ''


class TestStatTwoStepReveal:
    def test_step_flows_into_the_stat_slide_payload(self, teamed):
        """The slide carries the reveal step so the screen can show title-only
        (step 0) then value + winners (step 1)."""
        master = ceremony._ceremony_master(_request())
        key = master['stats'][0]['key'] if master['stats'] else 'mp_max'

        for step in (0, 1):
            state = types.SimpleNamespace(phase='stat', step=step, stat_key=key)
            payload = ceremony._slide_payload(master, state)
            assert payload['step'] == step
            assert payload['stat_key'] == key


class TestControlEndpoint:
    def test_stat_reveal_step_is_stored(self, op_client, teamed):
        tenant = teamed['tenant']
        master = ceremony._ceremony_master(_request())
        key = master['stats'][0]['key'] if master['stats'] else 'mp_max'

        op_client.get(f'/ceremony_control?phase=stat&stat_key={key}&step=0')
        state = CeremonyState.objects.get(tenant=tenant)
        assert (state.phase, state.stat_key, state.step) == ('stat', key, 0)

        op_client.get(f'/ceremony_control?phase=stat&stat_key={key}&step=1')
        state.refresh_from_db()
        assert state.step == 1  # second click reveals the value


    def test_requires_display_op(self, teamed):
        resp = Client().get('/ceremony_control?phase=teams', HTTP_HOST='test.example.com')
        assert resp.status_code in (302, 403)

    def test_start_and_reveal(self, op_client, teamed):
        tenant = teamed['tenant']
        op_client.get('/ceremony_control?phase=teams&step=0')
        op_client.get('/ceremony_control?phase=teams&step=1')
        state = CeremonyState.objects.get(tenant=tenant)
        assert state.phase == 'teams'
        assert state.step == 1

    def test_stop_returns_to_idle_without_publishing(self, op_client, teamed):
        tenant = teamed['tenant']
        op_client.get('/ceremony_control?phase=teams&step=3')
        # round 3 is partial and was never published by the fixture
        assert not PublishedRound.objects.filter(tenant=tenant, round_nb=3).exists()

        op_client.get('/ceremony_control?phase=idle')
        assert CeremonyState.objects.get(tenant=tenant).phase == 'idle'
        # exiting the ceremony must not publish anything
        assert not PublishedRound.objects.filter(tenant=tenant, round_nb=3).exists()

    def test_publish_reveals_all_rounds_and_ends(self, op_client, teamed):
        tenant = teamed['tenant']
        nb_rounds = teamed['variable'].nb_rounds
        op_client.get('/ceremony_control?phase=teams&step=2')

        resp = op_client.get('/ceremony_control?action=publish')
        body = json.loads(resp.content)
        assert body['published'] is True
        assert body['phase'] == 'idle'

        state = CeremonyState.objects.get(tenant=tenant)
        assert state.phase == 'idle'
        for rn in range(1, nb_rounds + 1):
            assert PublishedRound.objects.get(tenant=tenant, round_nb=rn).withheld is False


class TestScreenTakeover:
    def test_idle_shows_normal_view(self, teamed):
        Screen.objects.create(tenant=teamed['tenant'], name='S1', view='scores all')
        resp = Client().get('/1', HTTP_HOST='test.example.com')
        assert resp.status_code == 200
        assert '<title>Prize-giving</title>' not in resp.content.decode()

    def test_active_takes_over_screen(self, teamed):
        Screen.objects.create(tenant=teamed['tenant'], name='S1', view='scores all')
        CeremonyState.objects.create(tenant=teamed['tenant'], phase='teams', step=2)
        resp = Client().get('/1', HTTP_HOST='test.example.com')
        assert resp.status_code == 200
        html = resp.content.decode()
        # ceremony template took over (not the normal scores view); the current
        # slide is embedded for the client to render and patch live over the ws.
        assert '<title>Prize-giving</title>' in html
        assert '"phase": "teams"' in html
        assert '"done": false' in html  # step 2 of 4 — reveal still in progress
