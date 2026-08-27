"""Score-sheet OCR worker.

Consumes the Redis scan queue and runs the heavy OpenCV alignment + LLM OCR one
job at a time, writing the result back for the web tier to poll. Run one or more
of these alongside the web service (compose runs 4 replicas):

    python manage.py scan_worker

Keeping OCR here, off the gunicorn request workers, means a burst of parallel
scans can never starve score entry or the public displays.
"""
import logging
import signal
import time

import redis
from django.core.management.base import BaseCommand

from mahj import scan_key, scan_queue
from mahj.views import scan as scanview

logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = "Consume the score-sheet OCR queue (run one or more)."

    @staticmethod
    def _record(subdomain, result):
        """Surface a failure on the tenant's own Scanning page.

        An organiser is not reading this log. A revoked key, a rate-limited new
        account and a sheet no photo matches are otherwise invisible to them until
        someone at the venue complains — this is the whole diagnosis path.
        """
        kind = result.get('kind')
        if kind is None or result.get('status') != 'error':
            return
        if kind == 'auth':
            # Close the revocation loop within this job, rather than accepting and
            # failing every upload for the rest of the cache TTL.
            scan_key.forget(subdomain)
            scan_key.stamp_error(subdomain, "The API key was rejected. Check it, or "
                                            "replace it with a new one.")
        elif kind == 'rate':
            scan_key.stamp_error(
                subdomain, "The API account hit its rate limit. New accounts start on "
                           "the lowest limit. Check the limits and the credit on the "
                           "account in the Anthropic console.")
        elif kind == 'model':
            scan_key.stamp_error(subdomain, "This key cannot use the model that scanning "
                                            "needs.")
        elif kind == 'align':
            scan_key.stamp_error(
                subdomain, "A photo could not be matched to the score sheet. If this "
                           "keeps happening, check that the uploaded sheet is the one "
                           "your tables use.")

    def handle(self, *args, **options):
        self._running = True

        def _stop(signum, frame):
            self._running = False
        signal.signal(signal.SIGTERM, _stop)
        signal.signal(signal.SIGINT, _stop)

        # Nothing to preload: every sheet is a tenant's own, built from its stored
        # bytes on first use. A bad upload can therefore only fail that tenant's
        # job — one tournament's mistake must not take down a worker the whole
        # queue shares.
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
        subdomain = job.get('subdomain') or ''
        try:
            # Before _read_image, so a job for a tenant that cannot scan skips the
            # OpenCV pass entirely. Reaching here with no key is the enqueue/dequeue
            # race (the key was cleared while the photo was in the queue) — the
            # request path already refuses these.
            setup = scan_key.resolve_setup(subdomain)
            if not job.get('preview') and not setup.can_scan:
                result = {'status': 'error', 'error': scanview.ERR_NO_KEY}
            else:
                image = scanview._read_image(str(scan_queue.image_path(job_id)))
                template = scanview.resolve_template(setup)
                if job.get('preview'):
                    # The admin's "Test alignment": same staging, same worker, same
                    # alignment as a real scan, and no API call at all.
                    result = scanview.run_preview(image, template)
                else:
                    result = scanview.run_scan(image, setup.key, template)
            # A rehearsal from the Scanning page is not stamped: the organiser is
            # looking straight at the result, and recording their own experiment
            # would leave a "photos could not be matched" warning on their page
            # about a photo they took on purpose to see what would happen.
            if not job.get('rehearsal'):
                self._record(subdomain, result)
        except Exception:                            # never let one bad job kill the loop
            logger.exception("scan job %s failed", job_id)
            # A fixed string: this used to interpolate the exception, which reached
            # an anonymous poller through scan_status.
            result = {'status': 'error', 'error': scanview.ERR_UNREADABLE}
        finally:
            scan_queue.discard_image(job_id)
        # The result carries the job's own round/table/tenant forward, so the web
        # tier can read the scan's target from the job rather than from a request
        # body it cannot trust.
        #
        # Retried, and never allowed to raise: the OCR call is already paid for by the
        # time we get here, so a bus blip at write time must not lose the answer — or
        # take the worker loop down with it and stall every queued job behind it.
        if not scan_queue.set_result_with_retry(
                job_id, scan_queue.carry_job_facts(job, result)):
            logger.error("scan job %s finished but its result could not be stored; "
                         "the client will see it as timed out", job_id)
            return
        logger.info("scan job %s -> %s", job_id, result.get('status'))
