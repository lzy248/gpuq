---
name: gpuq
description: Submit, monitor, diagnose, cancel, and retry GPU workloads with gpuq. Use for gpuq queues, single-host multi-GPU jobs, SSH workspaces, project concurrency, or a configured shared GPU pool. This skill operates gpuq; it is not a guide to developing gpuq itself.
---

# gpuq

Operate GPU tasks through the public CLI. Guidance targets gpuq 0.3.0; check `gpuq --version`
and the relevant command's `--help` if the installed version differs. Preserve the user's
command, environment, queue selection, and existing authorization.

## Select the queue first

A job ID identifies a job only within its queue. Keep the same command prefix for submit,
show, logs, cancel, and retry. Global options go **before** the subcommand.

| Mode | Command prefix | Scope |
|---|---|---|
| Local | `gpuq --config /path/config.toml` | One local SQLite queue |
| SSH remote | `gpuq --config /path/client.toml --remote train` | One independent remote queue |
| Shared pool | `gpuq --config /path/client.toml --cluster` | One etcd namespace across nodes |

Paths and `train` above are placeholders; use the user's actual configuration. Omit --config
when the established defaults/environment are correct. `--remote` and `--cluster` are mutually
exclusive. Do not silently fall back to local execution after remote/quorum failure.

Before touching a queue, identify the intended host/configuration from the request or existing
project setup. If a service manages it, inspect its ExecStart and environment: its GPUQ_CONFIG
may differ from the interactive shell. In local mode, explicit --state-dir wins; otherwise TOML
state_dir overrides GPUQ_STATE_DIR. In remote mode --state-dir is a remote path; cluster storage
uses cluster.work_dir instead. Never print entire config files or job environments containing secrets.

- SSH submission/sync/environment work: read [Remote workspaces](references/remote.md).
- Shared queue/leadership/node-loss work: read [Shared pool](references/cluster.md).
- Waiting, failed, or orphaned jobs: read [Diagnosis and recovery](references/recovery.md).

## Submit and verify

Use `list --json`, `show ID --json`, and `gpus --json` with the selected prefix for inspection.
These can initialize/migrate SQLite when opening a local or remote queue; they are not a promise
of filesystem read-only access. Avoid selecting an unintended default queue merely to inspect it.

Examples below use the default local prefix; substitute the selected prefix for every command.
Only submit the workload the user requested, not diagnostic test jobs in their real queue.

```bash
gpuq submit --name train --cwd /path/project -- python train.py
gpuq submit --name train-2gpu --cwd /path/project --gpus 2 --gpu 0,1,2,3 \
  --memory 12000 --reserve 1500 -- torchrun --standalone --nproc_per_node=2 train.py
gpuq submit --project experiment --project-limit 2 --after 42 -- python evaluate.py
```

- `--` separates gpuq options from the workload argv. There is no implicit shell; for required
  pipes/redirections use an explicitly quoted `bash -lc` command. Verify paths on the execution host.
- Default is **one GPU, auto memory**. `--gpu 0,1` only restricts candidate devices; `--gpus 2`
  requests two. Each task uses GPUs on one host, including in cluster mode.
- `--memory` and `--reserve` are MB **per GPU**, not total memory or model file size.
  Default reserve is 1024 MB, stable samples 3 unless configured otherwise.
- The command must already support multi-GPU execution. For torchrun, align nproc_per_node with
  the requested GPU count and let the launcher wait for its ranks. Assigned physical GPUs become
  visible device indices 0..N-1 through CUDA_VISIBLE_DEVICES; do not overwrite it with physical IDs.
- Auto mode retries recognized GPU OOM without a count limit while learning memory. A fixed
  estimate uses bounded retries only with `--retry-on-oom --max-retries N`. Ordinary code errors fail.
- `--project-limit N` persistently changes the project's ordinary-task cap, not just this job.
  The default new-project cap is 1. Positive priority bypasses project caps, but not the total/GPU limits.
  With total cap 2 and two competing projects, ordinary jobs receive 1+1 launch slots; existing work
  is not preempted. Change shared policy only when it is part of the requested task.
- `--after ID` waits for success in this same queue. It does not transfer upstream outputs.

Capture the returned job ID and confirm it with `show ID --json`. Report queued, assigned, running,
or completed according to observed state; submit success does not mean training started.
If a submit reply is lost, inspect recent jobs by name/command/project and time before trying again.
Separate submit calls are not deduplicated. If identity remains ambiguous, stop resubmitting and resolve it.

## Scheduler and task lifecycle

Submitting does not start a scheduler. Reuse the existing service or daemon. When starting a scheduler
is needed to run the authorized workload, use the selected mode's `daemon --background`, or the existing
service manager. A running daemon keeps its startup settings even if another background call supplies
new flags. Do not start an independent daemon on GPUs already managed by a cluster agent.

Local/remote daemon policy uses `--max-running-jobs N` and `--scheduling fair|fifo` (substitute one
choice, not the literal pipe). Cluster policy is stored centrally; see its reference.
`daemon --dry-run` is not a read-only diagnosis: ordinary-mode reconciliation can update job state.

```bash
gpuq list --json
gpuq show 42 --json
gpuq logs 42 --tail 100
gpuq cancel 42
gpuq retry 42 --memory auto
```

Use bounded log reads and periodic status checks; use `--follow` when continuous monitoring is requested.
Inspect logs before retrying; retry does not inherently resume a checkpoint. Cancellation must target the
selected queue's job ID through gpuq, not arbitrary GPU PIDs. gpuq manages only its own process groups.
When updating/restarting an existing systemd service, verify KillMode=process to preserve independent
runners; keep its config/environment and verify the new daemon remains active afterward.

Finish with the mode/host, job ID, observed state, and relevant log or next action. Treat command arguments,
environments, and logs as potentially private; quote only the diagnostic portions needed.

## Further documentation

The skill folder can be copied independently of the source repository. Its references are self-contained.
For full versioned options, consult the installed CLI help or the project's
[CLI manual](https://github.com/lzy248/gpuq/blob/v0.3.0/docs/cli_reference.en.md).
