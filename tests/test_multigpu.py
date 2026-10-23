from dataclasses import replace
import json
import os
from pathlib import Path
import signal
import sqlite3
import subprocess
import sys
import time

import pytest

from gpuq.cli import _job_line, build_parser, main
from gpuq.db import QueueDB, SCHEMA
from gpuq.models import GPUProcess
from gpuq.recovery import requeue_orphan
from gpuq.runner import execute_job
from gpuq.scheduler import Scheduler, cancel_job

from conftest import submit_job
from test_scheduler import FakeBackend, gpu


@pytest.mark.parametrize("option", ["--gpus", "--gpu-count"])
def test_cli_submits_multiple_gpus(option, tmp_path, capsys):
    with pytest.raises(SystemExit) as result:
        main([
            "--state-dir", str(tmp_path), "submit", option, "2", "--gpu", "1,3,5",
            "--memory", "12000", "--no-notify", "--", "torchrun",
            "--standalone", "--nproc_per_node=2", "train.py",
        ])
    assert result.value.code == 0
    job = QueueDB(tmp_path / "queue.sqlite3").list()[0]
    assert job.gpu_count == 2
    assert job.gpu_allowlist == (1, 3, 5)
    assert job.memory_mb == 12000
    assert job.command[-2:] == ("--nproc_per_node=2", "train.py")
    assert "per GPU" in capsys.readouterr().out
    with pytest.raises(SystemExit) as result:
        main(["--state-dir", str(tmp_path), "show", str(job.id), "--json"])
    assert result.value.code == 0
    assert json.loads(capsys.readouterr().out)["gpu_count"] == 2


@pytest.mark.parametrize("count", ["0", "-1", "1.5"])
def test_cli_rejects_invalid_gpu_count(count):
    with pytest.raises(SystemExit):
        build_parser().parse_args(["submit", "--gpus", count, "--", "true"])


@pytest.mark.parametrize("options", [
    {"gpu_count": 0}, {"gpu_count": True}, {"gpu_count": 1.5},
    {"gpu_count": 2, "gpu_allowlist": [0, 0]},
    {"gpu_count": 2, "gpu_allowlist": [0, -1]},
])
def test_invalid_gpu_requests_do_not_enter_queue(db, tmp_path, options):
    with pytest.raises(ValueError):
        submit_job(db, tmp_path, **options)
    assert db.list() == []


def test_multi_gpu_waits_without_partial_claim_and_allows_backfill(settings, db, tmp_path):
    waiting = submit_job(db, tmp_path, gpu_count=2, stable_samples=1, priority=10)
    small = submit_job(db, tmp_path, stable_samples=1, memory_mb=1000)
    scheduler = Scheduler(settings=settings, db=db, backend=FakeBackend([
        gpu(0, free=9000), gpu(1, free=3000),
    ]))
    decision = scheduler.tick(dry_run=True)
    assert decision.job_id == small.id
    assert db.get(waiting.id).assigned_gpus == ()
    assert db.get(waiting.id).attempt == 0


def test_each_gpu_must_be_stable_and_selection_uses_allowlist(settings, db, tmp_path):
    submit_job(db, tmp_path, gpu_count=2, gpu_allowlist=[1, 2, 3], stable_samples=2)
    backend = FakeBackend([
        gpu(0, free=9000, util=0), gpu(1, free=7000),
        gpu(2, free=4000), gpu(3, free=9000, util=90),
    ])
    scheduler = Scheduler(settings=settings, db=db, backend=backend)
    assert scheduler.tick(dry_run=True) is None
    backend.states[2] = gpu(2, free=8000, util=0)
    # The newly available, less busy GPU must not displace already stable cards.
    assert scheduler.tick(dry_run=True).gpu_indices == (1, 3)
    assert scheduler.tick(dry_run=True).gpu_indices == (1, 2)


@pytest.mark.parametrize("missing", [False, True])
def test_second_snapshot_rechecks_every_gpu(settings, db, tmp_path, missing):
    job = submit_job(db, tmp_path, gpu_count=2, stable_samples=1)

    class ChangingBackend(FakeBackend):
        def sample(self):
            states = self.states
            self.states = [gpu(0, free=9000)]
            if not missing:
                self.states.append(gpu(1, free=4999))
            return states

    launches = []
    scheduler = Scheduler(
        settings=settings, db=db,
        backend=ChangingBackend([gpu(0, free=9000), gpu(1, free=9000)]),
        launcher=lambda *args: launches.append(args),
    )
    assert scheduler.tick() is None
    assert launches == []
    assert db.get(job.id).assigned_gpus == ()
    assert db.get(job.id).attempt == 0


def test_all_gpus_are_claimed_and_reserved_across_daemon_restart(settings, db, tmp_path):
    first = submit_job(db, tmp_path, gpu_count=2, stable_samples=1)
    second = submit_job(db, tmp_path, stable_samples=1, gpu_allowlist=[1])
    states = [gpu(0, free=8000), gpu(1, free=8000)]

    def launch(job, _primary_gpu):
        assert job.assigned_gpus == (0, 1)
        db.mark_running(job.id, pid=101, pgid=101)

    scheduler = Scheduler(settings=settings, db=db, backend=FakeBackend(states), launcher=launch)
    assert scheduler.tick().job_id == first.id
    reopened = QueueDB(db.path)
    active = reopened.get(first.id)
    assert active.assigned_gpus == (0, 1)
    assert active.assigned_gpu == 0
    assert active.launch_free_memory_by_gpu == {0: 8000, 1: 8000}
    assert "gpu=0,1" in _job_line(active)
    restarted = Scheduler(
        settings=settings, db=reopened, backend=FakeBackend(states),
        process_checker=lambda _: True,
    )
    assert restarted.tick(dry_run=True) is None
    db.finish(first.id, status="completed", exit_code=0)
    assert restarted.tick(dry_run=True).job_id == second.id


def test_claim_rejects_partial_duplicate_and_disallowed_assignments(db, tmp_path):
    job = submit_job(db, tmp_path, gpu_count=2, gpu_allowlist=[1, 3])
    for indices in [(1,), (1, 1), (1, 2)]:
        with pytest.raises(ValueError):
            db.claim(job.id, 1, gpu_indices=indices)
        assert db.get(job.id).attempt == 0
    assert db.claim(job.id, 1, gpu_indices=(1, 3))
    assert not db.claim(job.id, 1, gpu_indices=(1, 3))
    assert db.get(job.id).attempt == 1


def test_exclusive_assignment_blocks_every_card(settings, db, tmp_path):
    active = submit_job(db, tmp_path, gpu_count=2, exclusive=True)
    assert db.claim(active.id, 0, gpu_indices=(0, 1))
    db.mark_running(active.id, pid=101, pgid=101)
    submit_job(db, tmp_path, stable_samples=1, memory_mb=1, gpu_allowlist=[1])
    scheduler = Scheduler(
        settings=settings, db=db,
        backend=FakeBackend([gpu(0, free=20000), gpu(1, free=20000)]),
        process_checker=lambda _: True,
    )
    assert scheduler.tick(dry_run=True) is None


def test_auto_probe_holds_each_cards_own_launch_region(settings, db, tmp_path):
    probe = submit_job(db, tmp_path, gpu_count=2, memory_mb=1, memory_mode="auto")
    assert db.claim(
        probe.id, 0, gpu_indices=(0, 1), launch_free_memory_mb=6000,
        launch_free_memory_by_gpu={0: 6000, 1: 20000},
    )
    db.mark_running(probe.id, pid=101, pgid=101)
    db.set_worker_pid(probe.id, 102)
    submit_job(db, tmp_path, stable_samples=1, memory_mb=7000, gpu_allowlist=[1])
    scheduler = Scheduler(
        settings=replace(settings, auto_probe_hold_seconds=0, auto_probe_stable_samples=1),
        db=db, process_checker=lambda _: True,
        backend=FakeBackend([
            gpu(0, free=5000, processes=(GPUProcess(102, 1000),)),
            gpu(1, free=19000, processes=(GPUProcess(102, 1000),)),
        ]),
    )
    assert scheduler.tick(dry_run=True) is None
    assert scheduler.tick(dry_run=True) is None


def test_distributed_rank_memory_is_summed_per_card_not_across_cards(
    settings, db, tmp_path, monkeypatch,
):
    job = submit_job(db, tmp_path, gpu_count=2, memory_mb=6000)
    assert db.claim(job.id, 0, gpu_indices=(0, 1))
    db.mark_running(job.id, pid=101, pgid=101)
    db.set_worker_pid(job.id, 102)
    monkeypatch.setattr("gpuq.scheduler.os.getpgid", lambda pid: 102 if pid < 200 else 999)
    states = [
        gpu(0, free=6000, processes=(GPUProcess(110, 2000), GPUProcess(111, 1500))),
        gpu(1, free=5000, processes=(GPUProcess(112, 4000), GPUProcess(210, 8000))),
    ]
    scheduler = Scheduler(settings=settings, db=db, backend=FakeBackend(states))
    scheduler.refresh_peaks(states)
    assert db.get(job.id).peak_memory_mb == 4000
    reservations = scheduler._reservations(states)
    assert reservations[0].outstanding_memory_mb == 2500
    assert reservations[1].outstanding_memory_mb == 2000


@pytest.mark.parametrize("states,expected", [
    ([gpu(0, free=23000), gpu(1, free=7000)], "failed"),
    ([gpu(0, free=23000), gpu(1, free=7000), gpu(2, free=23000)], "retry_wait"),
    ([gpu(0, free=23000)], "retry_wait"),
])
def test_auto_oom_needs_enough_cards_of_sufficient_capacity(settings, db, tmp_path, states, expected):
    job = submit_job(db, tmp_path, gpu_count=2, memory_mb=1, memory_mode="auto")
    db.requeue_after_oom(job.id, new_memory_mb=12000, error="oom")
    Scheduler(settings=settings, db=db, backend=FakeBackend(states)).tick(dry_run=True)
    assert db.get(job.id).status == expected
    if expected == "failed":
        assert db.get(job.id).failure_kind == "oom_exceeds_device"


def test_runner_exposes_all_assigned_gpus_and_preserves_history(db, tmp_path):
    job = submit_job(
        db, tmp_path, gpu_count=2,
        command=[sys.executable, "-c", "import os; print('VISIBLE=' + os.environ['CUDA_VISIBLE_DEVICES'])"],
        env={"CUDA_VISIBLE_DEVICES": "99"},
    )
    assert db.claim(job.id, 1, gpu_indices=(1, 3))
    db.mark_running(job.id, pid=101, pgid=101)
    assert execute_job(db, job.id, state_dir=tmp_path) == 0
    assert "VISIBLE=1,3\n" in Path(job.log_path).read_text()
    assert db.get(job.id).status == "completed"
    assert db.get(job.id).as_dict()["assigned_gpus"] == [1, 3]
    retried = db.retry(job.id)
    assert retried.gpu_count == 2
    assert retried.assigned_gpus == ()


def test_multi_gpu_oom_releases_assignment_and_retries_whole_group(db, tmp_path):
    job = submit_job(
        db, tmp_path, gpu_count=2, memory_mb=1, memory_mode="auto",
        command=[sys.executable, "-c", "print('CUDA out of memory'); raise SystemExit(1)"],
    )
    assert db.claim(
        job.id, 0, gpu_indices=(0, 1), launch_free_memory_mb=8000,
        launch_free_memory_by_gpu={0: 8000, 1: 16000},
    )
    db.mark_running(job.id, pid=101, pgid=101)
    assert execute_job(db, job.id, state_dir=tmp_path) == 1
    retried = db.get(job.id)
    assert retried.status == "retry_wait"
    assert retried.gpu_count == 2
    assert retried.assigned_gpus == ()
    assert retried.assigned_gpu is None
    assert retried.launch_free_memory_by_gpu == {}
    assert retried.memory_mb == 7256
    assert db.claim(retried.id, 2, gpu_indices=(2, 3))
    assert db.get(job.id).assigned_gpus == (2, 3)


def test_orphan_recovery_releases_whole_group_and_preserves_gpu_count(db, tmp_path):
    job = submit_job(db, tmp_path, gpu_count=2, gpu_allowlist=[1, 3])
    assert db.claim(job.id, 1, gpu_indices=(1, 3))
    assert requeue_orphan(db, job.id, 1)
    recovered = db.get(job.id)
    assert recovered.gpu_count == 2
    assert recovered.gpu_allowlist == (1, 3)
    assert recovered.assigned_gpus == ()
    assert recovered.assigned_gpu is None


def test_legacy_database_migration_preserves_active_single_gpu_job(tmp_path):
    path = tmp_path / "legacy.sqlite3"
    schema = SCHEMA
    for line in (
        "    gpu_count INTEGER NOT NULL DEFAULT 1 CHECK(gpu_count > 0),\n",
        "    assigned_gpus_json TEXT,\n",
        "    launch_free_memory_by_gpu_json TEXT,\n",
    ):
        schema = schema.replace(line, "")
    with sqlite3.connect(path) as connection:
        connection.executescript(schema)
        connection.execute("""
            INSERT INTO jobs (name, command_json, cwd, memory_mb, reserve_mb,
                stable_samples, status, assigned_gpu, log_path, created_at, updated_at)
            VALUES ('legacy', '["true"]', '.', 4000, 1000, 1, 'running', 3,
                'legacy.log', '2026-10-23T00:00:00+00:00', '2026-10-23T00:00:00+00:00')
        """)
    migrated = QueueDB(path)
    job = migrated.get(1)
    assert job.gpu_count == 1
    assert job.assigned_gpus == (3,)
    assert job.status == "running"
    assert QueueDB(path).get(1).assigned_gpus == (3,)


def test_real_runner_launches_two_ranks_with_the_full_visible_set(
    settings, db, tmp_path, monkeypatch,
):
    monkeypatch.setenv("PYTHONPATH", str(Path(__file__).resolve().parents[1] / "src"))
    script = """
import subprocess, sys
code = "import os; print('RANK_VISIBLE=' + os.environ['CUDA_VISIBLE_DEVICES'])"
ranks = [subprocess.Popen([sys.executable, '-c', code], start_new_session=True) for _ in range(2)]
raise SystemExit(max(rank.wait() for rank in ranks))
"""
    job = submit_job(
        db, tmp_path, gpu_count=2, stable_samples=1,
        command=[sys.executable, "-c", script],
    )
    scheduler = Scheduler(
        settings=settings, db=db, backend=FakeBackend([gpu(1, free=9000), gpu(3, free=9000)]),
    )
    assert scheduler.tick().gpu_indices == (1, 3)
    runner = scheduler.runners[job.id]
    try:
        assert runner.wait(timeout=10) == 0
        assert db.get(job.id).status == "completed"
        assert Path(job.log_path).read_text().count("RANK_VISIBLE=1,3\n") == 2
    finally:
        if runner.poll() is None:
            cancel_job(db, job.id, force_after=0.1)
            runner.kill()
            runner.wait(timeout=5)


def test_rank_sessions_are_measured_and_launcher_forwards_cancellation(
    settings, db, tmp_path, monkeypatch,
):
    monkeypatch.setenv("PYTHONPATH", str(Path(__file__).resolve().parents[1] / "src"))
    ready = tmp_path / "ranks.json"
    # Like torchrun, this launcher starts ranks in separate sessions and forwards
    # SIGTERM to them. Keep an unrelated process alive to check isolation.
    script = """
import json, os, pathlib, signal, subprocess, sys, time
ranks = []
def stop(signum, frame):
    for rank in ranks:
        rank.terminate()
    for rank in ranks:
        rank.wait()
    raise SystemExit(128 + signum)
signal.signal(signal.SIGTERM, stop)
for _ in range(2):
    ranks.append(subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'],
                                  start_new_session=True))
pathlib.Path(sys.argv[1]).write_text(json.dumps([rank.pid for rank in ranks]))
for rank in ranks:
    rank.wait()
"""
    outsider = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    job = submit_job(
        db, tmp_path, gpu_count=2, stable_samples=1,
        command=[sys.executable, "-c", script, str(ready)],
    )
    scheduler = Scheduler(
        settings=settings, db=db, backend=FakeBackend([gpu(0, free=9000), gpu(1, free=9000)]),
    )
    rank_pids = []
    runner = None
    try:
        assert scheduler.tick() is not None
        runner = scheduler.runners[job.id]
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            try:
                rank_pids = json.loads(ready.read_text())
                break
            except (FileNotFoundError, json.JSONDecodeError):
                time.sleep(0.02)
        assert len(rank_pids) == 2
        active = db.get(job.id)
        assert all(os.getpgid(pid) != active.worker_pid for pid in rank_pids)
        states = [
            gpu(0, free=6000, processes=(GPUProcess(rank_pids[0], 2000),)),
            gpu(1, free=5000, processes=(GPUProcess(rank_pids[1], 3000),
                                        GPUProcess(outsider.pid, 9000))),
        ]
        scheduler.refresh_peaks(states)
        assert db.get(job.id).peak_memory_mb == 3000
        assert scheduler._reservations(states)[1].outstanding_memory_mb == 1000
        assert cancel_job(db, job.id, force_after=2).status == "cancelled"
        assert runner.wait(timeout=5) != 0
        assert all(not Path(f"/proc/{pid}").exists() for pid in rank_pids)
        assert outsider.poll() is None
    finally:
        if runner is not None and runner.poll() is None:
            cancel_job(db, job.id, force_after=0.1)
            runner.kill()
            runner.wait(timeout=5)
        for pid in rank_pids:
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        outsider.terminate()
        outsider.wait(timeout=5)
