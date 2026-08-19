"""The standalone launcher's first-run bootstrap (standalone/run.py).

The standalone build binds 0.0.0.0 so projectors and scorers' phones on the venue
LAN can reach it, so the admin account it creates on first run is reachable from
that whole network — a fixed default password would hand it to anyone on it.
"""
import stat

import pytest
from django.contrib.auth.models import User

from mahj.models import Tenant
from standalone.run import bootstrap


class TestFirstRunAdminPassword:
    def test_password_is_generated_not_a_known_default(self, db, tmp_path):
        password = bootstrap(tmp_path)
        assert password, 'bootstrap should return the password it generated'
        assert password != 'admin'
        assert len(password) >= 10

        admin = User.objects.get(username='admin')
        assert admin.is_superuser
        assert admin.check_password(password)
        # The old fixed default must not work.
        assert not admin.check_password('admin')

    def test_password_is_saved_for_the_operator_and_not_world_readable(self, db, tmp_path):
        """A console line scrolls away and the operator may come back later, so it
        is also on disk — but only readable by them."""
        password = bootstrap(tmp_path)
        note = tmp_path / 'first-login.txt'
        assert note.exists()
        assert password in note.read_text()
        assert stat.S_IMODE(note.stat().st_mode) == 0o600
        # And it tells them to clean up.
        assert 'delete' in note.read_text().lower()

    def test_second_run_creates_nothing_and_keeps_the_password(self, db, tmp_path):
        password = bootstrap(tmp_path)
        assert bootstrap(tmp_path) is None, 'must not re-create or rotate the admin'
        assert User.objects.filter(is_superuser=True).count() == 1
        assert User.objects.get(username='admin').check_password(password)

    def test_creates_the_local_tenant(self, db, tmp_path, monkeypatch):
        monkeypatch.setenv('LOCAL_TENANT', 'venue')
        bootstrap(tmp_path)
        assert Tenant.objects.filter(subdomain='venue').exists()

    def test_an_unwritable_data_dir_still_creates_the_admin(self, db, tmp_path):
        """The file is a convenience; losing it must not stop the app booting —
        the printed password is then the only copy."""
        locked = tmp_path / 'locked'
        locked.mkdir()
        locked.chmod(0o500)
        try:
            password = bootstrap(locked)
            assert password
            assert User.objects.get(username='admin').check_password(password)
        finally:
            locked.chmod(0o700)
