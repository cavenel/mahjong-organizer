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


def test_scores_per_table_json(request_):
    assert_snapshot('scores_per_table_json', views.scores_per_table_json(request_))


def test_stat_rounds(request_):
    assert_snapshot('stat_rounds', views.stat_rounds(request_))


def test_stat_all_rounds(request_):
    assert_snapshot('stat_all_rounds', views.stat_all_rounds(request_))


def test_player_rounds_json(request_, tournament):
    player_id = tournament['players'][0].id
    assert_snapshot('player_rounds_json_p1', views.player_rounds_json(request_, player_id))


def test_scores_per_player_json_default(request_):
    assert_snapshot('scores_per_player_json_default', views.scores_per_player_json(request_))


def test_scores_per_player_json_force_all(request_):
    assert_snapshot('scores_per_player_json_force_all',
                    views.scores_per_player_json(request_, force_all=True))


def test_scores_per_player_json_query_count(request_, django_assert_max_num_queries):
    # Was O(rounds × players) — now a small constant. Cap is a regression guard.
    with django_assert_max_num_queries(5):
        views.scores_per_player_json(request_)


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
