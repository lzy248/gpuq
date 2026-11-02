"""Deterministic failure/interleaving tests; consensus itself belongs to etcd."""
from dataclasses import replace
import json

import pytest

from gpuq.cluster import ClusterAgent, ClusterConfig, ClusterQueue, job_key
from gpuq.cluster_transfer import prepare_local, snapshot
from gpuq.coordination import Record, Unavailable
from gpuq.db import QueueDB
from conftest import submit_job
from test_scheduler import FakeBackend, FakeClock, gpu


class MemoryStore:
    def __init__(self):
        self.data = {}
        self.revision = 0
        self.leases = {}
        self.key_leases = {}
        self.online = True
        self.lost_reply = False

    def check(self):
        if not self.online:
            raise Unavailable("no quorum")

    def get(self, key):
        self.check()
        record = self.data.get(key)
        return Record(json.loads(json.dumps(record.value)), record.revision) if record else None

    def scan(self, prefix):
        self.check()
        return {k: self.get(k) for k in self.data if k.startswith(prefix)}

    def txn(self, checks, puts, *, deletes=(), leases=None):
        self.check()
        for key, record in checks.items():
            old = self.data.get(key)
            if (old.revision if old else 0) != (record.revision if record else 0):
                return False
        if leases and any(lease not in self.leases for lease in leases.values()):
            raise Unavailable("expired lease")
        self.revision += 1
        for key, value in puts.items():
            self.data[key] = Record(json.loads(json.dumps(value)), self.revision)
            self.key_leases.pop(key, None)
            if leases and key in leases:
                self.key_leases[key] = leases[key]
        for key in deletes:
            self.data.pop(key, None)
            self.key_leases.pop(key, None)
        if self.lost_reply:
            self.lost_reply = False
            raise Unavailable("reply lost after commit")
        return True

    def grant(self, ttl):
        self.check()
        lease = max(self.leases, default=0) + 1
        self.leases[lease] = ttl
        return lease

    def keepalive(self, lease):
        self.check()
        if lease not in self.leases:
            raise Unavailable("expired lease")

    def expire(self, lease):
        self.leases.pop(lease, None)
        for key in [k for k, v in self.key_leases.items() if v == lease]:
            self.data.pop(key, None)
            self.key_leases.pop(key)

    def members(self):
        return 3


@pytest.fixture
def cluster(tmp_path, settings):
    store = MemoryStore()
    queue = ClusterQueue(store)
    queue.initialize('a', limit=2, mode='fair', copies=2, failback=30, failover_retries=0)
    clock = FakeClock()
    launched = []

    def agent(node):
        config = ClusterConfig(node, 'test', ('http://localhost:2379',), tmp_path / node, {node: 'local'})
        instance = ClusterAgent(config, settings, store=store, backend=FakeBackend([gpu(0, free=100000)]), clock=clock, wall_clock=clock)
        def launch(job, index):
            launched.append((node, job.id))
            instance.db.mark_running(job.id, pid=999999, pgid=999999)
        instance.scheduler.launcher = launch
        instance.scheduler.process_checker = lambda pid: True
        return instance

    a, b = agent('a'), agent('b')
    a.tick()
    b.tick()
    source = tmp_path / 'source'
    source.mkdir()
    (source / 'train.py').write_text("print('training')")
    identity, manifest = snapshot(source, '.', [], [])
    for node in (a, b):
        prepare_local(node.config.work_dir, source, identity, manifest)
    staging = QueueDB(tmp_path / 'staging.sqlite3')

    def submit(project='A', **overrides):
        job = submit_job(staging, source, project_id=project, stable_samples=1, memory_mb=10, reserve_mb=0, **overrides)
        # Canonical submission inputs are deliberately distinct from runtime Job fields.
        keys = ['name', 'command', 'cwd', 'env', 'gpu_allowlist', 'gpu_count', 'project_id', 'memory_mb',
                'memory_mode', 'reserve_mb', 'max_utilization', 'stable_samples', 'priority', 'exclusive',
                'dependency_id', 'max_retries', 'retry_on_oom', 'log_path', 'notify']
        values = {key: getattr(job, key) for key in keys}
        return queue.submit(job, values, identity, ['a', 'b'], project_limit=overrides.get('project_limit'))
    return store, queue, a, b, agent, submit, launched, clock


def test_election_takeover_and_stable_preferred_failback(cluster):
    store, queue, a, b, agent, submit, launched, clock = cluster
    assert store.get('leader').value['node'] == 'a'
    old_leader = store.get('leader')
    store.expire(a.lease)
    b.tick()
    assert store.get('leader').value['node'] == 'b'
    assert not store.txn({'leader': old_leader}, {'unsafe': {}})
    restored = agent('a')
    restored.tick()
    b.tick()
    clock.advance(29)
    b.tick()
    assert store.get('leader').value['node'] == 'b'
    clock.advance(1)
    b.tick()
    assert store.get('leader').value['node'] == 'a'
    assert store.get('leader').value['token'] == restored.session


def test_one_global_pool_uses_both_nodes_and_project_fairness(cluster):
    store, queue, a, b, agent, submit, launched, clock = cluster
    first = submit('A', project_limit=2)
    second_a = submit('A')
    second_b = submit('B', project_limit=2)
    for _ in range(5):
        b.tick()
        a.tick()
    first_record = store.get(job_key(first)).value
    b_record = store.get(job_key(second_b)).value
    assert first_record['job']['status'] == 'running'
    assert b_record['job']['status'] == 'running'
    assert first_record['node'] != b_record['node']
    assert store.get(job_key(second_a)).value['job']['status'] == 'queued'
    assert len(launched) == 2


def test_no_quorum_authorizes_nothing_and_reports_survive(cluster):
    store, queue, a, b, agent, submit, launched, clock = cluster
    job_id = submit()
    for _ in range(3):
        a.tick()
    value = store.get(job_key(job_id)).value
    assert value['job']['status'] == 'running'
    local = a.db.by_submission_key(value['execution'])
    store.online = False
    a.db.finish(local.id, status='completed', exit_code=0)
    with pytest.raises(Unavailable):
        a.tick()
    assert len(launched) == 1
    store.online = True
    a.tick()
    assert store.get(job_key(job_id)).value['job']['status'] == 'completed'


def test_unknown_remote_execution_not_reassigned_after_master_loss(cluster):
    store, queue, a, b, agent, submit, launched, clock = cluster
    job_id = submit()
    for _ in range(3):
        a.tick()
    value = store.get(job_key(job_id)).value
    execution = value['execution']
    store.expire(a.lease)
    for _ in range(3):
        b.tick()
    current = store.get(job_key(job_id)).value
    assert current['node'] == 'a' and current['execution'] == execution
    assert current['job']['status'] == 'running'
    assert len(launched) == 1


def test_stale_assignment_requires_new_leader_authorization(cluster):
    store, queue, a, b, agent, submit, launched, clock = cluster
    job_id = submit()
    # Make only B offer, then A chooses B but has not handed over yet.
    a.scheduler.backend.states = []
    b.tick()
    a.tick()
    original = store.get(job_key(job_id)).value
    assert original['node'] == 'b' and original['job']['status'] == 'assigned'
    stale = store.get('leader')
    store.expire(a.lease)
    b.reconcile(queue.jobs(), store.get('leader'))
    assert launched == []
    b.tick()  # elect + adopt, retaining the same execution ID
    assert store.get('leader').value['node'] == 'b'
    assert not store.txn({'leader': stale}, {'unsafe': {}})
    b.tick()
    assert len(launched) == 1
    assert store.get(job_key(job_id)).value['execution'] == original['execution']


def test_duplicate_local_prepare_after_lost_reply_has_one_job(cluster):
    store, queue, a, b, agent, submit, launched, clock = cluster
    job_id = submit()
    a.tick()
    value = store.get(job_key(job_id)).value
    first = a._prepare_execution(value)
    second = a._prepare_execution(value)
    assert first.id == second.id
    assert len(a.db.list(limit=None)) == 1
    a.tick()
    a.tick()
    assert len(launched) == 1


def test_changed_disk_cannot_reuse_execution_node_id(cluster, tmp_path, settings):
    store, queue, a, b, agent, submit, launched, clock = cluster
    new = ClusterAgent(replace(a.config, work_dir=tmp_path / 'empty-disk'), settings, store=store,
                       backend=FakeBackend([]))
    with pytest.raises(ValueError, match='different storage'):
        new.tick()


def test_insufficient_replicas_do_not_submit(cluster, tmp_path):
    store, queue, a, b, agent, submit, launched, clock = cluster
    db = QueueDB(tmp_path / 'validation.sqlite3')
    job = submit_job(db, tmp_path)
    with pytest.raises(ValueError, match='replicas'):
        queue.submit(job, {}, 'manifest', ['a'])
    assert queue.jobs() == {}


def test_node_start_receipt_after_commit_loss_is_reconciled(cluster):
    store, queue, a, b, agent, submit, launched, clock = cluster
    job_id = submit()
    a.tick()
    leader = store.get('leader')
    store.lost_reply = True
    with pytest.raises(Unavailable):
        a.reconcile(queue.jobs(), leader)
    assert store.get(job_key(job_id)).value['job']['status'] == 'starting'
    a.reconcile(queue.jobs(), leader)
    a.scheduler.tick()
    a.reconcile(queue.jobs(), leader)
    assert len(launched) == 1


def test_dependency_blocks_until_global_completion(cluster):
    store, queue, a, b, agent, submit, launched, clock = cluster
    parent = submit('A')
    child = submit('B', dependency_id=1)
    for _ in range(3):
        a.tick()
        b.tick()
    assert store.get(job_key(child)).value['job']['status'] == 'queued'
    value = store.get(job_key(parent)).value
    node = a if value['node'] == 'a' else b
    node.db.finish(node.db.by_submission_key(value['execution']).id, status='completed', exit_code=0)
    for _ in range(4):
        a.tick()
        b.tick()
    assert store.get(job_key(child)).value['job']['status'] == 'running'


def test_fixed_oom_budget_survives_global_readmission(cluster):
    store, queue, a, b, agent, submit, launched, clock = cluster
    job_id = submit(max_retries=1, retry_on_oom=True)
    for _ in range(3):
        a.tick()
    first = store.get(job_key(job_id)).value
    local = a.db.by_submission_key(first['execution'])
    assert local.max_retries == 1 and local.attempt == 1
    a.db.requeue_after_oom(local.id, new_memory_mb=256, error='CUDA out of memory')
    a.tick()
    b.tick()
    a.tick()
    value = store.get(job_key(job_id)).value
    node = a if value['node'] == 'a' else b
    node.tick()
    current = node.db.by_submission_key(value['execution'])
    assert current.max_retries == 0
    assert current.memory_mb == 256 and current.oom_count == 1
    assert value['attempt_base'] == 1
    assert len(launched) == 2


def test_multigpu_never_combines_different_hosts(cluster):
    store, queue, a, b, agent, submit, launched, clock = cluster
    job_id = submit(gpu_count=2)
    for _ in range(3):
        a.tick()
        b.tick()
    assert store.get(job_key(job_id)).value['job']['status'] == 'queued'
    assert not launched
    b.scheduler.backend.states = [gpu(0, free=100000), gpu(1, free=100000)]
    for _ in range(3):
        b.tick()
        a.tick()
    value = store.get(job_key(job_id)).value
    assert value['node'] == 'b'
    assert value['job']['assigned_gpus'] == [0, 1]
    assert len(launched) == 1


def test_cancel_assigned_never_launches_and_running_waits_for_ack(cluster):
    store, queue, a, b, agent, submit, launched, clock = cluster
    job_id = submit()
    a.tick()
    queue.update(job_id, lambda v: v['job'].update(cancel_requested=True))
    a.tick()
    assert store.get(job_key(job_id)).value['job']['status'] == 'cancelled'
    assert not launched
    next_id = submit()
    for _ in range(3):
        a.tick()
    queue.update(next_id, lambda v: v['job'].update(cancel_requested=True))
    assert store.get(job_key(next_id)).value['job']['status'] == 'running'


def test_submission_replication_over_fake_ssh(tmp_path, monkeypatch, settings):
    import os
    import sys
    import subprocess
    from pathlib import Path
    from gpuq.cli import build_parser
    from gpuq.cluster import load_cluster, submit_cluster
    from gpuq.cluster_transfer import call, profile_for

    bindir = tmp_path / 'bin'
    bindir.mkdir()
    ssh = bindir / 'ssh'
    ssh.write_text('#!/bin/bash\nexec /bin/sh -c "${!#}"\n')
    ssh.chmod(0o755)
    monkeypatch.setenv('PATH', f"{bindir}:{os.environ['PATH']}")
    monkeypatch.setenv('PYTHONPATH', str(Path(__file__).resolve().parents[1] / 'src'))
    root = tmp_path / 'project'
    root.mkdir()
    subprocess.run(['git', 'init', '-q', str(root)], check=True)
    (root / 'pyproject.toml').write_text('# marker')
    (root / 'train.py').write_text('print(1)')
    (root / 'model.safetensors').write_bytes(b'not sent')
    (root / 'resume.ckpt').write_bytes(b'explicit resume')
    remote = tmp_path / 'b.toml'
    remote.write_text('[cluster]\nnode_id="b"\nnamespace="test"\nendpoints=["http://localhost:2379"]\n'
                      f'work_dir={json.dumps(str(tmp_path / "b"))}\n')
    config_path = tmp_path / 'a.toml'
    config_path.write_text('[cluster]\nnode_id="a"\nnamespace="test"\nendpoints=["http://localhost:2379"]\n'
                          f'work_dir={json.dumps(str(tmp_path / "a"))}\n'
                          '[cluster.nodes]\na="local"\nb="other"\n[remotes.other]\nhost="fake"\n'
                          f'command={json.dumps([sys.executable, "-m", "gpuq"])}\nconfig={json.dumps(str(remote))}\n')
    settings = replace(settings, config_path=config_path)
    config = load_cluster(settings)
    store = MemoryStore()
    queue = ClusterQueue(store)
    queue.initialize('a', limit=2, mode='fair', copies=2, failback=30, failover_retries=0)
    args = build_parser().parse_args(['--cluster', 'submit', '--cwd', str(root), '--sync-include',
                                      'resume.ckpt', '--', 'python', 'train.py'])
    submit_cluster(args, settings, config, queue)
    value = store.get(job_key(1)).value
    assert value['eligible_nodes'] == ['a', 'b']
    for node in ('a', 'b'):
        manifest = json.loads((tmp_path / node / 'manifests' / (value['manifest'] + '.json')).read_text())
        assert 'resume.ckpt' in manifest['files'] and 'model.safetensors' not in manifest['files']
    assert not (tmp_path / 'b' / 'queue.sqlite3').exists()
    from gpuq import cluster_transfer
    transfers = []
    original_write = cluster_transfer.write_blobs
    def tracked_write(root, manifest, missing, output):
        transfers.append(len(missing))
        return original_write(root, manifest, missing, output)
    monkeypatch.setattr(cluster_transfer, 'write_blobs', tracked_write)
    submit_cluster(args, settings, config, queue)
    assert transfers == [0, 0]
    with pytest.raises(RuntimeError, match='SSH request failed'):
        call(profile_for('other', config_path), {'operation': 'logs', 'namespace': 'wrong', 'execution': 'x'})


def test_fifo_still_honors_explicit_priority(cluster):
    store, queue, a, b, agent, submit, launched, clock = cluster
    record = store.get('config')
    store.txn({'config': record}, {'config': {**record.value, 'scheduling_mode': 'fifo', 'max_running_jobs': 1}})
    first = submit('A')
    high = submit('B', priority=10)
    for _ in range(3):
        a.tick()
    assert store.get(job_key(high)).value['job']['status'] == 'running'
    assert store.get(job_key(first)).value['job']['status'] == 'queued'


def test_cluster_cli_management_json_and_retry(cluster, tmp_path, monkeypatch, capsys):
    from gpuq.cli import main
    store, queue, a, b, agent, submit, launched, clock = cluster
    path = tmp_path / 'client.toml'
    path.write_text('[cluster]\nnode_id="a"\nendpoints=["http://localhost:2379"]\n')
    monkeypatch.setattr(ClusterConfig, 'store', lambda self: store)
    job_id = submit()

    def run(*argv, code=0):
        with pytest.raises(SystemExit) as result:
            main(['--config', str(path), '--cluster', *argv])
        assert result.value.code == code
        return capsys.readouterr().out

    run('priority', str(job_id), '5')
    assert json.loads(run('show', str(job_id), '--json'))['priority'] == 5
    assert json.loads(run('list', '--json'))[0]['node'] is None
    run('retry', str(job_id), code=2)
    run('cancel', str(job_id))
    assert json.loads(run('show', str(job_id), '--json'))['status'] == 'cancelled'
    run('retry', str(job_id), '--memory', 'auto', '--reset-attempts')
    value = json.loads(run('show', str(job_id), '--json'))
    assert value['status'] == 'queued' and value['memory_mode'] == 'auto'
    assert value['attempt'] == 0 and value['execution'] is None
    assert json.loads(run('projects', '--json'))[0]['pending'] == 1
    assert json.loads(run('cluster', 'status'))['leader']['node'] == 'a'
    run('daemon', '--max-running-jobs', '2', '--once', code=2)


def enable_failover(store, *, timeout=3600, idle=True, retries=1):
    old = store.get('config')
    store.txn({'config': old}, {'config': {**old.value, 'failover_after_seconds': timeout,
                                         'failover_when_idle': idle, 'max_failover_retries': retries}})


def test_idle_failover_cancels_old_attempt_and_fences_late_results(cluster, monkeypatch):
    store, queue, a, b, agent, submit, launched, clock = cluster
    enable_failover(store)
    job_id = submit()
    for _ in range(3):
        a.tick()
    original = store.get(job_key(job_id))
    old_local = a.db.by_submission_key(original.value['execution'])
    store.expire(a.lease)
    for _ in range(4):
        b.tick()
    current = store.get(job_key(job_id)).value
    assert current['node'] == 'b' and current['job']['status'] == 'running'
    assert current['execution'] != original.value['execution']
    assert current['failover_count'] == 1
    assert current['executions'][0]['superseded']
    assert not store.txn({job_key(job_id): original}, {job_key(job_id): original.value})
    new_local = b.db.by_submission_key(current['execution'])
    assert new_local.env['GPUQ_EXECUTION_ID'] == current['execution']
    assert new_local.env['GPUQ_FAILOVER_COUNT'] == '1'
    assert old_local.cwd != new_local.cwd
    cancelled = []
    def cancel(db, local_id, *, force_after):
        cancelled.append((db.path, local_id))
        db.finish(local_id, status='cancelled', exit_code=-15)
        return db.get(local_id)
    monkeypatch.setattr('gpuq.cluster.cancel_job', cancel)
    restored = agent('a')
    restored.tick()
    assert len(cancelled) == 1
    after = store.get(job_key(job_id)).value
    assert after['execution'] == current['execution'] and after['job']['status'] == 'running'
    assert after['executions'][0]['cancellation_ack']
    # Another disconnect cannot reset the one-retry budget.
    store.expire(b.lease)
    clock.advance(4000)
    for _ in range(3):
        restored.tick()
    assert store.get(job_key(job_id)).value['execution'] == current['execution']
    assert len(launched) == 2


def test_loss_timer_survives_leader_change_and_timeout_ignores_busy_pool(cluster):
    store, queue, a, b, agent, submit, launched, clock = cluster
    enable_failover(store, idle=False)
    lost = submit('A')
    for _ in range(3):
        a.tick()
    store.expire(a.lease)
    b.tick()
    assert store.get(job_key(lost)).value['lost_since'] == 0
    busy = submit('B')
    for _ in range(3):
        b.tick()
    assert store.get(job_key(busy)).value['job']['status'] == 'running'
    clock.advance(3599)
    b.tick()
    assert store.get(job_key(lost)).value['node'] == 'a'
    store.expire(b.lease)
    replacement = agent('b')
    replacement.tick()
    clock.advance(1)
    replacement.tick()
    value = store.get(job_key(lost)).value
    assert value['failover_count'] == 1 and value['job']['status'] == 'queued'
    assert value['executions'][0]['reason'] == 'node_timeout'


def test_idle_retry_waits_for_other_work_and_does_not_retry_cancelled_request(cluster):
    store, queue, a, b, agent, submit, launched, clock = cluster
    enable_failover(store, timeout=0)
    lost = submit('A')
    for _ in range(3):
        a.tick()
    busy = submit('B')
    for _ in range(3):
        b.tick()
        a.tick()
    store.expire(a.lease)
    b.tick()
    assert store.get(job_key(lost)).value['node'] == 'a'
    value = store.get(job_key(busy)).value
    assert value['node'] == 'b'
    local = b.db.by_submission_key(value['execution'])
    b.db.finish(local.id, status='completed', exit_code=0)
    # A user cancellation always wins over automatic requeue.
    queue.update(lost, lambda v: v['job'].update(cancel_requested=True))
    b.tick()
    assert store.get(job_key(lost)).value['node'] == 'a'
    assert store.get(job_key(lost)).value.get('failover_count', 0) == 0


def test_recovered_node_resets_loss_timer(cluster):
    store, queue, a, b, agent, submit, launched, clock = cluster
    enable_failover(store, idle=False)
    job_id = submit()
    for _ in range(3):
        a.tick()
    store.expire(a.lease)
    b.tick()
    clock.advance(3599)
    restored = agent('a')
    restored.tick()
    b.tick()
    assert 'lost_since' not in store.get(job_key(job_id)).value
    store.expire(restored.lease)
    b.tick()
    clock.advance(2)
    b.tick()
    assert store.get(job_key(job_id)).value.get('failover_count', 0) == 0


def test_default_failover_policy_and_invalid_timeout():
    from gpuq.cli import build_parser
    args = build_parser().parse_args(['cluster', 'init'])
    assert args.failover_after_seconds == 3600
    assert args.failover_when_idle and args.max_failover_retries == 1
    args = build_parser().parse_args(['cluster', 'init', '--no-failover-when-idle', '--max-failover-retries', '0'])
    assert not args.failover_when_idle and args.max_failover_retries == 0
    for timeout in (float('nan'), float('inf'), -1):
        store = MemoryStore()
        with pytest.raises(ValueError, match='finite'):
            ClusterQueue(store).initialize('a', limit=2, mode='fair', copies=2, failback=30,
                                           failover_after=timeout)
        assert store.get('config') is None
