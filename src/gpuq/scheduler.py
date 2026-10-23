from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
import fcntl
import os
import signal
import subprocess
import sys
import time
from typing import Callable

from .config import Settings, ensure_state_dirs
from .db import QueueDB
from .gpu import GPUBackend, NvidiaSMIBackend
from .models import GPUState, Job, JobStatus, TERMINAL_STATUSES
from .recovery import requeue_orphan


@dataclass(frozen=True)
class LaunchDecision:
    job_id: int
    gpu_index: int
    stable_count: int
    free_memory_mb: int
    required_memory_mb: int
    outstanding_reservation_mb: int
    utilization_percent: int
    gpu_indices: tuple[int, ...] = ()


@dataclass(frozen=True)
class GPUReservation:
    outstanding_memory_mb: int = 0
    reserve_mb: int = 0
    active_jobs: int = 0
    exclusive_active: bool = False


@dataclass
class AutoProbeObservation:
    first_seen: float
    samples: list[int]


def process_exists(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def process_descends_from(pid: int, ancestor: int) -> bool:
    """Recognize ranks that torchrun starts in separate process groups."""
    seen: set[int] = set()
    while pid > 1 and pid not in seen:
        if pid == ancestor:
            return True
        seen.add(pid)
        try:
            # The comm field can contain spaces and parentheses. Fields after
            # its final ')' start with state, then parent PID.
            fields = Path(f"/proc/{pid}/stat").read_text().rpartition(")")[2].split()
            pid = int(fields[1])
        except (OSError, ValueError, IndexError):
            return False
    return False


def runner_command(state_dir: str, job_id: int, *, frozen: bool | None = None) -> list[str]:
    is_frozen = getattr(sys, "frozen", False) if frozen is None else frozen
    if is_frozen:
        return [
            sys.executable,
            "--gpuq-internal-runner",
            "--state-dir",
            state_dir,
            "--job-id",
            str(job_id),
        ]
    return [
        sys.executable,
        "-m",
        "gpuq.runner",
        "--state-dir",
        state_dir,
        "--job-id",
        str(job_id),
    ]


class Scheduler:
    def __init__(
        self,
        *,
        settings: Settings,
        db: QueueDB | None = None,
        backend: GPUBackend | None = None,
        launcher: Callable[[Job, int], None] | None = None,
        process_checker: Callable[[int], bool] | None = None,
        clock: Callable[[], float] | None = None,
    ) -> None:
        self.settings = settings
        ensure_state_dirs(settings)
        self.db = db or QueueDB(settings.database_path)
        self.backend = backend or NvidiaSMIBackend(settings.nvidia_smi)
        self.launcher = launcher or self._launch
        self.process_checker = process_checker or process_exists
        self.clock = clock or time.monotonic
        self.stability: dict[tuple[int, int], int] = {}
        self.auto_probe_observations: dict[int, AutoProbeObservation] = {}
        self.runners: dict[int, subprocess.Popen[bytes]] = {}
        self.boot_time = None
        try:
            self.boot_time = next(float(line.split()[1]) for line in
                                  Path('/proc/stat').read_text().splitlines()
                                  if line.startswith('btime '))
        except (OSError, StopIteration, ValueError):
            pass

    def _auto_probe_can_share(self, job: Job) -> bool:
        # Multi-GPU ranks can initialize at different times. Keep each card's
        # launch region reserved throughout the uncalibrated attempt.
        if job.gpu_count > 1:
            return False
        observation = self.auto_probe_observations.get(job.id)
        if observation is None:
            return False
        if self.clock() - observation.first_seen < self.settings.auto_probe_hold_seconds:
            return False
        if len(observation.samples) < self.settings.auto_probe_stable_samples:
            return False
        return (
            max(observation.samples) - min(observation.samples)
            <= self.settings.auto_probe_stability_tolerance_mb
        )

    def reconcile_active(self) -> list[int]:
        """Recover (when enabled) or fail records whose processes disappeared.

        A live runner or worker is preserved across daemon restarts.  The
        conditional database update avoids racing a runner that writes its
        terminal state between the process check and this method.
        """
        recovered: list[int] = []
        for job in self.db.active():
            before_boot = (self.boot_time is not None
                           and datetime.fromisoformat(job.updated_at).timestamp() < self.boot_time)
            runner_alive = job.pid is not None and self.process_checker(job.pid)
            worker_alive = (
                job.worker_pid is not None and self.process_checker(job.worker_pid)
            )
            if not before_boot and (runner_alive or worker_alive):
                continue
            if requeue_orphan(self.db, job.id, self.settings.max_orphan_retries):
                recovered.append(job.id)
                print(f'gpuqd automatically requeued interrupted job={job.id}', flush=True)
                continue
            if self.db.fail_if_active(
                job.id,
                error="runner and worker disappeared before recording an exit status",
                failure_kind="orphaned",
            ):
                recovered.append(job.id)
        return recovered

    @staticmethod
    def _observed_memory(job: Job, states: list[GPUState]) -> dict[int, int]:
        """Sum this job's ranks on each card, never sum across cards."""
        observed: dict[int, int] = {}
        if job.worker_pid is None:
            return observed
        for state in states:
            if state.index not in job.assigned_gpus:
                continue
            for process in state.processes:
                owned = process.pid == job.worker_pid
                if not owned and job.gpu_count > 1:
                    try:
                        owned = os.getpgid(process.pid) == job.worker_pid
                    except OSError:
                        pass
                    if not owned:
                        owned = process_descends_from(process.pid, job.worker_pid)
                if owned:
                    observed[state.index] = (
                        observed.get(state.index, 0) + process.used_memory_mb
                    )
        return observed

    def _reservations(self, states: list[GPUState]) -> dict[int, GPUReservation]:
        reservations: dict[int, GPUReservation] = {}
        for job in self.db.active():
            observed_by_gpu = self._observed_memory(job, states)
            for gpu_index in job.assigned_gpus:
                current = reservations.get(gpu_index, GPUReservation())
                observed = observed_by_gpu.get(gpu_index, 0)
                target_memory = job.memory_mb
                probe_margin = job.reserve_mb
                launch_free = job.launch_free_memory_by_gpu.get(
                    gpu_index, job.launch_free_memory_mb
                )
                if job.is_uncalibrated_auto_probe and launch_free is not None:
                    probe_margin = max(probe_margin, self.settings.auto_probe_release_margin_mb)
                    if job.exclusive or not self._auto_probe_can_share(job):
                        target_memory = max(target_memory, launch_free - probe_margin)
                    else:
                        target_memory = max(target_memory, job.peak_memory_mb, observed)
                reservations[gpu_index] = GPUReservation(
                    outstanding_memory_mb=(
                        current.outstanding_memory_mb + max(0, target_memory - observed)
                    ),
                    reserve_mb=max(current.reserve_mb, probe_margin),
                    active_jobs=current.active_jobs + 1,
                    exclusive_active=current.exclusive_active or job.exclusive,
                )
        return reservations

    @staticmethod
    def _fit(
        job: Job,
        state: GPUState,
        reservation: GPUReservation,
    ) -> tuple[bool, int, int]:
        schedulable_free = max(
            0, state.free_memory_mb - reservation.outstanding_memory_mb
        )
        required = job.memory_mb + max(job.reserve_mb, reservation.reserve_mb)
        return schedulable_free >= required, schedulable_free - required, required

    def _allowed(
        self,
        job: Job,
        state: GPUState,
        reservation: GPUReservation,
    ) -> bool:
        if job.gpu_allowlist is not None and state.index not in job.gpu_allowlist:
            return False
        if reservation.exclusive_active:
            return False
        fits, _, _ = self._fit(job, state, reservation)
        if not fits:
            return False
        if job.max_utilization is not None and state.utilization_percent > job.max_utilization:
            return False
        if job.exclusive:
            # An explicit per-device policy may permit external programs.
            # Memory/reserve and utilization gates above still apply, and
            # exclusivity continues to prevent sharing among queue jobs.
            if state.index in self.settings.exclusive_shared_gpus:
                return reservation.active_jobs == 0
            return (
                reservation.active_jobs == 0
                and state.compute_process_count == 0
                and state.used_memory_mb <= self.settings.exclusive_max_used_mb
                and state.utilization_percent <= self.settings.exclusive_max_utilization
            )
        return True

    def decisions(self, jobs: list[Job], states: list[GPUState]) -> list[LaunchDecision]:
        alive_keys: set[tuple[int, int]] = set()
        decisions: list[LaunchDecision] = []
        reservations = self._reservations(states)
        for job in jobs:
            eligible: list[tuple[int, int, GPUState, GPUReservation, int]] = []
            for state in states:
                key = (job.id, state.index)
                alive_keys.add(key)
                reservation = reservations.get(state.index, GPUReservation())
                if self._allowed(job, state, reservation):
                    count = self.stability.get(key, 0) + 1
                    self.stability[key] = count
                    _, free_after, required = self._fit(job, state, reservation)
                    eligible.append(
                        (free_after, state.utilization_percent, state, reservation, required)
                    )
                else:
                    self.stability[key] = 0
            eligible = [
                row for row in eligible
                if self.stability[(job.id, row[2].index)] >= job.stable_samples
            ]
            if len(eligible) < job.gpu_count:
                continue
            # Memory is a hard feasibility gate.  Among GPUs that can safely
            # fit the job, prefer the least-loaded device; use best-fit only
            # as a tie-breaker so we do not place new work on an already busy
            # card merely because it has slightly less free memory.
            selected = sorted(
                eligible, key=lambda row: (row[1], row[0], row[2].index)
            )[:job.gpu_count]
            indices = tuple(sorted(row[2].index for row in selected))
            decisions.append(
                LaunchDecision(
                    job_id=job.id,
                    gpu_index=indices[0],
                    gpu_indices=indices,
                    stable_count=min(self.stability[(job.id, index)] for index in indices),
                    free_memory_mb=min(row[2].free_memory_mb for row in selected),
                    required_memory_mb=max(row[4] for row in selected),
                    outstanding_reservation_mb=max(
                        row[3].outstanding_memory_mb for row in selected
                    ),
                    utilization_percent=max(row[2].utilization_percent for row in selected),
                )
            )
        self.stability = {key: value for key, value in self.stability.items() if key in alive_keys}
        return decisions

    def fail_impossible_auto_jobs(
        self,
        jobs: list[Job],
        states: list[GPUState],
    ) -> list[Job]:
        """Fail calibrated auto jobs that cannot fit on enough eligible cards."""
        runnable: list[Job] = []
        for job in jobs:
            if not job.is_auto_memory or job.oom_count == 0:
                runnable.append(job)
                continue
            eligible_states = [
                state
                for state in states
                if job.gpu_allowlist is None or state.index in job.gpu_allowlist
            ]
            if len(eligible_states) < job.gpu_count:
                # The allowlisted device may be temporarily undiscoverable;
                # lack of a sample is not evidence of an impossible request.
                runnable.append(job)
                continue
            maximum_mb = sorted(
                (state.total_memory_mb for state in eligible_states), reverse=True
            )[job.gpu_count - 1]
            if job.required_free_memory_mb > maximum_mb:
                self.db.fail_impossible_memory(
                    job.id,
                    required_mb=job.required_free_memory_mb,
                    maximum_mb=maximum_mb,
                )
                continue
            runnable.append(job)
        return runnable

    def refresh_peaks(self, states: list[GPUState]) -> None:
        active_probe_ids: set[int] = set()
        now = self.clock()
        for job in self.db.active():
            if job.worker_pid is None:
                continue
            observed = max(self._observed_memory(job, states).values(), default=None)
            if observed is None and job.gpu_count == 1:
                observed = self.backend.process_memory_mb(job.worker_pid)
            if observed is not None and observed > job.peak_memory_mb:
                self.db.update_peak(job.id, observed)
            if not job.is_uncalibrated_auto_probe or job.exclusive or job.gpu_count > 1:
                continue
            active_probe_ids.add(job.id)
            if observed is None or observed <= 0:
                self.auto_probe_observations.pop(job.id, None)
                continue
            observation = self.auto_probe_observations.get(job.id)
            if observation is None:
                observation = AutoProbeObservation(first_seen=now, samples=[])
                self.auto_probe_observations[job.id] = observation
            observation.samples.append(observed)
            observation.samples = observation.samples[-self.settings.auto_probe_stable_samples :]
        self.auto_probe_observations = {
            job_id: observation
            for job_id, observation in self.auto_probe_observations.items()
            if job_id in active_probe_ids
        }

    def tick(self, *, dry_run: bool = False) -> LaunchDecision | None:
        self.reap_runners()
        self.reconcile_active()
        states = self.backend.sample()
        self.refresh_peaks(states)
        jobs = self.db.runnable()
        jobs = self.fail_impossible_auto_jobs(jobs, states)
        decisions = self.decisions(jobs, states)
        if not decisions:
            if not dry_run:
                for job in jobs:
                    self.db.set_waiting_memory(job.id)
            return None
        decision = decisions[0]
        if dry_run:
            return decision

        # The unmanaged multi-user environment has an unavoidable race.  Take a
        # second authoritative snapshot immediately before claiming the job.
        second = {state.index: state for state in self.backend.sample()}
        job = self.db.get(decision.job_id)
        reservations = self._reservations(list(second.values()))
        for gpu_index in decision.gpu_indices:
            state = second.get(gpu_index)
            reservation = reservations.get(gpu_index, GPUReservation())
            if state is None or not self._allowed(job, state, reservation):
                self.stability[(job.id, gpu_index)] = 0
                self.db.set_waiting_memory(job.id)
                return None
        selected_states = [second[index] for index in decision.gpu_indices]
        if not self.db.claim(
            job.id,
            decision.gpu_index,
            gpu_indices=decision.gpu_indices,
            launch_free_memory_mb=min(state.free_memory_mb for state in selected_states),
            launch_total_memory_mb=min(state.total_memory_mb for state in selected_states),
            launch_free_memory_by_gpu={
                state.index: state.free_memory_mb for state in selected_states
            },
        ):
            return None
        claimed = self.db.get(job.id)
        try:
            self.launcher(claimed, decision.gpu_index)
        except Exception as error:
            self.db.finish(
                job.id,
                status=JobStatus.FAILED.value,
                exit_code=None,
                error=f"scheduler launch failed: {type(error).__name__}: {error}",
                failure_kind="scheduler",
            )
            raise
        self.stability = {key: 0 for key in self.stability}
        return decision

    def _launch(self, job: Job, gpu_index: int) -> None:
        environment = os.environ.copy()
        environment["GPUQ_STATE_DIR"] = str(self.settings.state_dir)
        if self.settings.config_path is not None:
            environment["GPUQ_CONFIG"] = str(self.settings.config_path)
        process = subprocess.Popen(
            runner_command(str(self.settings.state_dir), job.id),
            env=environment,
            start_new_session=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        self.runners[job.id] = process
        self.db.mark_running(job.id, pid=process.pid, pgid=process.pid)

    def reap_runners(self) -> None:
        self.runners = {
            job_id: process
            for job_id, process in self.runners.items()
            if process.poll() is None
        }


def cancel_job(db: QueueDB, job_id: int, *, force_after: float = 10.0) -> Job:
    job = db.request_cancel(job_id)
    if job.status == JobStatus.CANCELLED.value or job.pgid is None:
        return db.get(job_id)

    # The actual command is started in its own session, so worker_pid is also
    # its process-group ID.  Preserve the runner whenever possible: it is the
    # component that records the exit code and final cancelled state.
    target_pgid = job.worker_pid or job.pgid
    try:
        os.killpg(target_pgid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    deadline = time.monotonic() + max(0.0, force_after)
    while time.monotonic() < deadline:
        current = db.get(job_id)
        if current.status in TERMINAL_STATUSES:
            return current
        time.sleep(0.1)

    # The runner may have published worker_pid just after the first snapshot.
    current = db.get(job_id)
    final_target = current.worker_pid or target_pgid
    try:
        os.killpg(final_target, signal.SIGKILL)
    except ProcessLookupError:
        pass
    db.finish(
        job_id,
        status=JobStatus.CANCELLED.value,
        exit_code=None,
        error="cancelled by user",
        failure_kind="cancelled",
    )
    return db.get(job_id)


def run_daemon(settings: Settings, *, once: bool = False, dry_run: bool = False) -> None:
    ensure_state_dirs(settings)
    lock_handle = settings.lock_path.open("a+")
    try:
        fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError as error:
        raise RuntimeError("another gpuqd instance is already running") from error
    lock_handle.seek(0)
    lock_handle.truncate()
    lock_handle.write(str(os.getpid()))
    lock_handle.flush()
    scheduler = Scheduler(settings=settings)
    stop = False

    def request_stop(_signum: int, _frame: object) -> None:
        nonlocal stop
        stop = True

    signal.signal(signal.SIGTERM, request_stop)
    signal.signal(signal.SIGINT, request_stop)
    while not stop:
        try:
            decision = scheduler.tick(dry_run=dry_run)
            if decision is not None:
                action = "would launch" if dry_run else "launched"
                print(
                    f"gpuqd {action} job={decision.job_id} "
                    f"gpu={','.join(map(str, decision.gpu_indices))} "
                    f"free={decision.free_memory_mb}MB "
                    f"reserved={decision.outstanding_reservation_mb}MB "
                    f"required={decision.required_memory_mb}MB "
                    f"stable={decision.stable_count}",
                    flush=True,
                )
        except (OSError, subprocess.SubprocessError) as error:
            print(f"gpuqd monitor error: {error}", file=sys.stderr, flush=True)
        if once:
            break
        deadline = time.monotonic() + settings.poll_seconds
        while not stop and time.monotonic() < deadline:
            # The second monotonic() call can cross the deadline after the
            # loop condition was evaluated.  Clamp the remaining duration so
            # a scheduling delay never turns into ``sleep(-epsilon)`` and
            # crashes the persistent daemon (or orphan its active workers).
            time.sleep(max(0.0, min(0.25, deadline - time.monotonic())))
