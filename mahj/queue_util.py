"""Shared retry helper for the two Redis work queues.

`scan_queue` and `restore_queue` are separate modules with their own keys and
payloads, but both end a job by writing its result to Redis — and both do it *after*
the expensive, already-paid-for work is done. An unguarded write there loses the
answer to a momentary bus outage and takes the worker loop down with it, stalling
every job queued behind. The retry policy is the same for both, so it lives once.

redis is imported inside the function, not at module scope: `restore_queue` is
imported by the URLconf and the standalone build ships without redis, so anything it
imports has to stay importable there too.
"""
import time


def write_with_retry(write, attempts=3, delay=0.5):
    """Call `write`, retrying a `redis.RedisError` a few times. True if it landed.

    Returns False rather than raising when it truly can't be written, so a worker can
    log the loss and carry on to the next job instead of dying on this one.
    """
    import redis

    for attempt in range(attempts):
        try:
            write()
            return True
        except redis.RedisError:
            if attempt == attempts - 1:
                return False
            time.sleep(delay)
    return False
