"""Database restore / pull worker.

Consumes the Redis ``restore`` queue and performs the destructive work the web
tier can't: a request that stops its own container dies mid-restore, so this runs
in a dedicated service instead (a twin of ``scan_worker``). Run one:

    python manage.py restore_worker

A **restore** never bounces containers and never touches the Docker socket. It
pauses the pgbouncer pool, drops/recreates the DB directly on the ``db`` service
(bypassing the paused pooler), runs ``pg_restore`` + ``migrate``, then resumes the
pool — so ``web`` stays up and clients merely stall for the few seconds it takes.

A **pull** rsyncs the off-host dumps down into ``/backups`` (non-destructive).
"""
import logging
import os
import signal
import subprocess
import sys
import time

import redis
from django.conf import settings
from django.core.management.base import BaseCommand

from mahj import restore_queue

logger = logging.getLogger(__name__)

# The Postgres service is reached directly (not via the pooler) for the
# drop/recreate/restore; the pooler is reached only for PAUSE/RESUME. Service
# names match docker-compose; overridable for non-standard stacks.
DB_DIRECT_HOST = os.environ.get('RESTORE_DB_HOST', 'db')
PGBOUNCER_HOST = os.environ.get('RESTORE_PGBOUNCER_HOST', 'pgbouncer')
DB_PORT = os.environ.get('DB_PORT', '5432')

# Core scoring tables for the post-restore sanity counts (double-quoted for
# consistency with scripts/restore_db.sh).
_COUNT_SQL = (
    'SELECT '
    '(SELECT count(*) FROM "mahj_player") AS players, '
    '(SELECT count(*) FROM "mahj_seat")   AS seats, '
    '(SELECT count(*) FROM "mahj_hand")   AS hands;'
)


class Command(BaseCommand):
    help = "Consume the database restore/pull queue (run exactly one)."

    def handle(self, *args, **options):
        self._running = True

        def _stop(signum, frame):
            self._running = False
        signal.signal(signal.SIGTERM, _stop)
        signal.signal(signal.SIGINT, _stop)

        self.stdout.write(self.style.SUCCESS("restore_worker ready; waiting for jobs…"))

        while self._running:
            try:
                job = restore_queue.dequeue(timeout=5)
            except redis.RedisError:
                # Ride out a redis_bus blip exactly like scan_worker: redis-py drops
                # the dead connection and reconnects on the next dequeue.
                logger.warning("restore_worker: bus unavailable, retrying")
                time.sleep(1)
                continue
            if job is None:
                continue
            self._process(job)

        self.stdout.write("restore_worker shutting down.")

    # -- dispatch ------------------------------------------------------------

    def _process(self, job):
        job_id = job.get('job_id')
        if not job_id:
            return
        action = job.get('action')
        try:
            if action == 'restore':
                self._restore(job_id, job.get('dump', ''), job.get('session_key'))
            elif action == 'pull':
                self._pull(job_id)
            else:
                restore_queue.set_result(job_id, {
                    'status': 'error', 'phase': 'done',
                    'error': f'Unknown action: {action!r}'})
        except Exception as e:                       # never let one bad job kill the loop
            logger.exception("restore job %s (%s) failed", job_id, action)
            restore_queue.set_result(job_id, {
                'status': 'error', 'phase': 'done', 'error': str(e)})

    def _set(self, job_id, status, phase, **extra):
        """Publish a progress update, retried and never fatal.

        Every update the restore page shows comes through here, including the final
        verdict. An unguarded write meant a bus blip mid-restore could take the worker
        down between dropping the database and finishing it — leaving the operator
        watching a page that never changes while their data is half-replaced. The
        restore itself must outlive a failure to *talk about* the restore.
        """
        result = {'status': status, 'phase': phase}
        result.update(extra)
        if not restore_queue.set_result_with_retry(job_id, result):
            logger.error("restore job %s reached %s/%s but the update could not be "
                         "stored; the page will look stalled", job_id, status, phase)
        logger.info("restore job %s -> %s/%s", job_id, status, phase)

    # -- restore -------------------------------------------------------------

    def _restore(self, job_id, dump_name, session_key=None):
        db = settings.DATABASES['default']
        name, user, password = db['NAME'], db['USER'], db['PASSWORD']

        dump_path = self._validate_dump(dump_name)
        env = dict(os.environ, PGPASSWORD=password)

        # The restore replaces the whole DB — including django_session — so it would
        # log out the admin who triggered it, and their completion poll would then
        # hit the login redirect and hang. Grab their session row now (DB still
        # intact) and re-insert it after the restore so they stay signed in.
        saved_session = self._read_session(name, user, password, session_key)

        self._set(job_id, 'running', 'pausing')
        paused = False
        try:
            # Block new writes: PAUSE waits for in-flight queries to finish, then
            # disconnects server connections, so the DROP below has a clear field.
            self._psql_admin('PAUSE %s;' % name, user, env)
            paused = True

            self._set(job_id, 'running', 'restoring')
            self._recreate_db(name, user, password)
            self._pg_restore(dump_path, name, user, env)

            # Bring an older dump up to the current schema, against the db service
            # directly since the pooler is still paused (web does this on boot).
            self._set(job_id, 'running', 'migrating')
            self._migrate(env)

            # Re-insert the triggering admin's session so the poll can still read
            # the result (and they stay logged in) after the DB swap.
            self._restore_session(name, user, password, session_key, saved_session)
        finally:
            # Always resume, even on failure: a paused pool would otherwise wedge
            # the whole live site behind a half-done restore.
            if paused:
                try:
                    self._psql_admin('RESUME %s;' % name, user, env)
                except Exception:
                    logger.exception("restore job %s: RESUME failed", job_id)

        counts = self._row_counts(name, user, password)
        self._set(job_id, 'done', 'done', dump=dump_name, counts=counts)

    def _validate_dump(self, dump_name):
        """Defence-in-depth: the view already checks this, but never trust the
        queue payload. Basename only (no path traversal), present in the backups
        dir, and carrying the pg_dump custom-format magic header."""
        if not dump_name or dump_name != os.path.basename(dump_name):
            raise ValueError("Invalid dump name")
        path = restore_queue.BACKUPS_DIR / dump_name
        if not path.is_file():
            raise ValueError(f"Dump not found: {dump_name}")
        with open(path, 'rb') as fh:
            if fh.read(5) != b'PGDMP':
                raise ValueError(f"Not a valid pg_dump file: {dump_name}")
        return path

    def _recreate_db(self, name, user, password):
        import psycopg2
        from psycopg2 import sql
        # Connect to the maintenance DB on the db service directly — you can't drop
        # the database you're connected to, and this bypasses the paused pooler.
        conn = psycopg2.connect(host=DB_DIRECT_HOST, port=DB_PORT, dbname='postgres',
                                user=user, password=password)
        try:
            conn.autocommit = True
            with conn.cursor() as cur:
                # WITH (FORCE) terminates any straggler sessions itself (PG13+).
                cur.execute(sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)")
                            .format(sql.Identifier(name)))
                cur.execute(sql.SQL("CREATE DATABASE {} OWNER {}")
                            .format(sql.Identifier(name), sql.Identifier(user)))
        finally:
            conn.close()

    def _pg_restore(self, dump_path, name, user, env):
        # --no-owner/--no-acl so the dump restores cleanly regardless of role names.
        self._run([
            'pg_restore', '-h', DB_DIRECT_HOST, '-p', DB_PORT, '-U', user,
            '-d', name, '--no-owner', '--no-acl', str(dump_path),
        ], env)

    def _migrate(self, env):
        # Force the migrate subprocess onto the db service directly (pooler paused).
        env = dict(env, DB_HOST=DB_DIRECT_HOST, DB_PORT=DB_PORT)
        self._run([sys.executable, 'manage.py', 'migrate', '--noinput'], env)

    def _psql_admin(self, command, user, env):
        # pgbouncer's admin console speaks the simple query protocol; psql -c uses
        # it (psycopg2's extended protocol would be rejected). Connects to the
        # virtual 'pgbouncer' database.
        self._run([
            'psql', '-h', PGBOUNCER_HOST, '-p', DB_PORT, '-U', user,
            '-d', 'pgbouncer', '-c', command,
        ], env)

    def _read_session(self, name, user, password, session_key):
        """Read one django_session row (the admin who triggered the restore) from
        the live DB, before it's dropped. None if we can't — we just proceed and
        they'll be logged out, which is the pre-fix behaviour."""
        if not session_key:
            return None
        import psycopg2
        try:
            conn = psycopg2.connect(host=DB_DIRECT_HOST, port=DB_PORT, dbname=name,
                                    user=user, password=password)
            try:
                with conn.cursor() as cur:
                    cur.execute("SELECT session_data, expire_date FROM django_session "
                                "WHERE session_key = %s", (session_key,))
                    return cur.fetchone()
            finally:
                conn.close()
        except Exception:
            logger.exception("restore: could not read admin session (they may be logged out)")
            return None

    def _restore_session(self, name, user, password, session_key, saved):
        """Re-insert the admin's saved session row into the restored DB so their
        cookie still authenticates. Best-effort: on failure they just re-login."""
        if not session_key or not saved:
            return
        import psycopg2
        try:
            conn = psycopg2.connect(host=DB_DIRECT_HOST, port=DB_PORT, dbname=name,
                                    user=user, password=password)
            try:
                conn.autocommit = True
                with conn.cursor() as cur:
                    cur.execute(
                        "INSERT INTO django_session (session_key, session_data, expire_date) "
                        "VALUES (%s, %s, %s) ON CONFLICT (session_key) DO UPDATE "
                        "SET session_data = EXCLUDED.session_data, expire_date = EXCLUDED.expire_date",
                        (session_key, saved[0], saved[1]))
            finally:
                conn.close()
        except Exception:
            logger.exception("restore: could not re-insert admin session (they'll need to re-login)")

    def _row_counts(self, name, user, password):
        import psycopg2
        try:
            conn = psycopg2.connect(host=DB_DIRECT_HOST, port=DB_PORT, dbname=name,
                                    user=user, password=password)
            try:
                with conn.cursor() as cur:
                    cur.execute(_COUNT_SQL)
                    players, seats, hands = cur.fetchone()
                    return {'players': players, 'seats': seats, 'hands': hands}
            finally:
                conn.close()
        except Exception:
            logger.exception("restore: row-count probe failed")
            return None

    # -- pull ----------------------------------------------------------------

    def _pull(self, job_id):
        remote = os.environ.get('REMOTE', '').strip()
        ssh_key = os.environ.get('SSH_KEY', '').strip()
        ssh_port = os.environ.get('SSH_PORT', '22').strip() or '22'
        if not remote or not ssh_key or not os.path.isfile(ssh_key):
            raise ValueError("Pull is not configured (set BACKUP_REMOTE / "
                             "MAHJ_BACKUP_SSH_KEY in the environment).")

        self._set(job_id, 'running', 'pulling')
        restore_queue.BACKUPS_DIR.mkdir(parents=True, exist_ok=True)
        # UserKnownHostsFile=/dev/null: don't require a writable $HOME/.ssh (the
        # container may run as a uid without a home dir); accept-new still avoids
        # an interactive prompt on first contact.
        ssh = (f"ssh -i {ssh_key} -p {ssh_port} "
               "-o StrictHostKeyChecking=accept-new -o BatchMode=yes "
               "-o UserKnownHostsFile=/dev/null")
        # Trailing slashes copy the CONTENTS of the remote dir down. Read-only
        # (no --delete) so we can never harm the off-host copy. -tz keeps file
        # mtimes (so the page's "newest" ordering stays correct) + compresses.
        # We deliberately DON'T preserve owner/group/perms or set dir times: the
        # worker isn't root and doesn't own the mounted backups dir, so any attempt
        # to chown/chgrp/chmod it (which -a does) fails with EPERM. The local copies
        # only need to be readable, which the default (umask 644) already gives.
        self._run([
            'rsync', '-rtz', '--no-owner', '--no-group', '--no-perms',
            '--omit-dir-times', '-e', ssh,
            f"{remote}/", f"{restore_queue.BACKUPS_DIR}/",
        ], dict(os.environ))
        held = len(list(restore_queue.BACKUPS_DIR.glob('mahj_*.dump')))
        self._set(job_id, 'done', 'done', held=held)

    # -- subprocess ----------------------------------------------------------

    def _run(self, cmd, env):
        proc = subprocess.run(cmd, env=env, stdout=subprocess.PIPE,
                              stderr=subprocess.STDOUT, text=True)
        if proc.returncode != 0:
            # Surface a trimmed tail of the command output to the operator.
            tail = (proc.stdout or '').strip().splitlines()[-5:]
            raise RuntimeError(f"{cmd[0]} failed (exit {proc.returncode}): "
                               + " / ".join(tail))
