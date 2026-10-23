<p align="center">
  <img src="assets/gpuq-logo.svg" width="180" alt="gpuq logo">
</p>

<h1 align="center">gpuq</h1>

<p align="center"><strong>Queue jobs. Catch free VRAM.</strong></p>
<p align="center">Stop watching GPUs. Let free memory call the next job.</p>

<p align="center">
  English · <a href="README.zh-CN.md">简体中文</a>
</p>

<p align="center">
  <a href="https://github.com/lzy248/gpuq/actions/workflows/ci.yml"><img alt="CI" src="https://github.com/lzy248/gpuq/actions/workflows/ci.yml/badge.svg"></a>
  <img alt="Python 3.10+" src="https://img.shields.io/badge/python-3.10%2B-3776AB">
  <a href="LICENSE"><img alt="MIT License" src="https://img.shields.io/badge/license-MIT-22c55e"></a>
</p>

`gpuq` is a lightweight, per-user opportunistic GPU job queue for shared servers
without a managed scheduler. It watches free VRAM, waits for a stable window, and
starts your next job without killing, pausing, or reserving memory from anyone else.

It is deliberately **not a miniature Slurm**. Slurm governs a cluster. `gpuq` helps
one user stop babysitting an unmanaged GPU box.

## Who is it for?

`gpuq` fits when:

- a lab or team shares a workstation/server by launching CUDA commands directly;
- you have a backlog of training or evaluation jobs but no Slurm administrator;
- GPUs are often partially occupied and you want to use leftover VRAM safely;
- you want a persistent personal queue, logs, dependencies, retries, and notifications;
- installing a root-level service or taking control of other users' processes is not an option.

Use Slurm, Kubernetes, or another managed scheduler instead when you need multi-node
jobs, cross-user quotas, fair-share scheduling, accounting, hard reservations,
preemption, or administrative resource isolation.

## gpuq vs. Slurm

| | gpuq | Slurm |
|---|---|---|
| Scope | One user's opportunistic queue | Cluster-wide resource manager |
| Deployment | User-space Python + SQLite; no root | Administrator-managed control plane |
| Authority | Observes free VRAM and launches when it fits | Allocates and enforces resources |
| Other users | Never kills or pauses their processes | Coordinates all enrolled jobs |
| Partial GPUs | Backfills leftover VRAM | Usually allocates configured GPU resources |
| Fairness / quotas / accounting | Intentionally absent | Core functionality |
| Multi-node / distributed allocation | Not supported | Supported |
| Best fit | Unmanaged shared GPU servers | Managed clusters |

`gpuq` cannot prevent a user outside the queue from allocating memory after its last
check. It reduces that race with stable samples, a launch-time recheck, safety reserve,
and OOM learning; it cannot provide Slurm's hard guarantees.

## Highlights

- **Auto VRAM mode by default.** Submit without estimating memory; recognizable
  CUDA/HIP OOMs teach the queue a higher minimum and retry without a count limit.
- **Correct failure boundary.** Non-OOM code/data/launch errors fail immediately.
  A learned request larger than every eligible single GPU terminates as
  `oom_exceeds_device` instead of waiting forever.
- **Opportunistic backfill.** Memory is the hard gate; among eligible GPUs, lower
  utilization wins, with best-fit as a tie-breaker.
- **Race reduction.** Stable sampling, an authoritative second snapshot, and
  outstanding reservations for slow-starting jobs reduce accidental overcommit.
- **Persistent personal queue.** SQLite-backed jobs survive terminals and daemon
  restarts; dependencies, priorities, cancellation, logs, and notifications are built in.
- **Interactive queue view.** `gpuq list` shows 20 jobs per page in a TTY; use
  left/right arrows to move and `q` or `Esc` to exit. An alternate screen keeps
  scrollback clean; pipes and JSON stay noninteractive.
- **No preemption.** `gpuq` only controls process groups that it started.

## Requirements

- Linux
- NVIDIA driver and a working `nvidia-smi`
- A CUDA workload using visible device IDs (`cuda:0` for one GPU, local ranks for multiple GPUs)

The standalone binary does not require Python. A source installation requires Python
3.10+. Python 3.11+ uses the standard library only; Python 3.10 installs the small
`tomli` compatibility dependency for configuration parsing.

## Install

### Standalone binary (recommended, Linux x86_64)

```bash
VERSION=0.2.0
cd /tmp
curl -fLO "https://github.com/lzy248/gpuq/releases/download/v${VERSION}/gpuq-v${VERSION}-linux-x86_64.tar.gz"
curl -fLO "https://github.com/lzy248/gpuq/releases/download/v${VERSION}/gpuq-v${VERSION}-linux-x86_64.tar.gz.sha256"
sha256sum -c "gpuq-v${VERSION}-linux-x86_64.tar.gz.sha256"
tar -xzf "gpuq-v${VERSION}-linux-x86_64.tar.gz"
install -Dm755 "gpuq-v${VERSION}-linux-x86_64/gpuq" "$HOME/.local/bin/gpuq"
gpuq --version
```

Ensure `~/.local/bin` is on `PATH`, then initialize:

```bash
gpuq init
gpuq doctor
```

The release archive also includes the example config, systemd unit, license, and both
README languages.

### Source install (optional; any supported architecture)

```bash
git clone https://github.com/lzy248/gpuq.git
cd gpuq
export PATH="$(python -m site --user-base)/bin:$PATH"
python -m pip install --user -e .
gpuq init
gpuq doctor
```

The `PATH` setting lets your shell find the user-installed `gpuq` and `gpuqd`
commands. Setting it before installation also avoids pip's warning that the scripts
directory is not on `PATH`. It applies only to the current shell and its child
processes. For future terminals, add the same `export` line to your shell's startup
file (for example, `~/.bashrc` for interactive Bash shells), then open a new terminal
or reload that file.

## Quick start

Start one persistent scheduler for the state directory:

```bash
gpuq daemon
```

Submit a job. Auto memory mode is the default, so `--memory` is optional:

```bash
gpuq submit \
  --name train-model \
  --gpu 0,1 \
  --reserve 1500 \
  --stable-samples 3 \
  --cwd "$PWD" \
  --env PYTHONPATH=. \
  -- python train.py --device cuda:0
```

If you know the peak, opt into a fixed estimate:

```bash
gpuq submit \
  --memory 12000 \
  --reserve 1500 \
  --retry-on-oom \
  --max-retries 2 \
  -- python train.py --device cuda:0
```

Inspect and control jobs:

```bash
gpuq list
gpuq show 1
gpuq logs -f 1
gpuq cancel 1
gpuq retry 1 --memory auto
```

### One job on multiple GPUs

Use `--gpus N` (alias `--gpu-count N`) to request N GPUs on this machine. The
default remains one GPU; `--gpu 0,1` alone is an allowlist, not a two-GPU request.

```bash
gpuq submit --gpus 2 --gpu 0,1,2,3 --memory 12000 --reserve 1500 \
  -- torchrun --standalone --nproc_per_node=2 train.py
```

This waits for two eligible GPUs, each with at least 13500 MB free after queue
reservations. All selected cards must pass stable sampling and the final recheck
before the task starts. `--memory`, `--reserve`, and OOM learning are per GPU.
The task must support multi-GPU execution itself; gpuq launches the command once.
Use a launcher such as `torchrun` that waits for all its workers to finish.

### Physical GPU IDs vs. visible device IDs

If the scheduler chooses physical GPU 1, it starts the child with:

```text
CUDA_VISIBLE_DEVICES=1
```

The child therefore sees that card as `cuda:0`. Pass `cuda:0` to your framework,
not the original physical index.

For a two-GPU assignment such as physical GPUs 1 and 3, the child receives
`CUDA_VISIBLE_DEVICES=1,3`, ordered by physical ID. They appear as `cuda:0` and
`cuda:1`; distributed ranks should use their local rank. `gpuq list` and
`gpuq show --json JOB_ID` report all assigned physical IDs.

## How auto memory works

```text
queued
  │ stable free VRAM >= reserve + 1 MB
  ▼
probe run ── success ───────────────► completed
  │
  ├─ non-OOM / launch error ───────► failed immediately
  │
  └─ recognized CUDA/HIP OOM
          │ learn a higher minimum
          ├─ minimum + reserve > largest eligible GPU ─► failed/oom_exceeds_device
          └─ otherwise ─► retry_wait ─► wait for stable free VRAM ─► run again
```

A long-running non-exclusive first probe does not need to hold its whole
launch-time free region until exit. After five minutes of observed worker VRAM
and 12 consecutive samples within a 128 MB band, gpuq shrinks that reservation
to the observed peak while retaining at least 1024 MB of fluctuation margin. A
later increase raises the peak floor immediately. Active `--exclusive` jobs
never release their card to backfill jobs.

After an OOM, the learned job-memory floor is the maximum of:

```text
ceil(previous estimate × 1.25)
observed process peak + failed allocation + 256 MB
launch free memory - reserve + 256 MB
```

Only recognized GPU OOMs take this unbounded retry path. This distinction prevents a
broken command from looping forever. See [Scheduling semantics](docs/scheduling.en.md)
for exact behavior and limitations.

## Common commands

| Command | Purpose |
|---|---|
| `gpuq init` | Initialize state and an example config |
| `gpuq submit -- COMMAND...` | Queue a command; auto memory is default |
| `gpuq list` / `gpuq ls` | Interactive or script-friendly queue view |
| `gpuq show JOB_ID` | Show full job state and learned memory fields |
| `gpuq logs -f JOB_ID` | Follow merged stdout/stderr |
| `gpuq cancel JOB_ID` | Stop only this job's process group |
| `gpuq retry JOB_ID` | Requeue a terminal job |
| `gpuq priority JOB_ID N` | Change scheduling priority |
| `gpuq gpus` | Show the scheduler's GPU snapshot |
| `gpuq daemon` | Run the persistent scheduling loop |
| `gpuq doctor` | Check state, database, and GPU discovery |

See the complete [CLI reference](docs/cli_reference.en.md).

## Run as a user service

```bash
mkdir -p ~/.config/systemd/user
cp systemd/gpuqd.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now gpuqd.service
systemctl --user status gpuqd.service
```

Adjust `ExecStart` if `gpuq` is installed outside `~/.local/bin`. The supplied unit
uses `KillMode=process`, so restarting only the daemon does not kill independently
managed runners and training workers.

## Safety model and limitations

`gpuq` never:

- kills, pauses, or preempts unrelated GPU processes;
- allocates dummy tensors to reserve VRAM;
- claims fairness or isolation across users;
- guarantees that an external process cannot win the launch race.

gpuq supports single-node multi-GPU jobs; multi-node scheduling is not supported.
Process-level peak sampling can underestimate detached workers or very
short-lived allocations. Read [Scheduling semantics](docs/scheduling.en.md) before
using it for expensive unattended runs.

## Development

```bash
python -m pip install -e '.[dev]'
ruff check src tests
pytest
```

The test suite uses fake GPU backends and does not require a GPU. Contributions are
welcome; see [CONTRIBUTING.md](CONTRIBUTING.md). Security issues should follow
[SECURITY.md](SECURITY.md).

## Documentation

- [CLI reference](docs/cli_reference.en.md) · [中文](docs/cli_reference.md)
- [Scheduling semantics and safety](docs/scheduling.en.md) · [中文](docs/scheduling.md)
- [Changelog](CHANGELOG.md)

## License

MIT © Liu Zeyang and gpuq contributors. See [LICENSE](LICENSE).
