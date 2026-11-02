# gpuq CLI reference

[简体中文](cli_reference.md) · English

This document covers the current source CLI, including Unreleased features; existing `0.2.0` releases may not include every capability.

## Global syntax

Global options must precede the subcommand:

```bash
gpuq [--remote HOST] [--state-dir PATH] [--config PATH] [--version] SUBCOMMAND ...
```

| Option | Meaning | Default |
|---|---|---|
| `--state-dir PATH` | SQLite, lock, and default log directory | `~/.local/state/gpuq` or `GPUQ_STATE_DIR` |
| `--config PATH` | TOML configuration | `~/.config/gpuq/config.toml` or `GPUQ_CONFIG` |
| `--version` | Print the installed version and exit | — |
| `--remote HOST` | SSH host/IP/user@host or local profile; operations target that remote queue | See remote guide |
| `--ssh-port N` | Override SSH port | Profile / OpenSSH defaults |
| `--ssh-identity PATH` | Local SSH identity file | Profile / OpenSSH defaults |
| `--remote-command TEXT` | Remote gpuq executable argv, parsed with shell quoting | See remote guide |
| `--remote-config PATH` | Remote TOML configuration path | See remote guide |
| `--remote-workdir PATH` | Remote persistent workspace/cache root | See remote guide |

In remote mode, `--config` reads local connection profiles, `--remote-config` selects remote configuration,
and `--state-dir` refers to remote state. The default workspace is `~/.local/share/gpuq/workspaces`.
SSH options require `--remote`. See [remote use](remote_usage.en.md) for profile fields, precedence and executable defaults.

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
| `--memory-mb N\|auto` / `--memory N\|auto` | No | Fixed job peak in MB or automatic learning; default `auto` |
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
| `--project ID` | Detected root | Explicit project identity; remote snapshots default to stable source identity |
| `--project-limit N` | New projects: 1 | Persist ordinary-job concurrency cap; omission retains policy, competing projects also obey fair shares |
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

### Remote submission and sync options

Require `--remote`. Default: incremental upload and isolated code snapshots; model weights are excluded and caches remain remote.

| Option | Meaning |
|---|---|
| `--no-sync` | Use existing remote checkout; requires --remote-cwd or profile.cwd |
| `--remote-cwd PATH` | Existing remote working directory; requires --no-sync |
| `--sync-exclude GLOB` | Additional exclusion glob; repeatable |
| `--sync-include PATH` | Explicit project-relative file/directory, e.g. checkpoint; overrides ignore rules; repeatable |
| `--sync-preview` | List selected local files without connecting |

`--no-sync` cannot be combined with include/exclude. See [remote use](remote_usage.en.md) for lockfiles, caches, exclusions and resume examples.

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

Text timestamps (created_at/updated_at/started_at/finished_at) use the CLI host’s local timezone with an explicit offset;
`--remote` uses the remote host’s timezone. `show --json` and `list --json` also output local timestamps with explicit offsets; database writes still use UTC.
Naive or invalid legacy values are displayed unchanged. `logs` does not rewrite log timestamps.

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
| `--memory-mb N\|auto` / `--memory N\|auto` | Replace the estimate or reset to a fresh auto probe |
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

| Option | Meaning |
|---|---|
| `--poll-seconds N` | Override sampling interval |
| `--once` | Run one scheduling tick |
| `--dry-run` | Sample and decide without launching; not a read-only operation |
| `--max-running-jobs N` | Total active-job cap; 0 means unlimited; applies to every priority |
| `--scheduling fair` / `--scheduling fifo` | Default fair project admission; fifo uses priority/ID order without project caps |
| `--background` | Detached startup; existing daemon retains settings; incompatible with once/dry-run |

Only one daemon may lock a state directory. `--once` creates a new scheduler process,
so three separate `--once` calls do not accumulate a `stable_samples=3` window.

## `gpuq projects`

Inspect project identity, concurrency caps, active/pending counts and last admission turn. JSON fields:
`project_id`, `concurrency_limit`, `active`, `pending`, `last_served`.

```bash
gpuq projects --json
gpuq --remote train projects --json
```

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
scheduling_mode = "fair"
max_running_jobs = 0
default_project_limit = 1
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


| Setting | Default / meaning |
|---|---|
| `scheduling_mode` | fair; fifo restores priority/ID ordering |
| `max_running_jobs` | 0, unlimited total concurrency; counts jobs, not GPUs |
| `default_project_limit` | 1; initializes ordinary-job caps for new projects only |

`notify_command` is an argument array, not a shell string. It may interpolate
`{title}`, `{message}`, and `{job_id}`. Notification failure never changes job state.

See [Scheduling semantics and safety](scheduling.en.md) for exact resource decisions.

## SSH remotes and project concurrency

Use `gpuq --remote user@host ...` to submit, inspect and cancel remote tasks with incremental isolated code
snapshots and persistent remote model caches. See [remote workspaces and environments](remote_usage.en.md).
`daemon --background` explicitly starts a detached daemon.

`submit --project` overrides automatic project identity; `--project-limit N` persists the ordinary project cap (default 1).
`daemon --max-running-jobs N` caps all active jobs (0 means unlimited); `--scheduling fair|fifo` selects fair projects or legacy FIFO.
With total cap 2, A alone may run 2 tasks; competing A/B get 1+1 without pausing running tasks. Inspect with `projects --json`.
