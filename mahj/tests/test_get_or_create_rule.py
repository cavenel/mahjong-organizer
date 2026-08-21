"""Every `get_or_create` needs a unique constraint covering its lookup.

Without one, two concurrent calls each insert a row and every later call raises
MultipleObjectsReturned — a permanent 500 on whatever page reads it. `PublishTarget`
was the one model in the tree missing it, and three passes of a review found it
independently, which is a sign the rule is worth enforcing mechanically rather than
noticing again next time.

Written as a sweep over the source rather than a list of models, so a model added
later is covered without anyone remembering to add it here.
"""
import ast
import pathlib

import pytest
from django.apps import apps

MAHJ = pathlib.Path(__file__).resolve().parent.parent
# Migrations are historical and may legitimately predate a constraint; tests are not
# production behaviour.
SKIP_DIRS = {'migrations', 'tests', '__pycache__'}


def _model_and_lookup(call):
    """('ModelName', {'lookup', 'fields'}) for a `Model.objects.get_or_create(...)`.

    Returns None for a call this sweep can't attribute — `cls.objects.get_or_create`
    inside a classmethod, for instance. Those are reported, not silently dropped.
    """
    func = call.func
    if not (isinstance(func, ast.Attribute) and func.attr == 'get_or_create'):
        return None
    manager = func.value
    if not (isinstance(manager, ast.Attribute) and manager.attr == 'objects'):
        return None
    owner = manager.value
    if not isinstance(owner, ast.Name) or owner.id in ('cls', 'self'):
        # `cls.objects.get_or_create` inside a classmethod, or anything reached through
        # an attribute chain: the model can't be named from the syntax alone.
        return None
    lookup = {kw.arg for kw in call.keywords if kw.arg not in (None, 'defaults')}
    return owner.id, lookup


def _sites():
    resolved, unattributed = [], []
    for path in sorted(MAHJ.rglob('*.py')):
        if set(path.parts) & SKIP_DIRS:
            continue
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            f = node.func
            if not (isinstance(f, ast.Attribute) and f.attr == 'get_or_create'):
                continue
            got = _model_and_lookup(node)
            where = f'{path.relative_to(MAHJ.parent)}:{node.lineno}'
            if got is None:
                unattributed.append(where)
            else:
                resolved.append((where, *got))
    return resolved, unattributed


def _unique_field_sets(model):
    sets = []
    for c in model._meta.constraints:
        fields = getattr(c, 'fields', None)
        if fields and getattr(c, 'condition', None) is None:
            sets.append(set(fields))
    for together in model._meta.unique_together:
        sets.append(set(together))
    for field in model._meta.fields:
        if field.unique:
            sets.append({field.name})
    return sets


def test_the_sweep_finds_the_sites_it_should():
    """Guard against the sweep silently matching nothing — then every assertion below
    would pass vacuously."""
    resolved, unattributed = _sites()
    assert len(resolved) >= 5, f'only found {len(resolved)} get_or_create sites'
    # `Tenant.get_default_pk` uses `cls.objects`, which AST can't attribute. If that
    # count grows, a new unattributable pattern appeared and needs checking by hand.
    assert len(unattributed) <= 1, f'unattributed get_or_create sites: {unattributed}'


@pytest.mark.parametrize('where,model_name,lookup', _sites()[0],
                         ids=lambda v: v if isinstance(v, str) else '')
def test_every_get_or_create_lookup_is_unique(where, model_name, lookup):
    model = apps.get_model('mahj', model_name)
    covering = [s for s in _unique_field_sets(model) if s <= lookup]
    assert covering, (
        f'{where}: {model_name}.objects.get_or_create({sorted(lookup)}) has no unique '
        f'constraint covering those fields, so two concurrent calls each insert a row '
        f'and every later call raises MultipleObjectsReturned. '
        f'Declared unique sets: {[sorted(s) for s in _unique_field_sets(model)]}')
