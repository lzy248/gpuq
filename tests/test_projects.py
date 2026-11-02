from dataclasses import replace
import sqlite3

import pytest

from gpuq.cli import build_parser, command_daemon
from gpuq.config import Settings, load_settings
from gpuq.db import QueueDB
from gpuq.projects import project_root
from gpuq.scheduler import Scheduler
from conftest import submit_job
from test_scheduler import FakeBackend, gpu


def scheduler_for(settings, db, **overrides):
    def launch(job, index):
        db.mark_running(job.id, pid=123, pgid=123)
    return Scheduler(settings=replace(settings, scheduling_mode="fair", **overrides), db=db,
                     backend=FakeBackend([gpu(0, free=1000000)]), launcher=launch,
                     process_checker=lambda pid: True)


def task(db, root, project, **kwargs):
    return submit_job(db, root, project_id=project, memory_mb=10, reserve_mb=0,
                      stable_samples=1, **kwargs)


def complete(db, job_id):
    db.finish(job_id, status="completed", exit_code=0)


def test_default_one_and_explicit_two_alone(settings, db, tmp_path):
    a = task(db, tmp_path, "A")
    task(db, tmp_path, "A")
    scheduler = scheduler_for(settings, db, max_running_jobs=2)
    assert scheduler.tick().job_id == a.id
    assert scheduler.tick() is None
    b = task(db, tmp_path, "A", project_limit=2)
    assert scheduler.tick() is not None
    assert scheduler.tick() is None
    assert db.get(b.id).status == "queued"


def test_competing_projects_split_two_slots(settings, db, tmp_path):
    a = task(db, tmp_path, "A", project_limit=2)
    task(db, tmp_path, "A")
    b = task(db, tmp_path, "B", project_limit=2)
    scheduler = scheduler_for(settings, db, max_running_jobs=2)
    assert scheduler.tick().job_id == a.id
    assert scheduler.tick().job_id == b.id
    assert scheduler.tick() is None
    assert {j.project_id for j in db.active()} == {"A", "B"}


def test_late_project_gets_next_slot_without_preemption(settings, db, tmp_path):
    a = task(db, tmp_path, "A", project_limit=2)
    a2 = task(db, tmp_path, "A")
    task(db, tmp_path, "A")
    scheduler = scheduler_for(settings, db, max_running_jobs=2)
    assert scheduler.tick().job_id == a.id
    assert scheduler.tick().job_id == a2.id
    b = task(db, tmp_path, "B")
    assert scheduler.tick() is None
    assert len(db.active()) == 2
    complete(db, a.id)
    assert scheduler.tick().job_id == b.id
    assert db.get(a2.id).status == "running"


def test_turns_survive_restart_and_dry_run(settings, db, tmp_path):
    for project in ["A"] * 5 + ["B"] * 3:
        task(db, tmp_path, project)
    selected = []
    for _ in range(6):
        db = QueueDB(db.path)
        scheduler = scheduler_for(settings, db, max_running_jobs=1)
        before = db.project_policies()
        scheduler.tick(dry_run=True)
        assert db.project_policies() == before
        decision = scheduler.tick()
        selected.append(db.get(decision.job_id).project_id)
        before = db.project_policies()
        assert not db.claim(decision.job_id, 0)
        assert db.project_policies() == before
        complete(db, decision.job_id)
    assert selected == ["A", "B", "A", "B", "A", "B"]


def test_priority_and_fifo_still_respect_total_and_gpu(settings, db, tmp_path):
    a = task(db, tmp_path, "A")
    high = task(db, tmp_path, "A", priority=10)
    task(db, tmp_path, "B")
    scheduler = scheduler_for(settings, db, max_running_jobs=1)
    assert scheduler.tick().job_id == high.id
    assert scheduler.tick() is None
    complete(db, high.id)
    assert scheduler.tick().job_id != a.id  # untouched B gets its first turn
    scheduler = Scheduler(settings=replace(settings, max_running_jobs=1), db=db)
    assert scheduler.order_projects(db.runnable()) == []
    for job in db.active():
        complete(db, job.id)
    assert scheduler.order_projects(db.runnable())[0].id == a.id


def test_odd_limit_and_blocked_dependencies(settings, db, tmp_path):
    for project in ["A", "B"]:
        for _ in range(4):
            task(db, tmp_path, project, project_limit=4)
    scheduler = scheduler_for(settings, db, max_running_jobs=3)
    for _ in range(3):
        assert scheduler.tick()
    assert sorted(sum(j.project_id == p for j in db.active()) for p in ["A", "B"]) == [1, 2]
    assert scheduler.tick() is None


def test_active_scan_has_no_200_limit(settings, db, tmp_path):
    for _ in range(201):
        job = task(db, tmp_path, "A")
        db.claim(job.id, 0)
    task(db, tmp_path, "B")
    assert len(db.active()) == 201
    scheduler = scheduler_for(settings, db, max_running_jobs=201)
    assert scheduler.order_projects(db.runnable()) == []


def test_detect_migrate_and_retain_project_policy(db, tmp_path):
    root = tmp_path / "project"
    child = root / "src"
    child.mkdir(parents=True)
    (root / ".git").write_text("gitdir: elsewhere")
    assert project_root(child) == root
    job = submit_job(db, child, default_project_limit=3)
    assert job.project_id == str(root)
    assert db.project_policies()[str(root)][0] == 3
    submit_job(db, child, project_limit=2)
    submit_job(db, child, default_project_limit=9)
    assert db.project_policies()[str(root)][0] == 2
    complete(db, job.id)
    assert db.retry(job.id).project_id == str(root)
    with sqlite3.connect(db.path) as conn:
        conn.execute("ALTER TABLE jobs DROP COLUMN project_id")
        conn.execute("DROP TABLE projects")
    reopened = QueueDB(db.path)
    assert reopened.get(job.id).project_id == str(root)
    assert reopened.project_policies()[str(root)][0] == 1


def test_daemon_override_preserves_settings(monkeypatch, tmp_path):
    settings = Settings(state_dir=tmp_path, exclusive_shared_gpus=(1,), max_orphan_retries=4,
                        default_project_limit=7)
    monkeypatch.setattr("gpuq.cli._settings", lambda args: settings)
    captured = []
    monkeypatch.setattr("gpuq.cli.run_daemon", lambda settings, **kw: captured.append(settings))
    args = build_parser().parse_args(["daemon", "--poll-seconds", "1", "--max-running-jobs", "2"])
    command_daemon(args)
    assert captured == [replace(settings, poll_seconds=1, max_running_jobs=2)]


@pytest.mark.parametrize("value", ['scheduling_mode = "wrong"', "max_running_jobs = -1", "default_project_limit = 0"])
def test_invalid_config(tmp_path, value):
    config = tmp_path / "config.toml"
    config.write_text("[gpuq]\n" + value)
    with pytest.raises(ValueError):
        load_settings(config_path=config)


def test_priority_never_bypasses_gpu_and_dependencies(settings, db, tmp_path):
    unavailable = task(db, tmp_path, "A", priority=100, gpu_allowlist=[9])
    blocked = task(db, tmp_path, "C", dependency_id=unavailable.id)
    runnable = task(db, tmp_path, "B")
    scheduler = scheduler_for(settings, db, max_running_jobs=2)
    assert scheduler.tick().job_id == runnable.id
    assert db.get(blocked.id).status == "queued"
    assert db.get(unavailable.id).status != "running"


def test_launch_failure_consumes_turn_and_oom_retains_project(settings, db, tmp_path):
    a = task(db, tmp_path, "A")
    b = task(db, tmp_path, "B")
    scheduler = scheduler_for(settings, db, max_running_jobs=1)
    def fail(job, gpu):
        raise RuntimeError("launch failed")
    scheduler.launcher = fail
    with pytest.raises(RuntimeError, match="launch failed"):
        scheduler.tick()
    assert db.get(a.id).status == "failed"
    assert db.project_policies()["A"][1] > 0
    db.retry(a.id)
    scheduler = scheduler_for(settings, db, max_running_jobs=1)
    assert scheduler.tick().job_id == b.id
    db.requeue_after_oom(b.id, new_memory_mb=20, error="OOM")
    assert db.get(b.id).project_id == "B"
