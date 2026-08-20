"""Score-sheet OCR worker.

Consumes the Redis scan queue and runs the heavy OpenCV alignment + LLM OCR one
job at a time, writing the result back for the web tier to poll. Run one or more
of these alongside the web service (compose runs 2 replicas):

    python manage.py scan_worker

Keeping OCR here, off the gunicorn request workers, means a burst of parallel
scans can never starve score entry or the public displays.
"""
import logging
import signal
import time

import redis
from django.core.management.base import BaseCommand

from mahj import scan_queue
from mahj.views import scan as scanview

logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = "Consume the score-sheet OCR queue (run one or more)."

    def handle(self, *args, **options):
        self._running = True

        def _stop(signum, frame):
            self._running = False
        signal.signal(signal.SIGTERM, _stop)
        signal.signal(signal.SIGINT, _stop)

        # Build the ORB template once up front; fail loudly now if it's missing
        # rather than on the first job.
        scanview._ensure_initialized()
        self.stdout.write(self.style.SUCCESS("scan_worker ready; waiting for jobs…"))

        while self._running:
            try:
                job = scan_queue.dequeue(timeout=5)
            except redis.RedisError:
                # A redis_bus blip (e.g. restart) raises out of blpop. Ride it out
                # instead of exiting the command: redis-py drops the dead connection
                # and reconnects on the next dequeue, so the worker self-heals.
                logger.warning("scan_worker: bus unavailable, retrying")
                time.sleep(1)
                continue
            if job is None:
                continue
            self._process(job)

        self.stdout.write("scan_worker shutting down.")

    def _process(self, job):
        job_id = job.get('job_id')
        if not job_id:
            return
        try:
            image = scanview._read_image(str(scan_queue.image_path(job_id)))
            result = scanview.run_scan(image)
        except Exception as e:                       # never let one bad job kill the loop
            logger.exception("scan job %s failed", job_id)
            result = {'status': 'error', 'error': f'Scan failed: {e}'}
        finally:
            scan_queue.discard_image(job_id)
        # The result carries the job's own round/table/tenant forward, so the web
        # tier can read the scan's target from the job rather than from a request
        # body it cannot trust.
        scan_queue.set_result(job_id, scan_queue.carry_job_facts(job, result))
        logger.info("scan job %s -> %s", job_id, result.get('status'))
