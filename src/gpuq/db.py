from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3
from typing import Any, Iterator, Sequence

from .models import Job, JobStatus, TERMINAL_STATUSES


SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    command_json TEXT NOT NULL,
    cwd TEXT NOT NULL,
    env_json TEXT NOT NULL DEFAULT '{}',
    gpu_allowlist_json TEXT,
    gpu_count INTEGER NOT NULL DEFAULT 1 CHECK(gpu_count > 0),
    assigned_gpus_json TEXT,
    launch_free_memory_by_gpu_json TEXT,
    memory_mb INTEGER NOT NULL CHECK(memory_mb > 0),
    memory_mode TEXT NOT NULL DEFAULT 'fixed' CHECK(memory_mode IN ('fixed', 'auto')),
    reserve_mb INTEGER NOT NULL CHECK(reserve_mb >= 0),
    max_utilization INTEGER,
    stable_samples INTEGER NOT NULL CHECK(stable_samples > 0),
    priority INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL,
    exclusive INTEGER NOT NULL DEFAULT 0,
    dependency_id INTEGER REFERENCES jobs(id),
    max_retries INTEGER NOT NULL DEFAULT 0 CHECK(max_retries >= 0),
    retry_on_oom INTEGER NOT NULL DEFAULT 0,
    attempt INTEGER NOT NULL DEFAULT 0,
    assigned_gpu INTEGER,
    pid INTEGER,
    pgid INTEGER,
    worker_pid INTEGER,
    exit_code INTEGER,
    cancel_requested INTEGER NOT NULL DEFAULT 0,
    log_path TEXT NOT NULL,
    error TEXT,
    failure_kind TEXT,
    peak_memory_mb INTEGER NOT NULL DEFAULT 0,
    oom_count INTEGER NOT NULL DEFAULT 0,
    launch_free_memory_mb INTEGER,
    launch_total_memory_mb INTEGER,
    last_oom_requested_mb INTEGER,
    notify INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    started_at TEXT,
    finished_at TEXT
);
CREATE INDEX IF NOT EXISTS jobs_status_priority_idx
    ON jobs(status, priority DESC, id ASC);
CREATE INDEX IF NOT EXISTS jobs_dependency_idx ON jobs(dependency_id);
"""


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class QueueDB:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path).expanduser().resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.initialize()

    def connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA busy_timeout=30000")
        return connection

    def initialize(self) -> None:
        with self.connect() as connection:
            connection.executescript(SCHEMA)
            columns = {
                str(row["name"])
                for row in connection.execute("PRAGMA table_info(jobs)").fetchall()
            }
            migrations = {
                "gpu_count": "INTEGER NOT NULL DEFAULT 1",
                "assigned_gpus_json": "TEXT",
                "launch_free_memory_by_gpu_json": "TEXT",
                "memory_mode": "TEXT NOT NULL DEFAULT 'fixed'",
                "oom_count": "INTEGER NOT NULL DEFAULT 0",
                "launch_free_memory_mb": "INTEGER",
                "launch_total_memory_mb": "INTEGER",
                "last_oom_requested_mb": "INTEGER",
            }
            for name, definition in migrations.items():
                if name not in columns:
                    connection.execute(
                        f"ALTER TABLE jobs ADD COLUMN {name} {definition}"
                    )

    @contextmanager
    def transaction(self, *, immediate: bool = False) -> Iterator[sqlite3.Connection]:
        connection = self.connect()
        try:
            connection.execute("BEGIN IMMEDIATE" if immediate else "BEGIN")
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    @staticmethod
    def _job(row: sqlite3.Row) -> Job:
        allowlist = json.loads(row["gpu_allowlist_json"]) if row["gpu_allowlist_json"] else None
        return Job(
            id=int(row["id"]),
            name=str(row["name"]),
            command=tuple(json.loads(row["command_json"])),
            cwd=str(row["cwd"]),
            env=dict(json.loads(row["env_json"])),
            gpu_allowlist=tuple(int(value) for value in allowlist) if allowlist else None,
            gpu_count=int(row["gpu_count"]),
            assigned_gpus=(
                tuple(json.loads(row["assigned_gpus_json"]))
                if row["assigned_gpus_json"]
                else ((int(row["assigned_gpu"]),) if row["assigned_gpu"] is not None else ())
            ),
            launch_free_memory_by_gpu={
                int(key): int(value)
                for key, value in json.loads(
                    row["launch_free_memory_by_gpu_json"] or "{}"
                ).items()
            },
            memory_mb=int(row["memory_mb"]),
            memory_mode=str(row["memory_mode"]),
            reserve_mb=int(row["reserve_mb"]),
            max_utilization=(int(row["max_utilization"]) if row["max_utilization"] is not None else None),
            stable_samples=int(row["stable_samples"]),
            priority=int(row["priority"]),
            status=str(row["status"]),
            exclusive=bool(row["exclusive"]),
            dependency_id=(int(row["dependency_id"]) if row["dependency_id"] is not None else None),
            max_retries=int(row["max_retries"]),
            retry_on_oom=bool(row["retry_on_oom"]),
            attempt=int(row["attempt"]),
            assigned_gpu=(int(row["assigned_gpu"]) if row["assigned_gpu"] is not None else None),
            pid=(int(row["pid"]) if row["pid"] is not None else None),
            pgid=(int(row["pgid"]) if row["pgid"] is not None else None),
            worker_pid=(int(row["worker_pid"]) if row["worker_pid"] is not None else None),
            exit_code=(int(row["exit_code"]) if row["exit_code"] is not None else None),
            cancel_requested=bool(row["cancel_requested"]),
            log_path=str(row["log_path"]),
            error=(str(row["error"]) if row["error"] is not None else None),
            failure_kind=(str(row["failure_kind"]) if row["failure_kind"] is not None else None),
            peak_memory_mb=int(row["peak_memory_mb"]),
            oom_count=int(row["oom_count"]),
            launch_free_memory_mb=(
                int(row["launch_free_memory_mb"])
                if row["launch_free_memory_mb"] is not None
                else None
            ),
            launch_total_memory_mb=(
                int(row["launch_total_memory_mb"])
                if row["launch_total_memory_mb"] is not None
                else None
            ),
            last_oom_requested_mb=(
                int(row["last_oom_requested_mb"])
                if row["last_oom_requested_mb"] is not None
                else None
            ),
            notify=bool(row["notify"]),
            created_at=str(row["created_at"]),
            updated_at=str(row["updated_at"]),
            started_at=(str(row["started_at"]) if row["started_at"] is not None else None),
            finished_at=(str(row["finished_at"]) if row["finished_at"] is not None else None),
        )

    def submit(
        self,
        *,
        name: str,
        command: Sequence[str],
        cwd: str,
        env: dict[str, str],
        gpu_allowlist: Sequence[int] | None,
        memory_mb: int,
        reserve_mb: int,
        max_utilization: int | None,
        stable_samples: int,
        priority: int,
        exclusive: bool,
        dependency_id: int | None,
        max_retries: int,
        retry_on_oom: bool,
        log_path: str,
        notify: bool,
        memory_mode: str = "fixed",
        gpu_count: int = 1,
    ) -> Job:
        if not command:
            raise ValueError("command cannot be empty")
        if memory_mb < 1 or reserve_mb < 0 or stable_samples < 1 or max_retries < 0:
            raise ValueError("invalid resource or retry value")
        if memory_mode not in {"fixed", "auto"}:
            raise ValueError("memory_mode must be fixed or auto")
        if isinstance(gpu_count, bool) or not isinstance(gpu_count, int) or gpu_count < 1:
            raise ValueError("gpu_count must be a positive integer")
        if gpu_allowlist is not None:
            gpu_allowlist = tuple(dict.fromkeys(gpu_allowlist))
            if any(index < 0 for index in gpu_allowlist):
                raise ValueError("GPU indices cannot be negative")
            if len(gpu_allowlist) < gpu_count:
                raise ValueError("gpu_count exceeds the number of distinct allowed GPUs")
        if max_utilization is not None and not 0 <= max_utilization <= 100:
            raise ValueError("max_utilization must be in [0, 100]")
        now = utcnow()
        with self.transaction(immediate=True) as connection:
            if dependency_id is not None:
                dependency = connection.execute(
                    "SELECT id FROM jobs WHERE id=?", (dependency_id,)
                ).fetchone()
                if dependency is None:
                    raise ValueError(f"dependency job {dependency_id} does not exist")
            cursor = connection.execute(
                """
                INSERT INTO jobs (
                    name, command_json, cwd, env_json, gpu_allowlist_json,
                    memory_mb, memory_mode, reserve_mb, max_utilization, stable_samples,
                    priority, status, exclusive, dependency_id, max_retries,
                    retry_on_oom, log_path, notify, created_at, updated_at, gpu_count
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    name,
                    json.dumps(list(command), ensure_ascii=False),
                    cwd,
                    json.dumps(env, ensure_ascii=False, sort_keys=True),
                    json.dumps(list(gpu_allowlist)) if gpu_allowlist else None,
                    memory_mb,
                    memory_mode,
                    reserve_mb,
                    max_utilization,
                    stable_samples,
                    priority,
                    JobStatus.QUEUED.value,
                    int(exclusive),
                    dependency_id,
                    max_retries,
                    int(retry_on_oom),
                    log_path,
                    int(notify),
                    now,
                    now,
                    gpu_count,
                ),
            )
            job_id = int(cursor.lastrowid)
        return self.get(job_id)

    def get(self, job_id: int) -> Job:
        with self.connect() as connection:
            row = connection.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
        if row is None:
            raise KeyError(f"job {job_id} not found")
        return self._job(row)

    def list(self, *, statuses: Sequence[str] | None = None, limit: int = 200) -> list[Job]:
        query = "SELECT * FROM jobs"
        parameters: list[Any] = []
        if statuses:
            placeholders = ",".join("?" for _ in statuses)
            query += f" WHERE status IN ({placeholders})"
            parameters.extend(statuses)
        query += " ORDER BY CASE WHEN status IN ('running','starting') THEN 0 ELSE 1 END, priority DESC, id ASC LIMIT ?"
        parameters.append(limit)
        with self.connect() as connection:
            rows = connection.execute(query, parameters).fetchall()
        return [self._job(row) for row in rows]

    def runnable(self) -> list[Job]:
        with self.transaction(immediate=True) as connection:
            failed_dependencies = connection.execute(
                """
                SELECT child.id, parent.status FROM jobs child
                JOIN jobs parent ON parent.id=child.dependency_id
                WHERE child.status IN (?, ?, ?) AND parent.status IN (?, ?, ?)
                """,
                (
                    JobStatus.QUEUED.value,
                    JobStatus.WAITING_MEMORY.value,
                    JobStatus.RETRY_WAIT.value,
                    JobStatus.FAILED.value,
                    JobStatus.CANCELLED.value,
                    JobStatus.BLOCKED_DEPENDENCY.value,
                ),
            ).fetchall()
            now = utcnow()
            for row in failed_dependencies:
                connection.execute(
                    "UPDATE jobs SET status=?, error=?, updated_at=?, finished_at=? WHERE id=?",
                    (
                        JobStatus.BLOCKED_DEPENDENCY.value,
                        f"dependency ended as {row['status']}",
                        now,
                        now,
                        int(row["id"]),
                    ),
                )
            rows = connection.execute(
                """
                SELECT child.* FROM jobs child
                LEFT JOIN jobs parent ON parent.id=child.dependency_id
                WHERE child.status IN (?, ?, ?)
                  AND (child.dependency_id IS NULL OR parent.status=? )
                ORDER BY child.priority DESC, child.id ASC
                """,
                (
                    JobStatus.QUEUED.value,
                    JobStatus.WAITING_MEMORY.value,
                    JobStatus.RETRY_WAIT.value,
                    JobStatus.COMPLETED.value,
                ),
            ).fetchall()
        return [self._job(row) for row in rows]

    def set_waiting_memory(self, job_id: int) -> None:
        with self.connect() as connection:
            connection.execute(
                "UPDATE jobs SET status=?, updated_at=? WHERE id=? AND status IN (?, ?, ?)",
                (
                    JobStatus.WAITING_MEMORY.value,
                    utcnow(),
                    job_id,
                    JobStatus.QUEUED.value,
                    JobStatus.WAITING_MEMORY.value,
                    JobStatus.RETRY_WAIT.value,
                ),
            )

    def claim(
        self,
        job_id: int,
        gpu_index: int,
        *,
        launch_free_memory_mb: int | None = None,
        launch_total_memory_mb: int | None = None,
        gpu_indices: Sequence[int] | None = None,
        launch_free_memory_by_gpu: dict[int, int] | None = None,
    ) -> bool:
        indices = tuple(gpu_indices) if gpu_indices is not None else (gpu_index,)
        if not indices or indices[0] != gpu_index or len(set(indices)) != len(indices):
            raise ValueError("assigned GPUs must be distinct and start with gpu_index")
        if any(index < 0 for index in indices):
            raise ValueError("GPU indices cannot be negative")
        with self.transaction(immediate=True) as connection:
            row = connection.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
            if row is None:
                return False
            job = self._job(row)
            if len(indices) != job.gpu_count:
                raise ValueError("assigned GPU count does not match requested gpu_count")
            if job.gpu_allowlist is not None and not set(indices) <= set(job.gpu_allowlist):
                raise ValueError("assigned GPU is outside the allowlist")
            cursor = connection.execute(
                """
                UPDATE jobs SET status=?, assigned_gpu=?, attempt=attempt+1,
                    updated_at=?, started_at=COALESCE(started_at, ?), error=NULL,
                    failure_kind=NULL, cancel_requested=0,
                    launch_free_memory_mb=?, launch_total_memory_mb=?,
                    assigned_gpus_json=?, launch_free_memory_by_gpu_json=?
                WHERE id=? AND status IN (?, ?, ?)
                """,
                (
                    JobStatus.STARTING.value,
                    gpu_index,
                    utcnow(),
                    utcnow(),
                    launch_free_memory_mb,
                    launch_total_memory_mb,
                    json.dumps(indices),
                    json.dumps(launch_free_memory_by_gpu or {}),
                    job_id,
                    JobStatus.QUEUED.value,
                    JobStatus.WAITING_MEMORY.value,
                    JobStatus.RETRY_WAIT.value,
                ),
            )
            return cursor.rowcount == 1

    def mark_running(self, job_id: int, *, pid: int, pgid: int) -> None:
        with self.connect() as connection:
            connection.execute(
                "UPDATE jobs SET status=?, pid=?, pgid=?, updated_at=? WHERE id=? AND status=?",
                (
                    JobStatus.RUNNING.value,
                    pid,
                    pgid,
                    utcnow(),
                    job_id,
                    JobStatus.STARTING.value,
                ),
            )

    def set_worker_pid(self, job_id: int, worker_pid: int) -> None:
        with self.connect() as connection:
            connection.execute(
                "UPDATE jobs SET worker_pid=?, updated_at=? WHERE id=?",
                (worker_pid, utcnow(), job_id),
            )

    def update_peak(self, job_id: int, peak_memory_mb: int) -> None:
        with self.connect() as connection:
            connection.execute(
                "UPDATE jobs SET peak_memory_mb=MAX(peak_memory_mb, ?), updated_at=? WHERE id=?",
                (peak_memory_mb, utcnow(), job_id),
            )

    def finish(
        self,
        job_id: int,
        *,
        status: str,
        exit_code: int | None,
        error: str | None = None,
        failure_kind: str | None = None,
    ) -> None:
        if status not in TERMINAL_STATUSES:
            raise ValueError("finish needs a terminal status")
        with self.connect() as connection:
            connection.execute(
                """
                UPDATE jobs SET status=?, exit_code=?, error=?, failure_kind=?,
                    finished_at=?, updated_at=?, pid=NULL, pgid=NULL, worker_pid=NULL,
                    memory_mb=CASE
                        WHEN ?=? AND memory_mode='auto'
                        THEN MAX(memory_mb, peak_memory_mb)
                        ELSE memory_mb
                    END
                WHERE id=?
                """,
                (
                    status,
                    exit_code,
                    error,
                    failure_kind,
                    utcnow(),
                    utcnow(),
                    status,
                    JobStatus.COMPLETED.value,
                    job_id,
                ),
            )

    def fail_if_active(
        self,
        job_id: int,
        *,
        error: str,
        failure_kind: str,
    ) -> bool:
        """Atomically fail a stale active record without overwriting a late runner result."""
        now = utcnow()
        with self.connect() as connection:
            cursor = connection.execute(
                """
                UPDATE jobs SET status=?, exit_code=NULL, error=?, failure_kind=?,
                    finished_at=?, updated_at=?, assigned_gpu=NULL, assigned_gpus_json=NULL,
                    launch_free_memory_by_gpu_json=NULL, pid=NULL,
                    pgid=NULL, worker_pid=NULL
                WHERE id=? AND status IN (?, ?)
                """,
                (
                    JobStatus.FAILED.value,
                    error,
                    failure_kind,
                    now,
                    now,
                    job_id,
                    JobStatus.STARTING.value,
                    JobStatus.RUNNING.value,
                ),
            )
        return cursor.rowcount == 1

    def requeue_after_oom(
        self,
        job_id: int,
        *,
        new_memory_mb: int,
        error: str,
        requested_mb: int | None = None,
    ) -> None:
        with self.connect() as connection:
            connection.execute(
                """
                UPDATE jobs SET status=?, memory_mb=?, error=?, failure_kind='oom',
                    assigned_gpu=NULL, assigned_gpus_json=NULL,
                    launch_free_memory_by_gpu_json=NULL, pid=NULL, pgid=NULL, worker_pid=NULL,
                    exit_code=NULL, cancel_requested=0, oom_count=oom_count+1,
                    last_oom_requested_mb=?, updated_at=? WHERE id=?
                """,
                (
                    JobStatus.RETRY_WAIT.value,
                    new_memory_mb,
                    error,
                    requested_mb,
                    utcnow(),
                    job_id,
                ),
            )

    def fail_impossible_memory(
        self,
        job_id: int,
        *,
        required_mb: int,
        maximum_mb: int,
    ) -> bool:
        now = utcnow()
        with self.connect() as connection:
            cursor = connection.execute(
                """
                UPDATE jobs SET status=?, exit_code=NULL,
                    error=?, failure_kind='oom_exceeds_device', finished_at=?, updated_at=?,
                    assigned_gpu=NULL, assigned_gpus_json=NULL,
                    launch_free_memory_by_gpu_json=NULL, pid=NULL, pgid=NULL, worker_pid=NULL
                WHERE id=? AND status IN (?, ?, ?)
                """,
                (
                    JobStatus.FAILED.value,
                    f"learned required free memory {required_mb}MB per GPU exceeds "
                    f"available group capacity {maximum_mb}MB per GPU",
                    now,
                    now,
                    job_id,
                    JobStatus.QUEUED.value,
                    JobStatus.WAITING_MEMORY.value,
                    JobStatus.RETRY_WAIT.value,
                ),
            )
        return cursor.rowcount == 1

    def request_cancel(self, job_id: int) -> Job:
        with self.transaction(immediate=True) as connection:
            row = connection.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
            if row is None:
                raise KeyError(f"job {job_id} not found")
            status = str(row["status"])
            if status in TERMINAL_STATUSES:
                return self._job(row)
            if status in {
                JobStatus.QUEUED.value,
                JobStatus.WAITING_MEMORY.value,
                JobStatus.RETRY_WAIT.value,
            }:
                now = utcnow()
                connection.execute(
                    "UPDATE jobs SET status=?, cancel_requested=1, finished_at=?, updated_at=? WHERE id=?",
                    (JobStatus.CANCELLED.value, now, now, job_id),
                )
            else:
                connection.execute(
                    "UPDATE jobs SET cancel_requested=1, updated_at=? WHERE id=?",
                    (utcnow(), job_id),
                )
        return self.get(job_id)

    def retry(
        self,
        job_id: int,
        *,
        memory_mb: int | None = None,
        memory_mode: str | None = None,
        reset_attempts: bool = False,
    ) -> Job:
        with self.transaction(immediate=True) as connection:
            row = connection.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
            if row is None:
                raise KeyError(f"job {job_id} not found")
            if str(row["status"]) not in TERMINAL_STATUSES:
                raise ValueError("only terminal jobs can be retried")
            new_mode = str(memory_mode or row["memory_mode"])
            if new_mode not in {"fixed", "auto"}:
                raise ValueError("memory_mode must be fixed or auto")
            new_memory = int(memory_mb if memory_mb is not None else row["memory_mb"])
            if new_memory < 1:
                raise ValueError("memory_mb must be positive")
            reset_learning = memory_mode is not None
            connection.execute(
                """
                UPDATE jobs SET status=?, memory_mb=?, memory_mode=?, assigned_gpu=NULL, assigned_gpus_json=NULL,
                    launch_free_memory_by_gpu_json=NULL, pid=NULL,
                    pgid=NULL, worker_pid=NULL, exit_code=NULL, cancel_requested=0,
                    error=NULL, failure_kind=NULL, finished_at=NULL,
                    retry_on_oom=CASE WHEN ?='auto' THEN 1 ELSE retry_on_oom END,
                    oom_count=CASE WHEN ? THEN 0 ELSE oom_count END,
                    launch_free_memory_mb=CASE WHEN ? THEN NULL ELSE launch_free_memory_mb END,
                    launch_total_memory_mb=CASE WHEN ? THEN NULL ELSE launch_total_memory_mb END,
                    last_oom_requested_mb=CASE WHEN ? THEN NULL ELSE last_oom_requested_mb END,
                    attempt=CASE WHEN ? THEN 0 ELSE attempt END, updated_at=? WHERE id=?
                """,
                (
                    JobStatus.QUEUED.value,
                    new_memory,
                    new_mode,
                    new_mode,
                    int(reset_learning),
                    int(reset_learning),
                    int(reset_learning),
                    int(reset_learning),
                    int(reset_attempts),
                    utcnow(),
                    job_id,
                ),
            )
        return self.get(job_id)

    def set_priority(self, job_id: int, priority: int) -> Job:
        with self.connect() as connection:
            cursor = connection.execute(
                "UPDATE jobs SET priority=?, updated_at=? WHERE id=?",
                (priority, utcnow(), job_id),
            )
            if cursor.rowcount != 1:
                raise KeyError(f"job {job_id} not found")
        return self.get(job_id)

    def active(self) -> list[Job]:
        return self.list(statuses=[JobStatus.STARTING.value, JobStatus.RUNNING.value])
