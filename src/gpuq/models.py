from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class JobStatus(str, Enum):
    QUEUED = "queued"
    WAITING_MEMORY = "waiting_memory"
    STARTING = "starting"
    RUNNING = "running"
    RETRY_WAIT = "retry_wait"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    BLOCKED_DEPENDENCY = "blocked_dependency"


TERMINAL_STATUSES = {
    JobStatus.COMPLETED.value,
    JobStatus.FAILED.value,
    JobStatus.CANCELLED.value,
    JobStatus.BLOCKED_DEPENDENCY.value,
}


@dataclass(frozen=True)
class GPUProcess:
    pid: int
    used_memory_mb: int


@dataclass(frozen=True)
class GPUState:
    index: int
    uuid: str
    name: str
    total_memory_mb: int
    used_memory_mb: int
    free_memory_mb: int
    utilization_percent: int
    temperature_c: int
    processes: tuple[GPUProcess, ...] = ()

    @property
    def compute_process_count(self) -> int:
        return len(self.processes)


@dataclass(frozen=True)
class Job:
    id: int
    name: str
    command: tuple[str, ...]
    cwd: str
    env: dict[str, str]
    gpu_allowlist: tuple[int, ...] | None
    memory_mb: int
    memory_mode: str
    reserve_mb: int
    max_utilization: int | None
    stable_samples: int
    priority: int
    status: str
    exclusive: bool
    dependency_id: int | None
    max_retries: int
    retry_on_oom: bool
    attempt: int
    assigned_gpu: int | None
    pid: int | None
    pgid: int | None
    worker_pid: int | None
    exit_code: int | None
    cancel_requested: bool
    log_path: str
    error: str | None
    failure_kind: str | None
    peak_memory_mb: int
    oom_count: int
    launch_free_memory_mb: int | None
    launch_total_memory_mb: int | None
    last_oom_requested_mb: int | None
    notify: bool
    created_at: str
    updated_at: str
    started_at: str | None
    finished_at: str | None
    gpu_count: int = 1
    assigned_gpus: tuple[int, ...] = ()
    launch_free_memory_by_gpu: dict[int, int] = field(default_factory=dict)

    @property
    def required_free_memory_mb(self) -> int:
        return self.memory_mb + self.reserve_mb

    @property
    def is_auto_memory(self) -> bool:
        return self.memory_mode == "auto"

    @property
    def is_uncalibrated_auto_probe(self) -> bool:
        return self.is_auto_memory and self.oom_count == 0 and self.memory_mb == 1

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "command": list(self.command),
            "cwd": self.cwd,
            "env": dict(self.env),
            "gpu_allowlist": list(self.gpu_allowlist) if self.gpu_allowlist else None,
            "gpu_count": self.gpu_count,
            "assigned_gpus": list(self.assigned_gpus),
            "launch_free_memory_by_gpu": dict(self.launch_free_memory_by_gpu),
            "memory_mb": self.memory_mb,
            "memory_mode": self.memory_mode,
            "reserve_mb": self.reserve_mb,
            "max_utilization": self.max_utilization,
            "stable_samples": self.stable_samples,
            "priority": self.priority,
            "status": self.status,
            "exclusive": self.exclusive,
            "dependency_id": self.dependency_id,
            "max_retries": self.max_retries,
            "retry_on_oom": self.retry_on_oom,
            "attempt": self.attempt,
            "assigned_gpu": self.assigned_gpu,
            "pid": self.pid,
            "pgid": self.pgid,
            "worker_pid": self.worker_pid,
            "exit_code": self.exit_code,
            "cancel_requested": self.cancel_requested,
            "log_path": self.log_path,
            "error": self.error,
            "failure_kind": self.failure_kind,
            "peak_memory_mb": self.peak_memory_mb,
            "oom_count": self.oom_count,
            "launch_free_memory_mb": self.launch_free_memory_mb,
            "launch_total_memory_mb": self.launch_total_memory_mb,
            "last_oom_requested_mb": self.last_oom_requested_mb,
            "notify": self.notify,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
        }
