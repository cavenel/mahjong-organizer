"""Host-key handling for the static-publish SFTP target.

An unpinned target used to auto-add the server's key on *every* connect, which
leaves it interceptable for its whole life. Now the first connect records the key
and each one after that is checked against it, so the trust-on-first-use window is
a single connection — the same bargain ssh itself makes.
"""
import io
import types

import paramiko
import pytest

from mahj.models import PublishTarget, Tenant
from mahj.publish import sftp_upload


# A real key pair: paramiko parses known_hosts lines for real, so the base64 has
# to be genuine. Generated once for the module.
SERVER_KEY = paramiko.RSAKey.generate(2048)
OTHER_KEY = paramiko.RSAKey.generate(2048)


def host_line(name, key):
    return f'{name} {key.get_name()} {key.get_base64()}'


class _FakeClient:
    """Enough of paramiko.SSHClient for _connect: records the policy it was given
    and hands back a transport carrying a host key."""

    def __init__(self):
        self.policy = None
        self.connected = False
        self._host_keys = paramiko.hostkeys.HostKeys()

    def set_missing_host_key_policy(self, policy):
        self.policy = policy

    def get_host_keys(self):
        return self._host_keys

    def connect(self, **kwargs):
        self.connected = True
        self.connect_kwargs = kwargs

    def get_transport(self):
        return types.SimpleNamespace(get_remote_server_key=lambda: SERVER_KEY)


@pytest.fixture
def fake_client(monkeypatch):
    client = _FakeClient()
    monkeypatch.setattr(sftp_upload.paramiko, 'SSHClient', lambda: client)
    return client


@pytest.fixture
def target(db):
    tenant = Tenant.objects.create(name='Pub', subdomain='pub')
    return PublishTarget.objects.create(
        tenant=tenant, enabled=True, host='example.org', port=22,
        username='u', path='/srv/site')


def _cfg(**over):
    base = dict(host='example.org', port=22, username='u', path='/srv/site',
                subdomain='pub')
    base.update(over)
    return sftp_upload.PublishConfig(**base)


class TestFirstConnectPinsTheKey:
    def test_unpinned_target_learns_and_stores_the_key(self, fake_client, target):
        sftp_upload._connect(_cfg())
        target.refresh_from_db()
        assert target.host_key == host_line('example.org', SERVER_KEY)

    def test_a_nonstandard_port_is_bracketed_like_known_hosts(self, fake_client, target):
        target.port = 2222
        target.save(update_fields=['port'])
        sftp_upload._connect(_cfg(port=2222))
        target.refresh_from_db()
        assert target.host_key == host_line('[example.org]:2222', SERVER_KEY)

    def test_it_never_overwrites_a_key_the_operator_pinned(self, fake_client, target):
        pinned = host_line('example.org', OTHER_KEY)
        target.host_key = pinned
        target.save(update_fields=['host_key'])
        # A pinned config goes down the RejectPolicy path and records nothing.
        sftp_upload._connect(_cfg(host_key=pinned))
        target.refresh_from_db()
        assert target.host_key == pinned


class TestPolicySelection:
    def test_pinned_target_refuses_an_unknown_key(self, fake_client, target):
        pinned = host_line('example.org', OTHER_KEY)
        sftp_upload._connect(_cfg(host_key=pinned))
        assert isinstance(fake_client.policy, paramiko.RejectPolicy)

    def test_unpinned_target_accepts_only_so_it_can_record_it(self, fake_client, target):
        sftp_upload._connect(_cfg())
        assert isinstance(fake_client.policy, paramiko.AutoAddPolicy)
        # …and having recorded it, the next connect is pinned.
        target.refresh_from_db()
        assert target.host_key
        sftp_upload._connect(_cfg(host_key=target.host_key))
        assert isinstance(fake_client.policy, paramiko.RejectPolicy)


class TestPrivateKeyMessages:
    """The operator pastes a key into a form and gets one line back, so the line has
    to name the actual problem. A passphrase-protected key is a *supported* type
    that just can't be used here (there is nowhere to type the passphrase), and
    PasswordRequiredException subclasses SSHException — so it used to be swallowed
    by the generic handler and reported as unsupported or invalid."""

    @staticmethod
    def _pem(passphrase=None):
        """A real generated key, so paramiko's own parser decides the outcome."""
        buf = io.StringIO()
        paramiko.RSAKey.generate(2048).write_private_key(buf, password=passphrase)
        return buf.getvalue()

    def test_an_unencrypted_key_loads(self):
        assert sftp_upload._load_private_key(self._pem()) is not None

    def test_an_encrypted_key_says_so(self):
        with pytest.raises(paramiko.SSHException) as exc:
            sftp_upload._load_private_key(self._pem(passphrase='hunter2'))
        assert 'passphrase-protected' in str(exc.value)

    def test_junk_is_still_reported_as_invalid(self):
        with pytest.raises(paramiko.SSHException) as exc:
            sftp_upload._load_private_key('not a key at all')
        assert 'Unsupported or invalid' in str(exc.value)


class TestUnsavedFormValues:
    def test_the_test_button_does_not_pin_anything(self, fake_client, target):
        """publish_target_test builds a PublishConfig with no subdomain precisely so
        unsaved form values can't write a host key onto the stored target."""
        sftp_upload._connect(_cfg(subdomain=''))
        target.refresh_from_db()
        assert target.host_key == ''

    def test_a_failure_to_record_does_not_fail_the_connection(self, fake_client, target,
                                                              monkeypatch):
        def boom(*a, **kw):
            raise RuntimeError('database went away')

        monkeypatch.setattr(sftp_upload, '_remember_host_key', boom)
        # Publishing must still go ahead; the next connect just learns it again.
        assert sftp_upload._connect(_cfg()) is fake_client


class TestOnlyConfiguredCredentialsAreOffered:
    """paramiko's allow_agent and look_for_keys default to True, so a publish would
    offer the process's own SSH identities to whichever host the organizer named.
    The form asks for a password or a private key; those are the only credentials
    that may be used."""

    def test_the_ambient_ssh_identities_are_not_offered(self, fake_client, target):
        sftp_upload._connect(_cfg())
        assert fake_client.connect_kwargs['allow_agent'] is False
        assert fake_client.connect_kwargs['look_for_keys'] is False

    def test_the_configured_credentials_still_are(self, fake_client, target):
        sftp_upload._connect(_cfg(password='formpw'))
        kw = fake_client.connect_kwargs
        assert kw['hostname'] == 'example.org'
        assert kw['username'] == 'u'
        assert kw['password'] == 'formpw'
