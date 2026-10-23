# Scheduling semantics and safety

[简体中文](scheduling.md) · English

## 1. Fixed-memory eligibility

For job `j` and GPU `g`:

```text
required(j) = estimated_peak_memory(j) + reserve(j)
free_memory(g) >= required(j)
```

For an active queue job whose allocation has not fully appeared in `nvidia-smi`, gpuq
also subtracts the unmaterialized part:

```text
outstanding(j) = max(0, estimated_peak(j) - observed_process_memory(j))
schedulable_free(g) = physical_free(g) - sum(outstanding(j))
```

The largest reserve among active and candidate jobs on a card is retained. This avoids
placing multiple slow-starting jobs into the same apparently free memory region.

An optional `max_utilization` is an additional hard gate. A condition must remain true
for `stable_samples` consecutive samples, followed by a second authoritative snapshot
immediately before claim. With a 10-second poll and three samples, the usual stable
window is about 20–30 seconds.

## 2. Auto-memory probe and learning

Auto mode is the submit default. Its initial internal job estimate is 1 MB, so it may
probe once `free >= reserve + 1 MB`. At claim time gpuq stores launch free/total memory.
It initially reserves the launch-time schedulable region against other jobs in the
same queue. For a long-running non-exclusive single-GPU probe, the reservation may shrink to the
observed peak after at least five minutes and 12 consecutive process-memory samples
within a 128 MB band. At least 1024 MB remains unavailable as fluctuation margin, and a
later peak increase raises the reservation floor immediately. The stability window is
restarted with the daemon. Active exclusive jobs always block same-GPU backfill. This
does not reserve CUDA memory and cannot exclude jobs launched outside gpuq.

On a recognizable CUDA/HIP OOM, let `requested` be the largest failed allocation parsed
from the log, or zero when unavailable. The new job-memory floor is:

```text
max(
  ceil(old_estimate * 1.25),
  observed_process_peak + requested + 256 MB,
  launch_free_memory - reserve + 256 MB
)
```

The next attempt waits for:

```text
free >= learned_memory + reserve
```

Auto OOM retries have no count limit. The limit is semantic, not numeric:

- only recognizable GPU OOMs requeue;
- non-OOM command/data/assertion errors fail immediately;
- launch and runner errors fail immediately;
- if learned memory plus reserve exceeds the Nth-largest eligible total memory
  for an N-GPU request, the job terminates as `failed/oom_exceeds_device`.
  If fewer than N GPUs are discovered, it keeps waiting.

If a framework hides or rewrites its OOM without a recognized CUDA/HIP marker, gpuq
conservatively treats the exit as a normal command failure rather than risking an
infinite loop.

## 3. Fixed-mode bounded OOM retry

When a numeric `--memory` is supplied with `--retry-on-oom`, fixed mode uses:

```text
new_estimate = max(ceil(old_estimate * 1.25), observed_peak + 256 MB)
```

and respects `--max-retries`. This preserves the explicit user budget and existing
scripts. Auto mode ignores `max_retries` only for its recognized OOM path.

## 4. GPU choice and backfill

Jobs are considered by:

```text
priority DESC, job_id ASC
```

A job that does not fit does not block lower-priority jobs that do fit. This is
opportunistic backfill, not a fairness guarantee.

Among eligible GPUs, selection is:

```text
argmin(utilization, free_memory - required_memory, gpu_index)
```

Memory, reserve, allowlist, and an optional utilization ceiling are eligibility gates.
Among passing cards, the least-utilized card wins; best-fit breaks utilization ties.

### Single-node multi-GPU jobs

`--gpus N` selects N distinct, stable cards using the same ordering. Every card
must independently satisfy the per-GPU memory, reserve, utilization, and exclusive
constraints. All N cards are checked again in the second snapshot before one
atomic SQLite claim stores the complete assignment. If any card fails the check,
no card is claimed. Each assigned card carries its own outstanding reservation,
including after a daemon restart; an exclusive job excludes queue backfill on all N.

The command runs once with sorted physical IDs in `CUDA_VISIBLE_DEVICES` and must
manage its own distributed workers, for example with `torchrun --standalone`.
The command must wait for its workers and forward termination signals to workers
in separate sessions, as `torchrun` does. Cancellation targets the launcher's group.
OOM retries and orphan recovery release the full assignment and retain the GPU count.

For multi-GPU jobs, process memory is summed for the worker's process group and
descendants (including torchrun's separate sessions) on each assigned GPU.
The largest per-card value is the job's peak; memory is never
pooled across cards. The minimum launch free memory feeds OOM learning. A fresh
multi-GPU auto probe conservatively keeps each card's own launch-time region for
the whole uncalibrated attempt because ranks may initialize at different times.
Known per-card estimates via `--memory` allow normal backfill.

## 5. Exclusive mode

`--exclusive` additionally requires:

- no visible compute process;
- used memory no greater than `exclusive_max_used_mb`;
- utilization no greater than `exclusive_max_utilization`;
- no active gpuq job reservation on the card.

Small driver or display allocations do not necessarily represent a compute task, so
exclusive mode does not require exactly zero used bytes.

## 6. Process isolation and recovery

Each job has a management runner and a separate child process group:

- stdout/stderr are merged into the job log;
- the runner writes terminal state to SQLite;
- daemon scheduling does not block on a job;
- cancellation targets only the job's child process group;
- the queue and logs survive terminals and daemon restarts.

The supplied systemd unit uses `KillMode=process`. Restarting the scheduler does not
kill independent runners/workers. A new daemon preserves an active record when either
PID is alive; if both disappear before a terminal state is written, it atomically marks
the job `failed/orphaned` without overwriting a concurrent legitimate result.

## 7. Why this is not Slurm

On an unmanaged server, an external user may allocate memory between gpuq's final check
and CUDA context creation. Separate users may also run separate queues that do not share
SQLite or locks. gpuq reduces but cannot eliminate this race.

It intentionally does not:

- kill or preempt unrelated processes;
- allocate placeholder tensors;
- enforce cross-user quotas or fairness;
- provide accounting, hard reservations, multi-node allocation, or administrative isolation.

Use a managed scheduler when those properties matter.

## 8. Peak-memory limitations

The daemon samples process memory through `nvidia-smi`. The observed peak may be low when:

- a single-GPU task creates CUDA child processes, or multi-GPU ranks detach and are reparented;
- the peak is shorter than the poll interval;
- the driver does not expose process memory;
- a task uses devices outside its assigned visible set.

The OOM log's failed allocation and launch-time free-memory floor make auto mode more
robust to missed peaks, but `reserve` remains important. Multi-node scheduling
and GPU topology optimization are not supported.
