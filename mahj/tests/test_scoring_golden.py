"""Golden-file tests for the complex scoring/stats views.

First run creates snapshots under snapshots/ and fails with a message.
Subsequent runs compare against the saved snapshot.

To regenerate after an intentional change: delete the file and re-run.
"""
import json
import os
import pytest

from mahj import views
from mahj.tests.conftest import normalize

SNAPSHOT_DIR = os.path.join(os.path.dirname(__file__), 'snapshots')


def assert_snapshot(name, actual):
    os.makedirs(SNAPSHOT_DIR, exist_ok=True)
    path = os.path.join(SNAPSHOT_DIR, f'{name}.json')
    serialized = json.dumps(normalize(actual), indent=2, sort_keys=True, default=str)
    if not os.path.exists(path):
        with open(path, 'w') as f:
            f.write(serialized)
        pytest.fail(f'Snapshot {name} created at {path}. Re-run to verify.')
    with open(path) as f:
        expected = f.read()
    assert serialized == expected, f'Snapshot {name} drift — inspect {path}'


def test_scores_per_table_grid(request_):
    assert_snapshot('scores_per_table_grid', views.scores_per_table_grid(request_))


def test_stat_rounds(request_):
    assert_snapshot('stat_rounds', views.stat_rounds(request_))


def test_stat_all_rounds(request_):
    assert_snapshot('stat_all_rounds', views.stat_all_rounds(request_))


def test_table_stats(request_):
    assert_snapshot('table_stats', views.table_stats(request_))


def test_table_stats_rounds(request_):
    assert_snapshot('table_stats_rounds', views.table_stats_rounds(request_))


def test_player_rounds_rows(request_, tournament):
    player_id = tournament['players'][0].id
    assert_snapshot('player_rounds_rows_p1', views.player_rounds_rows(request_, player_id))


def test_player_extra_stats(tournament):
    from mahj import scoring
    tenant, players, tournament = tournament['tenant'], tournament['players'], tournament['settings']
    stats = scoring.player_extra_stats(tenant, players[0], tournament)
    assert_snapshot('player_extra_stats_opp_strength_p1', stats['opp_strength'])


def test_scores_per_player_rows_default(request_):
    assert_snapshot('scores_per_player_rows_default', views.scores_per_player_rows(request_))


def test_scores_per_player_rows_force_all(request_):
    assert_snapshot('scores_per_player_rows_force_all',
                    views.scores_per_player_rows(request_, full_view=True))


def test_scores_per_player_rows_query_count(request_, django_assert_max_num_queries):
    # Was O(rounds × players) — now a small constant. Cap is a regression guard.
    with django_assert_max_num_queries(5):
        views.scores_per_player_rows(request_)


def test_all_player_rounds_matches_player_rounds(tournament):
    from mahj import scoring
    tenant, players = tournament['tenant'], tournament['players']
    bulk = scoring.all_player_rounds(tenant, players)
    for p in players:
        assert normalize(bulk[p.id]) == normalize(scoring.player_rounds(tenant, p))


def test_all_player_rounds_query_count(tournament, django_assert_max_num_queries):
    # The whole point of the bulk path: a small constant, not ~2 queries per player.
    from mahj import scoring
    tenant, players = tournament['tenant'], tournament['players']
    with django_assert_max_num_queries(5):
        scoring.all_player_rounds(tenant, players)
