import json
import threading
from collections import defaultdict, deque
from types import SimpleNamespace
import urllib.error
import urllib.request

import pytest
import bridge

JOB = '11111111-1111-1111-1111-111111111111'


class Channel:
    def __init__(self):
        self.owner = threading.get_ident()
        self.messages = defaultdict(deque)
        self.acks, self.nacks, self.published = [], [], []
        self.failure = None

    def check(self):
        assert threading.get_ident() == self.owner, 'AMQP touched by HTTP worker'

    def put(self, queue, body, tag=1):
        self.messages[queue].append((SimpleNamespace(delivery_tag=tag), None, body))

    def basic_get(self, queue, auto_ack):
        self.check()
        return self.messages[queue].popleft() if self.messages[queue] else (None, None, None)

    def basic_ack(self, tag):
        self.check()
        self.acks.append(tag)

    def basic_nack(self, tag, requeue):
        self.check()
        self.nacks.append((tag, requeue))

    def basic_publish(self, **kw):
        self.check()
        if self.failure:
            raise self.failure
        self.published.append(kw)


class Connection:
    def __init__(self):
        self.ticks = 0
        self.closed = False

    def process_data_events(self, limit):
        self.ticks += 1

    def close(self):
        self.closed = True


class HeldHTTP:
    def __init__(self, result=True):
        self.entered, self.release = threading.Event(), threading.Event()
        self.calls = []
        self.result = result

    def __call__(self, value):
        self.calls.append(value)
        self.entered.set()
        assert self.release.wait(3), 'test failed to release HTTP worker'
        return self.result


def finish(slot, held):
    held.release.set()
    slot.thread.join(2)
    assert not slot.thread.is_alive()


def test_held_job_keeps_heartbeats_events_and_due_delivery_moving(monkeypatch):
    now = [0.0]
    monkeypatch.setattr(bridge.time, 'monotonic', lambda: now[0])
    held = HeldHTTP()
    due = HeldHTTP()
    monkeypatch.setattr(bridge, 'notify_core_run_her_pov', held)
    monkeypatch.setattr(bridge, 'notify_core_due', due)
    ch, conn = Channel(), Connection()
    ch.put(bridge.JOBS_QUEUE, json.dumps({'job_id': JOB}))
    ch.put(bridge.JOBS_QUEUE, json.dumps({'job_id': JOB}), tag=2)
    runner = bridge.Bridge()
    runner.attach(conn, ch)
    try:
        runner.tick()
        assert held.entered.wait(1)
        ch.put(bridge.DUE_QUEUE, json.dumps({'task_id': 'due'}), tag=3)
        for i in range(10):
            now[0] += 15  # Five simulated 30-second heartbeat periods while held.
            runner.tick({'type': 'message', 'data': json.dumps({'type': 'ordinary', 'data': i})})
        assert due.entered.wait(1)
        assert conn.ticks == 11
        assert len(ch.published) == 10
        assert held.calls == [JOB]
        finish(runner.due, due)
        runner.tick()
        assert ch.acks == [3]
        assert held.calls == [JOB]
        finish(runner.job, held)
        runner.tick()
        assert ch.acks == [3, 1]
    finally:
        held.release.set()
        due.release.set()
        runner.close()


def test_reconnect_retains_slot_and_never_acks_old_tag(monkeypatch):
    held = HeldHTTP()
    monkeypatch.setattr(bridge, 'notify_core_run_her_pov', held)
    first, second = Channel(), Channel()
    first.put(bridge.JOBS_QUEUE, json.dumps({'job_id': JOB}))
    runner = bridge.Bridge()
    runner.attach(Connection(), first)
    try:
        runner.tick()
        assert held.entered.wait(1)
        runner.attach(Connection(), second)
        second.put(bridge.JOBS_QUEUE, json.dumps({'job_id': JOB}), tag=1)
        for _ in range(5):
            runner.tick()
        assert held.calls == [JOB]
        finish(runner.job, held)
        runner.tick()
        assert first.acks == second.acks == []
        # Replacement delivery may now run, but only after old HTTP settled.
        runner.job.thread.join(1)
        runner.tick()
        assert second.acks == [1]
    finally:
        held.release.set()
        runner.close()


def test_failure_requeues_with_cooldown_and_due_worker_does_not_block_job(monkeypatch):
    now = [100.0]
    monkeypatch.setattr(bridge.time, 'monotonic', lambda: now[0])
    held, due = HeldHTTP(False), HeldHTTP()
    monkeypatch.setattr(bridge, 'notify_core_run_her_pov', held)
    monkeypatch.setattr(bridge, 'notify_core_due', due)
    ch = Channel()
    ch.put(bridge.JOBS_QUEUE, json.dumps({'job_id': JOB}))
    ch.put(bridge.JOBS_QUEUE, json.dumps({'job_id': JOB}), tag=2)
    ch.put(bridge.DUE_QUEUE, json.dumps({'task_id': 'due'}), tag=3)
    runner = bridge.Bridge()
    runner.attach(Connection(), ch)
    try:
        runner.tick()
        assert held.entered.wait(1) and due.entered.wait(1)
        finish(runner.job, held)
        for _ in range(10):
            runner.tick({'type': 'message', 'data': '{"type":"job.enqueue","job_id":"' + JOB + '"}'})
        assert ch.nacks == [(1, True)]
        assert held.calls == [JOB]
        assert runner.conn.ticks == 11
        now[0] += 10
        runner.tick()
        runner.job.thread.join(1)
        assert held.calls == [JOB, JOB]
    finally:
        held.release.set()
        due.release.set()
        runner.close()


@pytest.mark.parametrize('code,ack', [(400, True), (422, True), (401, False), (403, False), (404, False), (429, False), (500, False), (503, False)])
def test_http_auth_and_infrastructure_failures_are_retryable(monkeypatch, code, ack):
    def reject(*args, **kwargs):
        raise urllib.error.HTTPError('http://unit.invalid', code, 'test', {}, None)
    monkeypatch.setattr(urllib.request, 'urlopen', reject)
    assert bridge.notify_core_run_her_pov(JOB) is ack


def test_publish_uses_mandatory_routing_and_propagates_rejection():
    ch = Channel()
    assert bridge.enqueue_job(ch, {'job_id': JOB})
    assert bridge.arm_defer(ch, {'task_id': 'due', 'hops': [10]})
    assert all(p['mandatory'] for p in ch.published)
    ch.failure = RuntimeError('unroutable/nack/disconnected before confirm')
    with pytest.raises(RuntimeError):
        bridge.enqueue_job(ch, {'job_id': JOB})


def test_malformed_work_is_rejected_and_due_hops_are_bounded():
    ch = Channel()
    for i, body in enumerate(['[]', '{}', 'null', '{', '{"job_id":"bad"}']):
        ch.put(bridge.JOBS_QUEUE, body, tag=i)
    for i in range(100):
        ch.put(bridge.DUE_QUEUE, json.dumps({'task_id': 'due', 'hops': [10]}), tag=100+i)
    runner = bridge.Bridge()
    runner.attach(Connection(), ch)
    for _ in range(5):
        runner.tick()
    assert ch.nacks[:1] == [(0, False)]
    assert set(ch.nacks) == {(i, False) for i in range(5)}
    assert len(ch.messages[bridge.DUE_QUEUE]) > 0
    runner.close()


@pytest.mark.parametrize('body,expected', [(b'{"ok":false}', False), (b'{"ok":true}', True), (b'{}', False), (b'not json', False)])
def test_core_claim_retry_result_is_not_mistaken_for_success(monkeypatch, body, expected):
    class Response:
        status = 200
        def __enter__(self):
            return self
        def __exit__(self, *args):
            pass
        def read(self):
            return body
    monkeypatch.setattr(urllib.request, 'urlopen', lambda *args, **kwargs: Response())
    assert bridge.notify_core_run_her_pov(JOB) is expected


def test_connection_enables_confirms_and_closes_on_topology_failure(monkeypatch):
    conn, ch = Connection(), Channel()
    conn.channel = lambda: ch
    confirmed = []
    ch.exchange_declare = lambda **kw: None
    ch.confirm_delivery = lambda: confirmed.append(True)
    monkeypatch.setattr(bridge.pika, 'PlainCredentials', lambda *a: object(), raising=False)
    monkeypatch.setattr(bridge.pika, 'ConnectionParameters', lambda **kw: object(), raising=False)
    monkeypatch.setattr(bridge.pika, 'BlockingConnection', lambda *a: conn, raising=False)
    monkeypatch.setattr(bridge, 'declare_defer_topology', lambda ch: None)
    monkeypatch.setattr(bridge, 'declare_jobs_topology', lambda ch: None)
    assert bridge.rabbit_connect() == (conn, ch)
    assert confirmed == [True]
    def broken(ch):
        raise RuntimeError('topology mismatch')
    monkeypatch.setattr(bridge, 'declare_jobs_topology', broken)
    with pytest.raises(RuntimeError):
        bridge.rabbit_connect()
    assert conn.closed


def test_failed_due_repark_never_acks_original_and_close_does_not_wait(monkeypatch):
    held = HeldHTTP()
    monkeypatch.setattr(bridge, 'notify_core_run_her_pov', held)
    ch, conn = Channel(), Connection()
    ch.put(bridge.JOBS_QUEUE, json.dumps({'job_id': JOB}))
    runner = bridge.Bridge()
    runner.attach(conn, ch)
    try:
        runner.tick()
        assert held.entered.wait(1)
        ch.put(bridge.DUE_QUEUE, json.dumps({'task_id': 'due', 'hops': [10]}), tag=2)
        ch.failure = RuntimeError('publish lost before broker confirm')
        with pytest.raises(RuntimeError):
            runner.tick()
        assert ch.acks == ch.nacks == []
        runner.close()
        assert conn.closed
        assert runner.job.thread.is_alive()
        runner.tick()
        assert ch.acks == ch.nacks == []
    finally:
        finish(runner.job, held)
        runner.close()


def test_connection_closes_even_when_channel_creation_fails(monkeypatch):
    conn = Connection()
    def broken():
        raise RuntimeError('channel unavailable')
    conn.channel = broken
    monkeypatch.setattr(bridge.pika, 'PlainCredentials', lambda *a: object(), raising=False)
    monkeypatch.setattr(bridge.pika, 'ConnectionParameters', lambda **kw: object(), raising=False)
    monkeypatch.setattr(bridge.pika, 'BlockingConnection', lambda *a: conn, raising=False)
    with pytest.raises(RuntimeError):
        bridge.rabbit_connect()
    assert conn.closed


@pytest.mark.parametrize('error_name', ['NackError', 'UnroutableError', 'AMQPConnectionError'])
def test_real_pika_failed_confirmation_never_claims_handoff_success(error_name):
    if not hasattr(bridge.pika, 'exceptions'):
        pytest.skip('actual pinned pika required for transport exception classes')
    ch = Channel()
    error_type = getattr(bridge.pika.exceptions, error_name)
    ch.failure = error_type([]) if error_name != 'AMQPConnectionError' else error_type()
    with pytest.raises(error_type):
        bridge.enqueue_job(ch, {'job_id': JOB})
    assert ch.published == []
