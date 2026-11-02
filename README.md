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

`gpuq` is a lightweight GPU task queue for training, evaluation, and batch experiments.
Submit your commands once; gpuq watches available GPU memory and starts jobs when resources are ready.
Use it on a workstation, through SSH on a remote server, or across a shared pool of machines.

## Who is it for?

- Researchers and students running experiments on shared lab GPUs.
- Developers queuing training, evaluation, or parameter sweeps without watching GPU availability.
- Users with several projects who want configurable concurrency and priorities.
- Users working across local and remote GPUs who want consistent submission, logs, and task management.

## Features

- **Automatic scheduling.** Choose GPUs by available memory and utilization, with stable sampling
  and a configurable memory reserve before launch.
- **Single- and multi-GPU jobs.** Run a command on one GPU or allocate several GPUs on the same host
  for workloads such as `torchrun`.
- **Memory learning.** Start with automatic memory estimation and learn from GPU OOMs, or provide
  a fixed estimate and retry limit.
- **Project concurrency.** Rotate launch opportunities across projects, set project and total task caps,
  and prioritize urgent jobs.
- **Persistent task management.** Keep task state, dependencies, logs, and notifications across terminal
  and scheduler restarts. Inspect the queue interactively or use JSON output in scripts.
- **Remote workspaces.** Submit and manage jobs over SSH, upload only changed content, and reuse
  Python environments and model caches on each node.
- **Shared GPU pool.** Optionally schedule across local and remote machines, with master election,
  standby takeover, preferred-master failback, and configurable retries for disconnected jobs.

## Requirements

- Linux
- NVIDIA driver and a working `nvidia-smi`
- A CUDA workload using visible device IDs (`cuda:0` for one GPU, local ranks for multiple GPUs)

The standalone binary does not require Python. A source installation requires Python
3.10+. Python 3.11+ uses the standard library only; Python 3.10 installs the small
`tomli` compatibility dependency for configuration parsing.

## Install

Current version: **0.3.0**. Install the standalone binary or build from source.

### Standalone binary (recommended, Linux x86_64)

```bash
VERSION=0.3.0
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

## Automatic memory estimation

Auto memory mode starts a job when stable free memory covers the configured reserve and an initial
probe. If the job encounters a recognized GPU OOM, gpuq raises its memory estimate and queues another
attempt. Other command errors are recorded as failures for inspection. Use `--memory N` when you know
the expected peak; memory and reserve values apply to each GPU.

See [scheduling details](docs/scheduling.en.md) for memory learning, resource selection, and retry rules.

## SSH remotes and project concurrency

Use `gpuq --remote user@host ...` to submit, inspect and cancel remote tasks with incremental isolated code
snapshots and persistent remote model caches. See [remote workspaces and environments](docs/remote_usage.en.md).
`daemon --background` explicitly starts a detached daemon.

`submit --project` overrides automatic project identity; `--project-limit N` persists the ordinary project cap (default 1).
`daemon --max-running-jobs N` caps all active jobs (0 means unlimited); `--scheduling fair|fifo` selects fair projects or legacy FIFO.
With total cap 2, A alone may run 2 tasks; competing A/B get 1+1 without pausing running tasks. Inspect with `projects --json`.

### Shared local and remote GPU pool

With [cluster configuration](docs/cluster_usage.en.md) in place, use `--cluster` to target the shared queue:

```bash
gpuq --cluster submit -- python train.py
gpuq --cluster list
gpuq --cluster logs -f 1
```

The pool uses three etcd voting members for coordination; the third can be a small witness host without
a GPU. Each job runs on one machine. Node-loss retries are configurable and may overlap with an old
execution during a network partition; see the guide for retry settings and checkpoint handling.

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
| `gpuq projects --json` | Inspect project concurrency and task counts |
| `gpuq --remote HOST list` | Inspect a remote queue over SSH |
| `gpuq cluster status` | Inspect shared-pool leadership and nodes |
| `gpuq --cluster list` | Inspect the shared queue |

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

## Scheduling on shared GPUs

gpuq manages the jobs you submit and leaves other users' processes running. It checks available resources
before launch; `--reserve` leaves headroom for memory fluctuations on shared GPUs. Resource checks and
memory sampling are described in the [scheduling guide](docs/scheduling.en.md).

## Development

Start with the [engineering guide](docs/README.md) for architecture and module contracts.
Documentation conventions are in [AGENTS.md](AGENTS.md).

```bash
python -m pip install -e '.[dev]'
python scripts/check_docs.py
ruff check src tests scripts/check_docs.py
pytest
```

The test suite uses fake GPU backends and does not require a GPU. Contributions are
welcome; see [CONTRIBUTING.md](CONTRIBUTING.md). Security issues should follow
[SECURITY.md](SECURITY.md).

## Documentation

- [CLI reference](docs/cli_reference.en.md) · [中文](docs/cli_reference.md)
- [Scheduling guide](docs/scheduling.en.md) · [中文](docs/scheduling.md)
- [Remote workspaces and environments](docs/remote_usage.en.md) · [中文](docs/remote_usage.md)
- [Shared pool setup and failover](docs/cluster_usage.en.md) · [中文](docs/cluster_usage.md)
- [Changelog](CHANGELOG.md)

## License

MIT © Liu Zeyang and gpuq contributors. See [LICENSE](LICENSE).
