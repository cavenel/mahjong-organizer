"""Per-tenant dump/restore (mahj.tenant_dump): the full round-trip, the
precheck's refusal to wipe on a bad file, and what a restore must NOT touch
(memberships, publish config)."""
import gzip
import json

import pytest
from django.contrib.auth.models import User
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db.utils import IntegrityError
from django.test import Client

from mahj.models import (
    CeremonyState, Membership, Player, PublishTarget, PublishedRound, Schedule,
    ScoreSheet, Screen, ScreenMode, Seat, Tenant, TournamentSettings,
)
from mahj import tenant_dump
from mahj.tenant_dump import (
    TENANT_MODELS, TenantDumpError, dump_filename, dump_tenant, parse_dump,
    restore_tenant, schema_version, wipe_tenant,
)

from .conftest import grant

LOGO = b'\x89PNG\r\n\x1a\n' + bytes(range(256))


@pytest.fixture
def full_tournament(tournament):
    """The standard 16-player fixture, extended so every dumped model and every
    special-cased field type has data: screens, screen modes, a live ceremony,
    a withheld round, a binary logo and a running timer."""
    tenant = tournament['tenant']
    settings = tournament['settings']
    settings.logo = LOGO
    settings.logo_etag = 'abc123'
    settings.counter = 1_755_600_000_000  # running timer (epoch ms)
    settings.save()
    Screen.objects.create(tenant=tenant, name='Left wall', view='standings')
    Screen.objects.create(tenant=tenant, name='Stage', view='timer')
    ScreenMode.objects.create(tenant=tenant, name='Play', views=['standings', 'timer'])
    CeremonyState.objects.create(tenant=tenant, phase='players', step=3, stat_key='')
    # Round 3: published but withheld for the ceremony reveal.
    PublishedRound.objects.create(tenant=tenant, round_nb=3, withheld=True)
    Schedule.objects.create(tenant=tenant, day='Sun', time='18:00', name='Ceremony', is_round=False)
    return tournament


def snapshot(tenant):
    """Every dumped row as raw Python field values (no dump-side encoding, so an
    encode/decode loss can't hide), ordered by id, pk/tenant excluded."""
    state = {}
    for model in TENANT_MODELS:
        fields = [f.name for f in model._meta.concrete_fields
                  if f.name not in ('id', 'tenant')]
        rows = []
        for obj in model.objects.filter(tenant=tenant).order_by('id'):
            row = {}
            for name in fields:
                value = getattr(obj, name)
                # sqlite/postgres may hand BinaryField back as memoryview.
                row[name] = bytes(value) if isinstance(value, memoryview) else value
            rows.append(row)
        state[model.__name__] = rows
    return state


# ---------------------------------------------------------------- round-trip

def test_round_trip_same_tenant(full_tournament):
    tenant = full_tournament['tenant']
    before = snapshot(tenant)
    data = dump_tenant(tenant)

    # Diverge hard from the dumped state before restoring over it.
    Player.objects.filter(tenant=tenant).delete()
    Screen.objects.filter(tenant=tenant).delete()
    TournamentSettings.objects.filter(tenant=tenant).update(counter=-1, logo=None)

    result = restore_tenant(tenant, parse_dump(data))
    assert snapshot(tenant) == before
    assert result['source_subdomain'] == 'test'
    assert result['counts']['Player'] == 16
    assert result['counts']['Seat'] == 48


def test_round_trip_other_tenant(full_tournament):
    """The failover case: a dump from one install restored into a freshly
    created tenant with a different subdomain."""
    source = full_tournament['tenant']
    data = dump_tenant(source)
    target = Tenant.objects.create(name='Laptop', subdomain='laptop')

    result = restore_tenant(target, parse_dump(data))
    assert result['source_subdomain'] == 'test'
    assert snapshot(target) == snapshot(source)
    # The source tenant was read, never written.
    assert Player.objects.filter(tenant=source).count() == 16


def test_schedule_order_and_withheld_survive(full_tournament):
    tenant = full_tournament['tenant']
    data = dump_tenant(tenant)
    wipe_tenant(tenant)
    restore_tenant(tenant, parse_dump(data))

    # Schedule order is semantic (the Nth is_round row is round N) and is kept
    # by id even though every id was reassigned.
    names = list(Schedule.objects.filter(tenant=tenant).order_by('id')
                 .values_list('name', flat=True))
    assert names == ['Round 1', 'Round 2', 'Round 3', 'Ceremony']
    withheld = dict(PublishedRound.objects.filter(tenant=tenant)
                    .values_list('round_nb', 'withheld'))
    assert withheld == {1: False, 2: False, 3: True}


def test_binary_timer_and_autonow_fields_survive(full_tournament):
    tenant = full_tournament['tenant']
    original_times = list(Screen.objects.filter(tenant=tenant).order_by('id')
                          .values_list('time', flat=True))
    data = dump_tenant(tenant)
    restore_tenant(tenant, parse_dump(data))

    settings = TournamentSettings.objects.get(tenant=tenant)
    assert bytes(settings.logo) == LOGO
    assert settings.counter == 1_755_600_000_000
    # Screen.time is auto_now_add: the restore must keep the dumped stamps, not
    # the restore moment.
    restored_times = list(Screen.objects.filter(tenant=tenant).order_by('id')
                          .values_list('time', flat=True))
    assert restored_times == original_times


def test_dump_carries_no_publish_target_or_secrets(full_tournament):
    tenant = full_tournament['tenant']
    PublishTarget.objects.create(
        tenant=tenant, enabled=True, host='web.example.org', username='u',
        path='public_html', password_enc=b'ciphertext')
    payload = json.loads(gzip.decompress(dump_tenant(tenant)))
    assert 'PublishTarget' not in payload['models']
    assert 'Membership' not in payload['models']
    assert b'ciphertext' not in gzip.decompress(dump_tenant(tenant))


def test_restore_leaves_publish_target_and_memberships_alone(full_tournament, django_user_model):
    tenant = full_tournament['tenant']
    data = dump_tenant(tenant)
    user = django_user_model.objects.create_user('admin1')
    grant(user, tenant, admin=True)
    target = PublishTarget.objects.create(
        tenant=tenant, enabled=True, host='web.example.org', username='u',
        path='public_html', password_enc=b'ciphertext')

    restore_tenant(tenant, parse_dump(data))
    assert Membership.objects.filter(user=user, tenant=tenant, is_tenant_admin=True).exists()
    kept = PublishTarget.objects.get(tenant=tenant)
    assert kept.pk == target.pk
    assert kept.host == 'web.example.org'
    assert bytes(kept.password_enc) == b'ciphertext'


# ----------------------------------------------------------------- metadata

def test_dump_metadata_and_filename(full_tournament):
    payload = json.loads(gzip.decompress(dump_tenant(full_tournament['tenant'])))
    assert payload['format'] == 1
    assert payload['subdomain'] == 'test'
    assert payload['migration'] == schema_version()
    assert schema_version()  # the mahj migration leaf resolves to a real name
    name = dump_filename('test')
    assert name.startswith('mahj_test_') and name.endswith('.json.gz')


# ---------------------------------------------------- precheck: reject early

def rejects(data, match):
    with pytest.raises(TenantDumpError, match=match):
        parse_dump(data)


def as_dump(payload):
    return gzip.compress(json.dumps(payload).encode())


def valid_payload(**overrides):
    payload = {'format': 1, 'migration': schema_version(), 'subdomain': 'x',
               'created_utc': '', 'models': {}}
    payload.update(overrides)
    return payload


def test_parse_rejects_garbage(db):
    rejects(b'not gzip at all', 'json.gz')
    rejects(gzip.compress(b'{broken'), 'no JSON')
    rejects(as_dump({'format': 99}), 'format')
    rejects(as_dump(valid_payload(migration='0001_from_the_future')),
            'different app version')
    rejects(as_dump(valid_payload(models=[])), 'no model data')
    rejects(as_dump(valid_payload(models={'Nope': []})), 'unknown model')
    rejects(as_dump(valid_payload(models={'Player': [{'hacked': 1}]})), 'unknown field')


def test_a_gzip_bomb_is_refused_without_inflating_it(db, monkeypatch):
    """gzip reaches ~1000:1, so a file well inside the 50 MB request cap can ask for
    tens of gigabytes — and gzip.decompress() inflates the whole stream before
    anyone can look at it. The cap is lowered here so the test doesn't have to
    build a real bomb; what it pins is that the refusal happens *during* the read
    rather than after a full inflate.
    """
    monkeypatch.setattr(tenant_dump, 'MAX_UNCOMPRESSED_BYTES', 4 * 1024 * 1024)
    monkeypatch.setattr(tenant_dump, '_GUNZIP_CHUNK', 64 * 1024)
    bomb = gzip.compress(b'\0' * (32 * 1024 * 1024))
    assert len(bomb) < 200 * 1024, 'the point is that the compressed file is small'

    with pytest.raises(TenantDumpError) as exc:
        parse_dump(bomb)
    assert 'expands to far more' in str(exc.value)


def test_a_real_dump_is_nowhere_near_the_cap(full_tournament):
    """The counterpart: the cap must never be able to refuse a genuine file."""
    data = dump_tenant(full_tournament['tenant'])
    assert len(gzip.decompress(data)) < tenant_dump.MAX_UNCOMPRESSED_BYTES / 10
    assert parse_dump(data)['subdomain'] == full_tournament['tenant'].subdomain


def test_bad_file_never_wipes(full_tournament):
    tenant = full_tournament['tenant']
    before = snapshot(tenant)
    with pytest.raises(TenantDumpError):
        parse_dump(as_dump(valid_payload(migration='mismatch')))
    assert snapshot(tenant) == before


# ---------------------------------------------------------- HTTP endpoints

HOST = 'test.example.com'   # the fixture tenant's host (subdomain 'test')


def logged_in_client(tenant, admin=True, reauthed=True):
    user = User.objects.create_user(f'u_{admin}_{reauthed}', password='pw')
    grant(user, tenant, admin=admin, scorer=not admin)
    c = Client()
    c.defaults['HTTP_HOST'] = HOST
    c.force_login(user)
    if reauthed:
        resp = c.post('/user_reauth', data=json.dumps({'password': 'pw'}),
                      content_type='application/json')
        assert resp.status_code == 200
    return c


def upload(data, client, confirm='test'):
    return client.post('/tenant_restore', {
        'dumpfile': SimpleUploadedFile('dump.json.gz', data, content_type='application/gzip'),
        'confirm': confirm,
    })


def test_download_endpoint(full_tournament):
    client = logged_in_client(full_tournament['tenant'], reauthed=False)
    resp = client.get('/tenant_dump')
    assert resp.status_code == 200
    assert resp['Content-Type'] == 'application/gzip'
    assert 'mahj_test_' in resp['Content-Disposition']
    payload = json.loads(gzip.decompress(resp.content))
    assert payload['subdomain'] == 'test'
    assert len(payload['models']['Player']) == 16


def test_download_needs_tenant_admin(full_tournament):
    client = logged_in_client(full_tournament['tenant'], admin=False, reauthed=False)
    assert client.get('/tenant_dump').status_code == 403


def test_restore_endpoint_round_trip(full_tournament):
    tenant = full_tournament['tenant']
    before = snapshot(tenant)
    client = logged_in_client(tenant)
    data = client.get('/tenant_dump').content

    Player.objects.filter(tenant=tenant).delete()
    resp = upload(data, client)
    assert resp.status_code == 200
    body = resp.json()
    assert body['status'] == 'ok'
    assert body['counts']['Player'] == 16
    assert body['source_subdomain'] == 'test'
    assert snapshot(tenant) == before


def test_restore_needs_recent_reauth(full_tournament):
    tenant = full_tournament['tenant']
    data = dump_tenant(tenant)
    client = logged_in_client(tenant, reauthed=False)
    resp = upload(data, client)
    assert resp.status_code == 403
    assert resp.json()['status'] == 'reauth_required'


def test_restore_needs_matching_confirmation(full_tournament):
    tenant = full_tournament['tenant']
    before = snapshot(tenant)
    client = logged_in_client(tenant)
    resp = upload(dump_tenant(tenant), client, confirm='wrong')
    assert resp.status_code == 400
    assert snapshot(tenant) == before


def test_restore_rejects_garbage_and_keeps_data(full_tournament):
    tenant = full_tournament['tenant']
    before = snapshot(tenant)
    client = logged_in_client(tenant)
    resp = upload(b'not a dump', client)
    assert resp.status_code == 400
    assert 'json.gz' in resp.json()['error']
    assert snapshot(tenant) == before


def test_failed_restore_rolls_back(full_tournament):
    """A constraint violation mid-load (here: a duplicated draw number) must
    leave the tenant exactly as it was — wipe and load share one transaction."""
    tenant = full_tournament['tenant']
    before = snapshot(tenant)
    payload = parse_dump(dump_tenant(tenant))
    payload['models']['Player'][1]['draw_number'] = \
        payload['models']['Player'][0]['draw_number']
    with pytest.raises(IntegrityError):
        restore_tenant(tenant, payload)
    assert snapshot(tenant) == before
