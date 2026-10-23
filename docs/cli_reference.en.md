# gpuq CLI reference

[简体中文](cli_reference.md) · English

This document covers the public CLI in `gpuq 0.2.0`.

## Global syntax

Global options must precede the subcommand:

```bash
gpuq [--state-dir PATH] [--config PATH] [--version] SUBCOMMAND ...
```

| Option | Meaning | Default |
|---|---|---|
| `--state-dir PATH` | SQLite, lock, and default log directory | `~/.local/state/gpuq` or `GPUQ_STATE_DIR` |
| `--config PATH` | TOML configuration | `~/.config/gpuq/config.toml` or `GPUQ_CONFIG` |
| `--version` | Print the installed version and exit | — |

## `gpuq init`

Initializes the state directory and SQLite schema. If the configuration file does
not exist, it creates an example without overwriting existing settings.

```bash
gpuq init
gpuq --state-dir /data/my-gpuq init
```

## `gpuq submit`

```bash
gpuq submit [OPTIONS] -- COMMAND [ARGUMENTS...]
```

The `--` separator keeps training arguments from being parsed as gpuq options.

### Resources

| Option | Required | Meaning |
|---|---:|---|
| `--memory-mb N|auto` / `--memory N|auto` | No | Fixed job peak in MB or automatic learning; default `auto` |
| `--reserve-mb N` / `--reserve N` | No | Additional launch-race and estimation margin; config default is 1024 MB |
| `--gpu LIST` | No | Eligible physical GPU IDs, e.g. `--gpu 1` or `--gpu 0,1`; repeatable |
| `--gpus N` / `--gpu-count N` | No | GPUs used simultaneously by this job on one host; positive integer, default 1 |
| `--max-utilization N` | No | Optional launch-time utilization ceiling, 0–100 |
| `--stable-samples N` | No | Consecutive eligible samples required before launch; default 3 |
| `--exclusive` | No | Require no compute process and near-idle memory/utilization |

Omitting `--memory` is equivalent to `--memory auto`. The first probe requires
`reserve + 1 MB`; only a recognizable CUDA/HIP OOM learns a new floor and requeues.
Supplying a number selects fixed mode. The fixed number is the expected workload peak,
not the model file size, and does not include reserve.

Memory and reserve are **per GPU**. `--gpu` restricts candidate physical IDs;
`--gpus` sets how many to select. A request larger than the number of distinct
allowlisted IDs is rejected. If discovery finds too few cards, the job waits
without reserving a partial set. For example:

```bash
gpuq submit --gpus 2 --gpu 0,1,2,3 --memory 12000 --reserve 1500 \
  -- torchrun --standalone --nproc_per_node=2 train.py
```

gpuq launches the command once with all assigned IDs in `CUDA_VISIBLE_DEVICES`,
sorted by physical ID; the application sees local devices `0..N-1`. The command
must implement multi-GPU execution and wait for its workers (as `torchrun` does).
Exclusivity, stable sampling, and the final resource check apply to every card.
`list` displays the whole assignment; JSON includes `gpu_count` and `assigned_gpus`.
The legacy JSON field `assigned_gpu` remains the first assigned GPU.

### Scheduling

| Option | Default | Meaning |
|---|---:|---|
| `--priority N` | `0` | Higher values are considered first |
| `--after JOB_ID` | None | Run only after the dependency completes successfully |
| `--max-retries N` | `0` | Extra OOM attempts in fixed mode; ignored by auto mode |
| `--retry-on-oom` | Off | Enable bounded OOM growth in fixed mode; always active in auto mode |

Auto mode retries recognized OOMs without a count limit while the learned requirement
fits the requested number of eligible GPUs. Non-OOM exits, assertions, data errors, launch failures,
and runner failures terminate immediately. If learned memory plus reserve exceeds
the Nth-largest eligible GPU's total memory for an N-GPU request, the job
terminates with `failure_kind=oom_exceeds_device`. Too few discovered cards alone
does not trigger this failure. A multi-GPU auto probe retains each card's own
launch-time reservation for the entire uncalibrated attempt; OOM requeues the whole job.

After upgrading, restart the daemon before submitting multi-GPU jobs so it uses
the new scheduler. Existing database rows migrate automatically as single-GPU jobs.

### Process, logs, and notification

| Option | Default | Meaning |
|---|---|---|
| `--name TEXT` | Executable name | Display name |
| `--cwd PATH` | Current directory | Child working directory |
| `--env KEY=VALUE` | None | Add or override an environment variable; repeatable |
| `--log PATH` | Auto path under `logs/` | Combined stdout/stderr |
| `--no-notify` | Off | Disable notifications for this job |

### Examples

Default auto memory, physical GPU 1:

```bash
gpuq submit \
  --name experiment-a \
  --gpu 1 \
  --reserve 1500 \
  --cwd /home/me/project \
  --env PYTHONPATH=. \
  -- python train.py --device cuda:0
```

Known fixed peak with bounded OOM adjustment:

```bash
gpuq submit \
  --memory 10000 \
  --reserve 1500 \
  --retry-on-oom \
  --max-retries 2 \
  -- python train.py --device cuda:0
```

Dependency chain:

```bash
gpuq submit --name train --memory 10000 -- python train.py
# submitted job 21
gpuq submit --name eval --after 21 --memory 6000 -- python eval.py
```

Pipes and redirection must be explicitly delegated to a shell:

```bash
gpuq submit --memory 4000 -- bash -lc 'python train.py 2>&1 | tee extra.log'
```

## `gpuq list` / `gpuq ls`

```bash
gpuq list
gpuq ls --status running --status waiting_memory
gpuq list --limit 1000 --json
```

| Option | Meaning |
|---|---|
| `--status STATUS` | Filter by status; repeatable |
| `--limit N` | Maximum number loaded, default 200 |
| `--json` | Machine-readable JSON |
| `--page-size N` | Interactive page size, default 20 |
| `--no-pager` | Disable the pager even in a TTY |

When stdin and stdout are TTYs and more than one page is present, left/right arrows
change pages; `q` or `Esc` exits. The alternate screen buffer restores the original
terminal exactly, so page changes do not pollute scrollback. JSON, pipes, redirection,
and `--no-pager` always produce a static noninteractive listing.

## `gpuq show JOB_ID`

Displays command, resource mode, learned memory, GPU, PIDs, attempts, peak memory,
timestamps, and failure details.

```bash
gpuq show 21
gpuq show 21 --json
```

## `gpuq logs JOB_ID`

```bash
gpuq logs 21
gpuq logs -n 500 21
gpuq logs 21 --tail 8
gpuq logs -f 21
gpuq log 21 --tail 0 -f
```

| Option | Meaning |
|---|---|
| `-n N` / `--lines N` / `--tail N` | Initial tail length, default 100; N must be non-negative, and 0 skips existing output |
| `-f` / `--follow` | Follow until the job reaches a terminal state |

`log` is an alias for `logs`. Use `--tail 0 -f` to display only newly appended output.
Reading existing output retains only the last N lines instead of loading the entire log into memory.

## `gpuq cancel JOB_ID`

Queued jobs are marked cancelled. Running jobs receive SIGTERM on the task process
group, followed by SIGKILL after the timeout if needed.

```bash
gpuq cancel 21
gpuq cancel 21 --force-after 30
```

Cancellation only targets process groups started for that gpuq job.

## `gpuq retry JOB_ID`

Only terminal jobs can be requeued.

```bash
gpuq retry 21
gpuq retry 21 --memory 8000 --reset-attempts
gpuq retry 21 --memory auto
```

| Option | Meaning |
|---|---|
| `--memory-mb N|auto` / `--memory N|auto` | Replace the estimate or reset to a fresh auto probe |
| `--reset-attempts` | Reset the displayed attempt counter |

## `gpuq priority JOB_ID VALUE`

```bash
gpuq priority 21 100
gpuq priority 22 -10
```

Higher values are considered first. A high-priority job that does not currently fit
does not block a lower-priority job that does fit (opportunistic backfill).

## `gpuq gpus`

Shows free/total memory, utilization, temperature, and visible compute PIDs.

```bash
gpuq gpus
gpuq gpus --json
```

## `gpuq daemon`

```bash
gpuq daemon
gpuq daemon --poll-seconds 5
gpuq daemon --once
gpuq daemon --once --dry-run
```

Only one daemon may lock a state directory. `--once` creates a new scheduler process,
so three separate `--once` calls do not accumulate a `stable_samples=3` window.

## `gpuq doctor`

Checks the state directory, SQLite database, and `nvidia-smi` discovery.

```bash
gpuq doctor
```

## Job states

| State | Meaning |
|---|---|
| `queued` | Newly submitted |
| `waiting_memory` | No GPU currently satisfies the gate |
| `starting` | Atomically claimed; runner is starting |
| `running` | Child command is active |
| `retry_wait` | OOM learned a new floor; waiting to retry |
| `completed` | Exit code 0 |
| `failed` | Launch, runner, command, or impossible-memory failure |
| `cancelled` | Cancelled by the user |
| `blocked_dependency` | Dependency did not complete successfully |

## Configuration

```toml
[gpuq]
state_dir = "~/.local/state/gpuq"
poll_seconds = 10
default_reserve_mb = 1024
default_stable_samples = 3
auto_probe_hold_seconds = 300
auto_probe_stable_samples = 12
auto_probe_stability_tolerance_mb = 128
auto_probe_release_margin_mb = 1024
exclusive_max_used_mb = 768
exclusive_max_utilization = 5
nvidia_smi = "nvidia-smi"
notify_command = ["notify-send", "{title}", "{message}"]
```

`notify_command` is an argument array, not a shell string. It may interpolate
`{title}`, `{message}`, and `{job_id}`. Notification failure never changes job state.

See [Scheduling semantics and safety](scheduling.en.md) for exact resource decisions.
