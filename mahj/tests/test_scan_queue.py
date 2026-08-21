"""The scan work queue and its worker loop.

Both sit on the live-scoring path — a photographed score sheet reaches the score
grid through them — and both were largely untested, because everything here talks
to Redis and the suite has none. A fake client covers that: the module's whole job
is the *protocol* it speaks to Redis (which key, which TTL, what shape), so a fake
that records commands tests the part that can actually be wrong.

What matters at an event is the failure behaviour: a job whose pending marker
expired while it queued, a bus that drops out at the moment the answer comes back,
one bad photo taking the worker down and stalling every scan behind it.
"""
import json

import pytest
import redis

from mahj import scan_queue


class FakeRedis:
    """Enough of redis-py for scan_queue: a dict, a list, and recorded TTLs."""

    def __init__(self, fail_writes=0):
        self.store = {}
        self.queue = []
        self.ttls = {}
        self.expire_calls = []
        self.fail_writes = fail_writes      # how many set() calls to fail first
        self.set_calls = 0

    def set(self, key, value, ex=None):
        self.set_calls += 1
        if self.set_calls <= self.fail_writes:
            raise redis.ConnectionError('bus down')
        self.store[key] = value
        self.ttls[key] = ex

    def get(self, key):
        return self.store.get(key)

    def rpush(self, key, value):
        self.queue.append(value)

    def blpop(self, key, timeout=None):
        if not self.queue:
            return None
        return (key, self.queue.pop(0))

    def expire(self, key, ttl):
        self.expire_calls.append((key, ttl))
        if key in self.store:
            self.ttls[key] = ttl
            return True
        return False


@pytest.fixture
def bus(monkeypatch):
    fake = FakeRedis()
    monkeypatch.setattr(scan_queue, '_redis', lambda: fake)
    return fake


class TestEnqueueAndPoll:
    def test_a_queued_job_is_immediately_pollable_as_pending(self, bus):
        """The client starts polling the moment it gets a job_id, so the pending
        marker has to exist before the worker has touched anything — otherwise the
        first poll reads as "timed out or lost"."""
        scan_queue.enqueue({'job_id': 'j1', 'round_nb': 2, 'table_nb': 3,
                            'subdomain': 'test'})
        assert scan_queue.get_result('j1') == {
            'status': 'pending', 'round_nb': 2, 'table_nb': 3, 'subdomain': 'test'}

    def test_the_pending_marker_carries_the_server_decided_facts(self, bus):
        """These are what scan_prefill trusts instead of the request body, so they
        must be on the marker from the start rather than added by the worker."""
        scan_queue.enqueue({'job_id': 'j2', 'round_nb': 1, 'table_nb': 1,
                            'subdomain': 'other'})
        marker = scan_queue.get_result('j2')
        for fact in scan_queue.JOB_FACTS:
            assert fact in marker

    def test_the_job_itself_reaches_the_queue(self, bus):
        scan_queue.enqueue({'job_id': 'j3', 'round_nb': 4, 'table_nb': 2,
                            'subdomain': 'test'})
        assert json.loads(bus.queue[0])['job_id'] == 'j3'

    def test_an_unknown_job_polls_as_nothing(self, bus):
        assert scan_queue.get_result('never-existed') is None


class TestDequeueRefreshesThePendingMarker:
    def test_picking_a_job_up_restarts_its_result_ttl(self, bus):
        """The marker is set with RESULT_TTL at enqueue. A backlog longer than that
        TTL meant it expired while the job was still waiting, and the client was
        told the scan was lost for a scan that then completed."""
        scan_queue.enqueue({'job_id': 'j4', 'subdomain': 'test'})
        job = scan_queue.dequeue()
        assert job['job_id'] == 'j4'
        assert (scan_queue._RESULT_PREFIX + 'j4',
                scan_queue.RESULT_TTL) in bus.expire_calls

    def test_an_empty_queue_returns_none(self, bus):
        assert scan_queue.dequeue() is None

    def test_a_failed_refresh_does_not_lose_the_job(self, bus, monkeypatch):
        """The refresh is a nicety; the result write is what matters. A bus hiccup
        here must not drop a job whose photo has already been taken."""
        scan_queue.enqueue({'job_id': 'j5', 'subdomain': 'test'})

        def boom(*a, **kw):
            raise redis.ConnectionError('bus down')
        monkeypatch.setattr(bus, 'expire', boom)
        assert scan_queue.dequeue()['job_id'] == 'j5'


class TestResultWritesSurviveABusBlip:
    def test_a_transient_failure_is_retried(self, monkeypatch):
        fake = FakeRedis(fail_writes=2)
        monkeypatch.setattr(scan_queue, '_redis', lambda: fake)
        monkeypatch.setattr('mahj.queue_util.time.sleep', lambda s: None)
        assert scan_queue.set_result_with_retry('j6', {'status': 'done'}) is True
        assert scan_queue.get_result('j6') == {'status': 'done'}

    def test_a_persistent_failure_reports_rather_than_raises(self, monkeypatch):
        """The OCR call is already paid for. A worker that raised here would die on
        this job and stall every scan queued behind it."""
        fake = FakeRedis(fail_writes=99)
        monkeypatch.setattr(scan_queue, '_redis', lambda: fake)
        monkeypatch.setattr('mahj.queue_util.time.sleep', lambda s: None)
        assert scan_queue.set_result_with_retry('j7', {'status': 'done'}) is False


class TestJobFactsAreServerDecided:
    def test_they_overwrite_whatever_the_worker_read(self, bus):
        """A worker's result describes the image; it cannot know which table the
        photo was of. If the OCR ever returned these keys they must not win."""
        job = {'job_id': 'j8', 'round_nb': 3, 'table_nb': 4, 'subdomain': 'test'}
        merged = scan_queue.carry_job_facts(
            job, {'status': 'done', 'scores': [1], 'round_nb': 99,
                  'table_nb': 99, 'subdomain': 'attacker'})
        assert (merged['round_nb'], merged['table_nb']) == (3, 4)
        assert merged['subdomain'] == 'test'
        assert merged['scores'] == [1]          # the worker's own output is kept

    def test_it_does_not_mutate_the_result_it_was_given(self, bus):
        result = {'status': 'done'}
        scan_queue.carry_job_facts({'job_id': 'j9', 'subdomain': 'test'}, result)
        assert result == {'status': 'done'}


class TestImageStaging:
    def test_staging_writes_the_bytes_and_returns_a_usable_id(self, tmp_path,
                                                              monkeypatch):
        monkeypatch.setattr(scan_queue, 'JOBS_DIR', tmp_path / 'jobs')
        job_id = scan_queue.stage_image(b'\x89PNG-ish')
        assert scan_queue.image_path(job_id).read_bytes() == b'\x89PNG-ish'

    def test_discarding_is_idempotent(self, tmp_path, monkeypatch):
        """The worker discards after reading, and the stale sweep may have got there
        first — so a second discard must not raise into the worker loop."""
        monkeypatch.setattr(scan_queue, 'JOBS_DIR', tmp_path / 'jobs')
        job_id = scan_queue.stage_image(b'x')
        scan_queue.discard_image(job_id)
        scan_queue.discard_image(job_id)        # no exception
        assert not scan_queue.image_path(job_id).exists()


# ---------------------------------------------------------------------------
# The worker loop. Its whole job is to survive: one unreadable photo, or a bus
# restart, must not end the process and stall every scan queued behind it. The
# heavy OpenCV/LLM work is stubbed — this is about the loop, not the OCR.
# ---------------------------------------------------------------------------

class TestWorkerLoop:

    @pytest.fixture
    def worker(self, monkeypatch):
        from mahj.management.commands import scan_worker as mod
        monkeypatch.setattr(mod.scanview, '_ensure_initialized', lambda: None)
        cmd = mod.Command()
        return cmd, mod

    def _drain(self, cmd, mod, jobs, monkeypatch):
        """Run the loop over a fixed list of dequeue results, then stop."""
        seq = list(jobs)

        def fake_dequeue(timeout=5):
            if not seq:
                cmd._running = False
                return None
            item = seq.pop(0)
            if isinstance(item, Exception):
                raise item
            return item
        monkeypatch.setattr(mod.scan_queue, 'dequeue', fake_dequeue)
        monkeypatch.setattr(mod.time, 'sleep', lambda s: None)
        cmd.handle()

    def test_a_bus_outage_does_not_end_the_worker(self, worker, monkeypatch, tmp_path):
        """A redis_bus restart raises out of blpop. Exiting here would take the
        worker down for good, and compose would restart it into the same blip."""
        cmd, mod = worker
        processed = []
        monkeypatch.setattr(cmd, '_process', processed.append)
        self._drain(cmd, mod, [redis.ConnectionError('bus down'),
                               {'job_id': 'after-the-blip'}], monkeypatch)
        assert processed == [{'job_id': 'after-the-blip'}]

    def test_an_idle_poll_is_not_treated_as_a_job(self, worker, monkeypatch):
        cmd, mod = worker
        processed = []
        monkeypatch.setattr(cmd, '_process', processed.append)
        self._drain(cmd, mod, [None, {'job_id': 'real'}], monkeypatch)
        assert processed == [{'job_id': 'real'}]

    def test_one_unreadable_photo_does_not_kill_the_loop(self, worker, monkeypatch,
                                                         tmp_path):
        """The failure that matters at an event: a bad job must become an error
        result for that scan, not a dead worker and a stalled queue."""
        cmd, mod = worker
        monkeypatch.setattr(mod.scan_queue, 'JOBS_DIR', tmp_path)
        monkeypatch.setattr(mod.scanview, '_read_image',
                            lambda p: (_ for _ in ()).throw(ValueError('garbled')))
        written = {}
        monkeypatch.setattr(mod.scan_queue, 'set_result_with_retry',
                            lambda jid, res: written.update({jid: res}) or True)
        cmd._process({'job_id': 'bad', 'round_nb': 1, 'table_nb': 1,
                      'subdomain': 'test'})
        assert written['bad']['status'] == 'error'
        assert 'garbled' in written['bad']['error']
        # And the job facts still ride along, so the client's poll is answerable.
        assert written['bad']['subdomain'] == 'test'

    def test_a_job_without_an_id_is_skipped(self, worker, monkeypatch):
        cmd, mod = worker
        called = []
        monkeypatch.setattr(mod.scan_queue, 'set_result_with_retry',
                            lambda *a: called.append(a) or True)
        cmd._process({'round_nb': 1})
        assert called == []

    def test_a_result_that_cannot_be_stored_is_logged_not_raised(self, worker,
                                                                 monkeypatch,
                                                                 tmp_path, caplog):
        """The OCR call is already paid for; losing the answer must not also lose
        the worker."""
        cmd, mod = worker
        monkeypatch.setattr(mod.scan_queue, 'JOBS_DIR', tmp_path)
        monkeypatch.setattr(mod.scanview, '_read_image', lambda p: object())
        monkeypatch.setattr(mod.scanview, 'run_scan',
                            lambda img: {'status': 'done', 'scores': []})
        monkeypatch.setattr(mod.scan_queue, 'set_result_with_retry',
                            lambda jid, res: False)
        cmd._process({'job_id': 'lost', 'subdomain': 'test'})
        assert 'could not be stored' in caplog.text

    def test_the_staged_image_is_discarded_even_when_the_scan_fails(self, worker,
                                                                    monkeypatch,
                                                                    tmp_path):
        """Anonymous uploads land in the shared jobs dir; a run of failures that
        left them there would grow the volume until it filled."""
        cmd, mod = worker
        monkeypatch.setattr(mod.scan_queue, 'JOBS_DIR', tmp_path)
        job_id = scan_queue.stage_image(b'x')
        monkeypatch.setattr(mod.scanview, '_read_image',
                            lambda p: (_ for _ in ()).throw(ValueError('nope')))
        monkeypatch.setattr(mod.scan_queue, 'set_result_with_retry',
                            lambda jid, res: True)
        cmd._process({'job_id': job_id, 'subdomain': 'test'})
        assert not (tmp_path / job_id).exists()


# ---------------------------------------------------------------------------
# Request-handling paths in views/scan.py that don't need the imaging stack.
# The OCR itself (cv2 / numpy / PIL / anthropic) can only run where those are
# installed — CI and the Docker image — so what is testable here is the parsing
# and the parameter handling around it.
# ---------------------------------------------------------------------------

class TestOcrResponseParsing:
    def test_the_first_text_block_is_used_not_block_zero(self):
        """The model may put a non-text block first; assuming index 0 raised."""
        from mahj.views.scan import _first_text

        class Block:
            def __init__(self, type_, text=None):
                self.type = type_
                self.text = text

        msg = type('M', (), {'content': [Block('thinking'), Block('text', '{"a":1}')]})()
        assert _first_text(msg) == '{"a":1}'

    def test_a_response_with_no_text_block_is_an_error(self):
        """Better than returning None into json.loads, which would blame the
        parser for the model's answer."""
        from mahj.views.scan import _first_text
        msg = type('M', (), {'content': []})()
        with pytest.raises(ValueError, match='no text block'):
            _first_text(msg)


class TestScanSeatsParameters:
    """`scan_seats` is a public endpoint reading raw query parameters, so a
    non-numeric one is a routine event rather than a 500."""

    @pytest.mark.parametrize('qs', [
        '', '?round_nb=1', '?table_nb=1', '?round_nb=x&table_nb=1',
        '?round_nb=1&table_nb=', '?round_nb=1.5&table_nb=1',
    ])
    def test_a_missing_or_unparseable_parameter_is_a_400(self, client_, tournament, qs):
        from django.test import override_settings
        with override_settings(SCAN_ENABLED=True):
            resp = client_.get(f'/scan_seats{qs}')
        assert resp.status_code == 400
        assert resp.json()['ok'] is False

    def test_a_real_round_and_table_returns_its_seats(self, client_, tournament):
        from django.test import override_settings
        with override_settings(SCAN_ENABLED=True):
            resp = client_.get('/scan_seats?round_nb=1&table_nb=1')
        assert resp.status_code == 200
        body = resp.json()
        assert body['ok'] is True
        assert len(body['seats']) == 4
