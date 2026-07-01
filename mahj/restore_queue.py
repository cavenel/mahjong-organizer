"""Redis-backed work queue for admin-triggered database restore / pull jobs.

The admin console enqueues a job (restore a dump, or pull dumps from the off-host
remote) and returns immediately; the single ``restore_worker`` process consumes
the queue and does the heavy/destructive work, writing a result back to Redis for
the page to poll. This keeps the destructive `stop-the-pool / drop / pg_restore`
dance OFF the gunicorn request workers — a request can't tear down its own
container, and a restore must not block the event loop.

Mirrors ``mahj.scan_queue``: same noeviction bus Redis, same FIFO + polled-result
shape. The job dict carries an ``action`` (``restore`` | ``pull``); a restore also
carries the dump ``basename`` to restore.
"""
import json
import os
import uuid
from pathlib import Path

import redis
from django.conf import settings

QUEUE_KEY = 'restore:queue'
_RESULT_PREFIX = 'restore:result:'
# Restores can take a little while and the operator watches the page the whole
# time, so keep results longer than the scan queue's 10-min window.
RESULT_TTL = 3600

# Container path the host backups dir is mounted at (see docker-compose). Override
# via env so tests (which run outside the container) can point it elsewhere.
BACKUPS_DIR = Path(os.environ.get('MAHJ_BACKUPS_DIR', '/backups'))

_client = None


def _redis():
    global _client
    if _client is None:
        # Same noeviction bus as the scan queue: a queued restore must never be
        # evicted out from under a waiting operator.
        _client = redis.from_url(settings.REDIS_BUS_URL, decode_responses=True)
    return _client


def new_job_id():
    return uuid.uuid4().hex


def enqueue(job):
    """Push a job dict (must include 'job_id' and 'action') and mark it pending."""
    set_result(job['job_id'], {'status': 'pending', 'phase': 'queued'})
    _redis().rpush(QUEUE_KEY, json.dumps(job))


def dequeue(timeout=5):
    """Block up to `timeout` seconds for the next job (FIFO). None on timeout."""
    item = _redis().blpop(QUEUE_KEY, timeout=timeout)
    if item is None:
        return None
    return json.loads(item[1])


def set_result(job_id, result):
    _redis().set(_RESULT_PREFIX + job_id, json.dumps(result), ex=RESULT_TTL)


def get_result(job_id):
    raw = _redis().get(_RESULT_PREFIX + job_id)
    return json.loads(raw) if raw else None
