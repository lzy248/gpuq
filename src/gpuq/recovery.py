"""Atomic, bounded recovery of interrupted jobs; never retries terminal errors."""
from datetime import datetime, timezone

from .db import QueueDB


def requeue_orphan(db: QueueDB, job_id: int, limit: int) -> bool:
    if limit <= 0:
        return False
    with db.transaction(immediate=True) as conn:
        conn.execute("""CREATE TABLE IF NOT EXISTS orphan_recoveries (
            job_id INTEGER NOT NULL REFERENCES jobs(id),
            recovery INTEGER NOT NULL, recovered_at TEXT NOT NULL,
            previous_attempt INTEGER NOT NULL, PRIMARY KEY(job_id, recovery))""")
        row = conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
        if row is None or row['status'] not in ('starting', 'running') or row['cancel_requested']:
            return False
        count = conn.execute(
            "SELECT COUNT(*) FROM orphan_recoveries WHERE job_id=?", (job_id,)
        ).fetchone()[0]
        if count >= limit:
            return False
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        conn.execute("""UPDATE jobs SET status='queued', assigned_gpu=NULL, assigned_gpus_json=NULL,
            launch_free_memory_by_gpu_json=NULL,
            pid=NULL, pgid=NULL, worker_pid=NULL, exit_code=NULL, finished_at=NULL,
            started_at=NULL, error=NULL, failure_kind=NULL, updated_at=? WHERE id=?""",
            (now, job_id))
        conn.execute("INSERT INTO orphan_recoveries VALUES (?, ?, ?, ?)",
                     (job_id, count + 1, now, row['attempt']))
        return True
