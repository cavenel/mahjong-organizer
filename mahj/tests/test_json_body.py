"""``json_body`` (views/helpers.py) — the one shared request-body JSON parser.

Every JSON endpoint routes its body through it, so malformed input has a single
uniform outcome: ``BadRequest``, which Django renders as a plain 400. The
end-to-end path (garbage body -> 400 response) is covered per-endpoint where
those endpoints are tested (e.g. test_team_draw's malformed-body test).
"""
import pytest
from django.core.exceptions import BadRequest
from django.test import RequestFactory

from mahj.views.helpers import json_body

rf = RequestFactory()


def _post(data):
    return rf.post('/', data=data, content_type='application/json')


def test_object_body_parses():
    assert json_body(_post('{"a": 1}')) == {'a': 1}


def test_empty_body_is_empty_object():
    assert json_body(_post('')) == {}


def test_malformed_body_raises_bad_request():
    with pytest.raises(BadRequest):
        json_body(_post('not json'))


def test_non_object_body_raises_bad_request():
    # Endpoints call .get()/['key'] on the result, so a JSON array or scalar
    # must be rejected here, not surface later as an AttributeError 500.
    with pytest.raises(BadRequest):
        json_body(_post('[1, 2]'))
