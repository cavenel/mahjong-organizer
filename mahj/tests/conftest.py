import types
import pytest
from django.contrib.auth.models import AnonymousUser
from django.db.models import ForeignKey, Model
from django.db.models.query import QuerySet
from django.test import RequestFactory

from mahj.models import Tenant, Player, Variable, Schedule, Position, Hand, PublishedRound


@pytest.fixture
def tenant(db):
    return Tenant.objects.create(name='Test', subdomain='test')


@pytest.fixture
def tournament(tenant):
    """Seed: 16 players, 3 rounds (2 complete with hands, 1 partial — Positions but no points)."""
    variable = Variable.objects.create(
        tenant=tenant, welcome='Welcome', title='T', fullname='FT',
        nb_rounds=3, rules='MCR', total_time=60 * 60, zoom=1.0, score_lines=20,
    )
    for i in range(3):
        Schedule.objects.create(tenant=tenant, day='Sat', time=f'{10 + i:02d}:00', name=f'Round {i + 1}')

    countries = ['Sweden', 'Sweden', 'Sweden', 'France', 'Japan', 'Germany', 'Sweden', 'France',
                 'Japan', 'Germany', 'Sweden', 'France', 'Japan', 'Germany', 'Sweden', 'France']
    players = [
        Player.objects.create(
            tenant=tenant, rand_id=i + 1,
            full_name=f'Player{i + 1} Lastname',
            first_name=f'Player{i + 1}',
            country=countries[i], EMA_ID=f'E{i + 1:05d}', email='',
        )
        for i in range(16)
    ]

    rotations = [
        [[0, 1, 2, 3], [4, 5, 6, 7], [8, 9, 10, 11], [12, 13, 14, 15]],
        [[0, 5, 10, 15], [4, 9, 14, 3], [8, 13, 2, 7], [12, 1, 6, 11]],
        [[0, 6, 11, 13], [4, 10, 15, 1], [8, 14, 3, 5], [12, 2, 7, 9]],
    ]
    for rn in range(3):
        for tn in range(4):
            for pos in range(4):
                p_idx = rotations[rn][tn][pos]
                complete = rn < 2
                Position.objects.create(
                    tenant=tenant, round_nb=rn + 1, table_nb=tn + 1,
                    position=pos + 1, player=players[p_idx],
                    minipoints=(p_idx * 10 + rn * 5) % 200 if complete else None,
                    tablepoints=float([4, 2, 1, 0][pos]) if complete else None,
                )

    for rn in range(2):
        for tn in range(4):
            for hn in range(1, 17):
                Hand.objects.create(
                    tenant=tenant, round_nb=rn + 1, table_nb=tn + 1, hand_nb=hn,
                    pts=((rn + 1) * 100 + tn * 10 + hn) % 50,
                    win_by=((rn + tn + hn) % 4) + 1,
                    win_from=((rn + tn + hn + 1) % 4) + 1,
                )
            Hand.objects.create(
                tenant=tenant, round_nb=rn + 1, table_nb=tn + 1, hand_nb=17,
                pts=1, win_by=0, win_from=0,
            )

    # Publish the two completed rounds so public viewers see them
    # (mirrors scorer-admin workflow: complete a round, then publish it).
    for rn in range(1, 3):
        PublishedRound.objects.create(tenant=tenant, round_nb=rn, reveal_level=100)

    return {'tenant': tenant, 'variable': variable, 'players': players}


@pytest.fixture
def request_(tournament):
    rf = RequestFactory()
    req = rf.get('/', HTTP_HOST='test.mahj.ovh')
    req.user = AnonymousUser()
    return req


@pytest.fixture
def riichi_tournament(tournament):
    """The standard fixture re-ruled as Riichi: ranking is on minipoints alone
    (no table points), so it exercises the non-MCR scoring/standings path."""
    variable = tournament['variable']
    variable.rules = 'Riichi'
    variable.save()
    return tournament


@pytest.fixture
def request_riichi(riichi_tournament):
    rf = RequestFactory()
    req = rf.get('/', HTTP_HOST='test.mahj.ovh')
    req.user = AnonymousUser()
    return req


def _model_snapshot(m):
    data = {'__model__': type(m).__name__, 'pk': m.pk}
    for field in m._meta.fields:
        name = field.name
        if isinstance(field, ForeignKey):
            data[f'{name}_id'] = getattr(m, f'{name}_id')
        else:
            data[name] = getattr(m, name)
    return data


def normalize(obj):
    """Recursively convert Django objects to stable JSON-serializable form."""
    if obj is None or isinstance(obj, (bool, int, float, str)):
        return obj
    if isinstance(obj, Model):
        return _model_snapshot(obj)
    if isinstance(obj, QuerySet):
        return [normalize(x) for x in obj]
    if isinstance(obj, types.MethodType):
        try:
            return normalize(obj())
        except Exception as e:
            return f'<method {obj.__func__.__name__} raised {type(e).__name__}>'
    if isinstance(obj, dict):
        return {k: normalize(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [normalize(x) for x in obj]
    return repr(obj)
