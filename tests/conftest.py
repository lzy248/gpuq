from __future__ import annotations

from pathlib import Path

import pytest

from gpuq.config import Settings
from gpuq.db import QueueDB


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    # Resource-policy tests isolate GPU behavior from project admission limits.
    # Dedicated fairness tests explicitly opt into the production default, fair.
    return Settings(state_dir=tmp_path, poll_seconds=0.01, default_reserve_mb=512,
                    scheduling_mode="fifo")


@pytest.fixture
def db(settings: Settings) -> QueueDB:
    return QueueDB(settings.database_path)


def submit_job(db: QueueDB, root: Path, **overrides):
    values = {
        "name": "example",
        "command": ["python", "-c", "print('ok')"],
        "cwd": str(root),
        "env": {},
        "gpu_allowlist": None,
        "memory_mb": 4000,
        "reserve_mb": 1000,
        "max_utilization": None,
        "stable_samples": 2,
        "priority": 0,
        "exclusive": False,
        "dependency_id": None,
        "max_retries": 0,
        "retry_on_oom": False,
        "log_path": str(root / "job.log"),
        "notify": False,
    }
    values.update(overrides)
    return db.submit(**values)
