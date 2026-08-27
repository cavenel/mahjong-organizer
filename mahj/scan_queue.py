"""Redis-backed work queue for off-request score-sheet OCR.

The web request stages the uploaded image and enqueues a job, then returns
immediately; one or more `scan_worker` processes consume the queue and run the
heavy OpenCV alignment + LLM OCR one job at a time, writing the result back to
Redis for the client to poll. This keeps OCR entirely off the gunicorn request
workers, so a burst of parallel scans can never starve score entry or displays.

Image bytes are staged on the shared `captures/scan_jobs/` volume (not in Redis)
so the OCR payload doesn't bloat the Redis instance that also holds the cache,
sessions, and channel layer.

A job with `preview: True` is the Scanning admin page's alignment test: the
worker aligns and crops it exactly as it would a real scan, but makes no API
call, so an organiser can prove their sheet template works before the venue.
It rides this queue rather than running inline so it exercises the same path a
real scan takes — and so OpenCV stays off the request workers.
"""
import json
import time
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


# Staged images are unlinked when a worker picks the job up, so the only files left
# behind are from jobs whose worker died mid-run, or that were never dequeued because
# the queue was lost. A little older than the result TTL: past that the job can't be
# polled or written any more, so its image is certainly dead.
STALE_IMAGE_AGE_S = RESULT_TTL * 2


def sweep_stale_images(now=None):
    """Delete staged images older than STALE_IMAGE_AGE_S. Returns how many went.

    Called opportunistically when a new image is staged, so the directory is tidied by
    the traffic that dirties it and there's no cron to forget. Anonymous uploads land
    here, so without this a run of failed jobs grows the volume until it's full.
    """
    import time
    cutoff = (now if now is not None else time.time()) - STALE_IMAGE_AGE_S
    removed = 0
    try:
        entries = list(JOBS_DIR.iterdir())
    except OSError:
        return 0
    for path in entries:
        try:
            if path.is_file() and path.stat().st_mtime < cutoff:
                path.unlink()
                removed += 1
        except OSError:
            continue        # raced with a worker, or not ours to remove
    return removed


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
    """Block up to `timeout` seconds for the next job (FIFO). None on timeout.

    Refreshes the job's pending marker as it hands the job over: the marker is what
    the client polls, and it was set with RESULT_TTL when the job was *enqueued*. A
    backlog longer than that TTL meant the marker expired while the job was still
    waiting, so the client was told "timed out or lost" for a scan that then completed
    anyway. Restarting the clock at the moment work begins is what the TTL was for.
    """
    item = _redis().blpop(QUEUE_KEY, timeout=timeout)
    if item is None:
        return None
    job = json.loads(item[1])
    job_id = job.get('job_id')
    if job_id:
        try:
            _redis().expire(_RESULT_PREFIX + job_id, RESULT_TTL)
        except redis.RedisError:
            pass        # the result write below is what actually matters
    return job


def set_result(job_id, result):
    _redis().set(_RESULT_PREFIX + job_id, json.dumps(result), ex=RESULT_TTL)


def write_with_retry(write, attempts=3, delay=0.5):
    """Call `write`, retrying a `redis.RedisError` a few times. True if it landed.

    Returns False rather than raising when it truly can't be written, so a worker
    can log the loss and carry on to the next job instead of dying on this one.
    """
    for attempt in range(attempts):
        try:
            write()
            return True
        except redis.RedisError:
            if attempt == attempts - 1:
                return False
            time.sleep(delay)
    return False


def set_result_with_retry(job_id, result):
    """``set_result``, retried across a brief Redis outage. True if it landed.

    The OCR call is already paid for by the time a worker gets here, so losing the
    answer to a bus restart is the expensive failure — an unguarded write would
    also take the worker loop down with it, stalling every job queued behind.
    """
    return write_with_retry(lambda: set_result(job_id, result))


def get_result(job_id):
    raw = _redis().get(_RESULT_PREFIX + job_id)
    return json.loads(raw) if raw else None
