"""Simple-pool persistence, transport, routing and placement without GPUs or etcd."""
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from gpuq.cli import main
from gpuq.cluster import ClusterAgent, ClusterQueue, job_key, load_cluster
from gpuq.config import load_settings
from gpuq.coordination import Unavailable
from gpuq.pool_store import SQLiteStore
from test_scheduler import FakeBackend, FakeClock, gpu


def test_sqlite_cas_persistence_expiry_and_rollback(tmp_path):
    clock = FakeClock()
    path = tmp_path / 'control.sqlite3'
    store = SQLiteStore(path, clock=clock)
    assert store.txn({'key': None}, {'key': {'a': 1}})
    old = store.get('key')
    def attempt(value):
        return SQLiteStore(path, clock=clock).txn({'key': old}, {'key': {'a': value}})
    with ThreadPoolExecutor(max_workers=2) as executor:
        assert sum(executor.map(attempt, [2, 3])) == 1
    lease = store.grant(10)
    store.txn({}, {'leased': {}}, leases={'leased': lease})
    clock.advance(9)
    store.keepalive(lease)
    clock.advance(9)
    assert store.get('leased') is not None
    clock.advance(1)
    assert store.get('leased') is None
    with pytest.raises(Unavailable):
        store.keepalive(lease)
    assert store.grant(10) > lease
    with pytest.raises(ValueError):
        store.txn({}, {'first': {}, 'large': {'a': 'x' * (1024 * 1024)}})
    assert store.get('first') is None
    assert SQLiteStore(path, clock=clock).get('key').revision > old.revision
    assert not store.txn({'key': old}, {'key': {}})
    assert store.scan('k') == {'key': store.get('key')}


@pytest.fixture
def simple_pool(tmp_path, monkeypatch):
    bindir = tmp_path / 'bin'
    bindir.mkdir()
    ssh = bindir / 'ssh'
    ssh.write_text('#!/bin/bash\nexec /bin/sh -c "${!#}"\n')
    ssh.chmod(0o755)
    monkeypatch.setenv('PATH', f"{bindir}:{os.environ['PATH']}")
    monkeypatch.setenv('PYTHONPATH', str(Path(__file__).resolve().parents[1] / 'src'))
    master, worker = tmp_path / 'master.toml', tmp_path / 'worker.toml'
    common = '[gpuq]\ndefault_queue="cluster"\ndefault_project_limit=2\n'
    master.write_text(common + '[cluster]\nbackend="sqlite"\nnode_id="a"\nnamespace="pool"\n'
                      f'work_dir={json.dumps(str(tmp_path / "a"))}\n'
                      '[cluster.nodes]\na="local"\nb="worker"\n[remotes.worker]\nhost="fake"\n'
                      f'config={json.dumps(str(worker))}\ncommand={json.dumps([sys.executable, "-m", "gpuq"])}\n')
    worker.write_text(common + '[cluster]\nbackend="ssh"\ncoordinator="master"\nnode_id="b"\nnamespace="pool"\n'
                      'gpu_allowlist=[0,1]\n'
                      f'work_dir={json.dumps(str(tmp_path / "b"))}\n'
                      '[remotes.master]\nhost="fake"\n'
                      f'config={json.dumps(str(master))}\ncommand={json.dumps([sys.executable, "-m", "gpuq"])}\n')
    source = tmp_path / 'source'
    source.mkdir()
    subprocess.run(['git', 'init', '-q', str(source)], check=True)
    (source / 'train.py').write_text('print(1)')
    launched = []
    agents = []
    for path in (master, worker):
        settings = load_settings(config_path=path)
        config = load_cluster(settings)
        instance = ClusterAgent(config, settings, backend=FakeBackend([gpu(0, free=100000), gpu(1, free=100000)]))
        def launch(job, index, agent=instance):
            launched.append((agent.config.node_id, job.id, job.assigned_gpus))
            agent.db.mark_running(job.id, pid=999999, pgid=999999)
        instance.scheduler.launcher = launch
        instance.scheduler.process_checker = lambda pid: True
        agents.append(instance)
    try:
        yield master, worker, source, agents, launched
    finally:
        agents[1].store.close()


def invoke(path, *args, code=0):
    with pytest.raises(SystemExit) as result:
        main(['--config', str(path), *args])
    assert result.value.code == code


def test_default_submit_spreads_across_hosts_and_master_is_fixed(simple_pool, capsys):
    master, worker, source, (a, b), launched = simple_pool
    invoke(master, 'cluster', 'init', '--max-running-jobs', '2', '--max-failover-retries', '0')
    assert a.queue.policy().value['single_master']
    for _ in range(2):
        invoke(master, 'submit', '--cwd', str(source), '--stable-samples', '1', '--memory', '10',
               '--reserve', '0', '--', 'python', 'train.py')
    for _ in range(5):
        b.tick()
        a.tick()
    assert len(launched) == 2
    assert {node for node, _, _ in launched} == {'a', 'b'}
    assert all(len(gpus) == 1 for _, _, gpus in launched)
    capsys.readouterr()
    invoke(master, 'list', '--json')
    rows = json.loads(capsys.readouterr().out)
    assert len(rows) == 2 and all(row['status'] == 'running' for row in rows)
    invoke(master, '--local', '--state-dir', str(source / 'independent'), 'list', '--json')
    assert json.loads(capsys.readouterr().out) == []
    a.store.txn({}, {}, deletes=('leader', 'nodes/a'))
    b.tick()
    assert a.store.get('leader') is None  # Workers never take over a simple pool.
    a.tick()
    assert a.store.get('leader').value['node'] == 'a'
    assert len(launched) == 2  # Reconnect does not launch duplicates.
    invoke(worker, 'cluster', 'init', code=2)


def test_two_jobs_use_two_cards_on_one_node(simple_pool):
    master, worker, source, (a, b), launched = simple_pool
    invoke(master, 'cluster', 'init', '--max-running-jobs', '2')
    b.config = replace(b.config, gpu_allowlist=())
    for _ in range(2):
        invoke(master, 'submit', '--cwd', str(source), '--stable-samples', '1', '--memory', '10',
               '--reserve', '0', '--', 'python', 'train.py')
    for _ in range(5):
        b.tick()
        a.tick()
    assert {gpus for _, _, gpus in launched} == {(0,), (1,)}
    assert {node for node, _, _ in launched} == {'a'}
    assert a.store.get(job_key(2)).value['job']['status'] == 'running'


def test_ssh_store_rejects_wrong_namespace_and_no_fallback(simple_pool):
    master, worker, source, (a, b), launched = simple_pool
    invoke(master, 'cluster', 'init')
    assert b.store.get('config') == a.store.get('config')
    process = b.store.process
    assert b.store.scan('config')['config'] == a.store.get('config')
    assert b.store.process is process
    b.store.close()
    b.store.namespace = 'different'
    with pytest.raises(Unavailable):
        b.store.get('config')
    assert b.store.process is None
    assert not (b.config.work_dir / 'control.sqlite3').exists()


def test_configuration_validation_and_legacy_backend(tmp_path):
    config = tmp_path / 'config.toml'
    config.write_text('[cluster]\nendpoints=["http://localhost:2379"]\n')
    assert load_cluster(load_settings(config_path=config)).backend == 'etcd'
    config.write_text('[cluster]\nnode_id="a"\n')
    assert load_cluster(load_settings(config_path=config)).backend == 'sqlite'
    for contents in ['[gpuq]\ndefault_queue="oops"', '[cluster]\nbackend="ssh"',
                     '[cluster]\ngpu_allowlist=[-1]', '[cluster]\nbackend="sqlite"\nendpoints=["http://x"]']:
        config.write_text(contents)
        with pytest.raises(ValueError):
            load_cluster(load_settings(config_path=config))


def test_single_replica_simple_pool_init(tmp_path):
    queue = ClusterQueue(SQLiteStore(tmp_path / 'control.sqlite3'))
    queue.initialize('a', limit=2, mode='fair', copies=1, failback=30)
    assert queue.policy().value['snapshot_copies'] == 1


def test_worker_disconnect_retains_result_and_reconnects_without_duplicate(simple_pool):
    master, worker, source, (a, b), launched = simple_pool
    a.config = replace(a.config, gpu_allowlist=())
    invoke(master, 'cluster', 'init', '--max-failover-retries', '0')
    invoke(master, 'submit', '--cwd', str(source), '--stable-samples', '1', '--memory', '10', '--', 'python', 'train.py')
    for _ in range(4):
        a.tick()
        b.tick()
    record = a.store.get(job_key(1)).value
    assert record['node'] == 'b' and record['job']['status'] == 'running'
    local = b.db.by_submission_key(record['execution'])
    b.store.close()
    original = b.store.profile
    b.store.profile = {**original, 'command': ['false']}
    with pytest.raises(Unavailable):
        b.tick()
    assert b.worker_db.allowed == set()
    b.db.finish(local.id, status='completed', exit_code=0)
    assert a.store.get(job_key(1)).value['job']['status'] == 'running'
    b.store.profile = original
    b.tick()
    assert a.store.get(job_key(1)).value['job']['status'] == 'completed'
    assert len(launched) == 1


def test_local_background_override_is_passed_to_child(tmp_path, monkeypatch):
    from gpuq.background import start_daemon
    from gpuq.config import Settings
    captured = []
    calls = iter([False, True])
    monkeypatch.setattr('gpuq.background.daemon_running', lambda path: next(calls))
    monkeypatch.setattr('gpuq.background.subprocess.Popen', lambda command, **kwargs: captured.append(command))
    start_daemon(Settings(state_dir=tmp_path, default_queue='cluster'))
    assert '--local' in captured[0]
    assert captured[0].index('--local') < captured[0].index('daemon')
