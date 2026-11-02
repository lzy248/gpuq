"""Opt-in real quorum tests: GPUQ_TEST_ETCD points to an official etcd executable."""
import os
import socket
import subprocess
import time
import uuid

import pytest

from gpuq.cluster import ClusterAgent, ClusterConfig, ClusterQueue
from gpuq.coordination import EtcdStore, Unavailable
from test_scheduler import FakeBackend, gpu


@pytest.fixture
def quorum(tmp_path):
    binary = os.environ.get('GPUQ_TEST_ETCD')
    if not binary:
        pytest.skip('set GPUQ_TEST_ETCD to run real three-member etcd integration')
    sockets = []
    try:
        for _ in range(6):
            sock = socket.socket()
            sock.bind(('127.0.0.1', 0))
            sockets.append(sock)
        ports = [sock.getsockname()[1] for sock in sockets]
    finally:
        for sock in sockets:
            sock.close()
    clients = [f'http://127.0.0.1:{p}' for p in ports[:3]]
    peers = [f'http://127.0.0.1:{p}' for p in ports[3:]]
    initial = ','.join(f'n{i}={p}' for i, p in enumerate(peers))
    token = uuid.uuid4().hex
    processes = {}
    logs = []

    def start(index):
        log = (tmp_path / f'etcd-{index}.log').open('ab')
        logs.append(log)
        process = subprocess.Popen([
            binary, '--name', f'n{index}', '--data-dir', str(tmp_path / f'data-{index}'),
            '--listen-client-urls', clients[index], '--advertise-client-urls', clients[index],
            '--listen-peer-urls', peers[index], '--initial-advertise-peer-urls', peers[index],
            '--initial-cluster', initial, '--initial-cluster-token', token, '--log-level', 'error',
        ], stdin=subprocess.DEVNULL, stdout=log, stderr=log)
        processes[index] = process

    def stop(index):
        process = processes.pop(index, None)
        if process:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)

    try:
        for index in range(3):
            start(index)
        store = EtcdStore(clients, '/gpuq/test-' + token, timeout=0.5)
        deadline = time.monotonic() + 20
        while True:
            try:
                if store.members() == 3 and store.get('ready') is None:
                    break
            except Unavailable:
                pass
            assert time.monotonic() < deadline, 'etcd cluster did not become ready'
            time.sleep(0.1)
        yield store, start, stop
    finally:
        for index in list(processes):
            stop(index)
        for log in logs:
            log.close()


def until(predicate, action, timeout=25):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            action()
            if predicate():
                return
        except Unavailable:
            pass
        time.sleep(0.1)
    raise AssertionError('condition not reached before deadline')


def test_real_quorum_takeover_stale_fencing_failback_and_majority_loss(quorum, settings, tmp_path):
    store, start, stop = quorum
    queue = ClusterQueue(store)
    queue.initialize('a', limit=2, mode='fair', copies=2, failback=0.3)

    def make(node):
        config = ClusterConfig(node, 'test', tuple(store.endpoints), tmp_path / f'worker-{node}',
                               {node: 'local'}, lease_seconds=10)
        return ClusterAgent(config, settings, store=store, backend=FakeBackend([gpu(0, free=100000)]))

    a, b = make('a'), make('b')
    a.tick()
    b.tick()
    old = store.get('leader')
    assert old.value['node'] == 'a'
    # A's host fails: its etcd member and gpuq agent both stop. B+the witness remain.
    stop(0)
    until(lambda: store.get('leader') is not None and store.get('leader').value['node'] == 'b', b.tick)
    assert not store.txn({'leader': old}, {'stale-dispatch': {'bad': True}})
    assert store.get('stale-dispatch') is None
    # A restarts with the same persistent journal; delayed handback uses a new lease.
    start(0)
    restored = make('a')
    until(lambda: store.get('leader').value['node'] == 'a', lambda: (restored.tick(), b.tick()))
    assert store.get('leader').value['token'] == restored.session
    # A and the witness disappear. B alone cannot authorize any global write.
    stop(0)
    stop(2)
    with pytest.raises(Unavailable):
        b.tick()
    assert b.worker_db.allowed == set()
    start(0)
    start(2)
    until(lambda: store.get('stale-dispatch') is None, lambda: store.get('config'))


def test_real_transaction_isolation_and_expired_lease(quorum):
    store, start, stop = quorum
    assert store.txn({'item': None}, {'item': {'value': 1}})
    stale = store.get('item')
    assert store.txn({'item': stale}, {'item': {'value': 2}})
    assert not store.txn({'item': stale}, {'item': {'value': 3}})
    assert store.scan('it')['item'].value == {'value': 2}
    lease = store.grant(2)
    assert store.txn({'leased': None}, {'leased': {'ok': True}}, leases={'leased': lease})
    store.keepalive(lease)
    until(lambda: store.get('leased') is None, lambda: None, timeout=8)
    with pytest.raises(Unavailable):
        store.keepalive(lease)


def test_real_fenced_node_loss_retry_is_durable_and_cancellation_wins(quorum, tmp_path):
    from conftest import submit_job
    from gpuq.cluster import job_key
    from gpuq.db import QueueDB

    store, start, stop = quorum
    queue = ClusterQueue(store)
    queue.initialize('a', limit=2, mode='fair', copies=2, failback=30)
    lease = store.grant(10)
    store.txn({'leader': None}, {'leader': {'node': 'b', 'token': 'new-master'},
                                'nodes/b': {'node': 'b'}}, leases={'leader': lease, 'nodes/b': lease})
    leader = store.get('leader')
    db = QueueDB(tmp_path / 'staging.sqlite3')
    job = submit_job(db, tmp_path)
    job_id = queue.submit(job, {}, 'inputs', ['a', 'b'])
    def assigned(value):
        value.update(node='a', execution='old', attempt_base=0,
                     executions=[{'node': 'a', 'execution': 'old'}])
        value['job'].update(status='running', attempt=1)
    queue.update(job_id, assigned)
    stale = store.get(job_key(job_id))
    assert queue.requeue_lost(leader, store.scan('nodes/'), 100)
    current = store.get(job_key(job_id)).value
    assert current['job']['status'] == 'queued' and current['failover_count'] == 1
    assert current['executions'][0]['superseded']
    assert not store.txn({job_key(job_id): stale}, {job_key(job_id): stale.value})
    # Even a new queue object with a fresh read cannot spend the same budget twice.
    queue.update(job_id, assigned)
    fresh = ClusterQueue(store)
    assert not fresh.requeue_lost(leader, store.scan('nodes/'), 10000)
    another = queue.submit(job, {}, 'inputs', ['a', 'b'])
    queue.update(another, assigned)
    queue.update(another, lambda v: v['job'].update(cancel_requested=True))
    assert not fresh.requeue_lost(leader, store.scan('nodes/'), 20000)
    assert store.get(job_key(another)).value['job']['cancel_requested']
