"""Redis-backed work queue for off-request score-sheet OCR.

The web request stages the uploaded image and enqueues a job, then returns
immediately; one or more `scan_worker` processes consume the queue and run the
heavy OpenCV alignment + LLM OCR one job at a time, writing the result back to
Redis for the client to poll. This keeps OCR entirely off the gunicorn request
workers, so a burst of parallel scans can never starve score entry or displays.

Image bytes are staged on the shared `captures/scan_jobs/` volume (not in Redis)
so the OCR payload doesn't bloat the Redis instance that also holds the cache,
sessions, and channel layer.
"""
import json
import uuid
from pathlib import Path

import redis
from django.conf import settings

QUEUE_KEY = 'scan:queue'
_RESULT_PREFIX = 'scan:result:'
RESULT_TTL = 600   # seconds a finished/pending result is retained for polling

JOBS_DIR = Path(__file__).resolve().parent / 'captures' / 'scan_jobs'

_client = None


def _redis():
    global _client
    if _client is None:
        # The work queue lives on the noeviction bus Redis, not the LRU cache, so a
        # queued/pending job can't be evicted out from under a waiting scan.
        _client = redis.from_url(settings.REDIS_BUS_URL, decode_responses=True)
    return _client


# ---- image staging --------------------------------------------------------

def stage_image(raw_bytes):
    """Persist uploaded image bytes to the shared jobs dir; return a job_id."""
    job_id = uuid.uuid4().hex
    JOBS_DIR.mkdir(parents=True, exist_ok=True)
    (JOBS_DIR / job_id).write_bytes(raw_bytes)
    return job_id


def image_path(job_id):
    return JOBS_DIR / job_id


def discard_image(job_id):
    try:
        image_path(job_id).unlink()
    except OSError:
        pass


# ---- queue + results ------------------------------------------------------

# What the server decided about a job, as opposed to what a client later claims.
# Carried into the result so scan_prefill can read the target off the job instead of
# trusting a request body — see the docstring there.
JOB_FACTS = ('round_nb', 'table_nb', 'subdomain')


def enqueue(job):
    """Push a job dict (must include 'job_id') and mark it pending."""
    pending = {'status': 'pending'}
    pending.update({k: job.get(k) for k in JOB_FACTS})
    set_result(job['job_id'], pending)
    _redis().rpush(QUEUE_KEY, json.dumps(job))


def carry_job_facts(job, result):
    """Copy the server-decided fields from `job` onto a worker's `result`.

    The worker builds the result from what it read off the image, which says nothing
    about which table the photo was for. These come from the staging request's URL,
    so they are the trustworthy half.
    """
    out = dict(result)
    out.update({k: job.get(k) for k in JOB_FACTS})
    return out


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
