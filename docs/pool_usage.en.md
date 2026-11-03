# Simple GPU pool: configure once, submit normally

[简体中文](pool_usage.md) · [Engineering guide](README.md) · [CLI reference](cli_reference.en.md)

Available in current source; released v0.3.0 does not include this mode. Install matching source versions on both hosts.

One master stores the global queue, and local/remote GPUs execute its tasks. This uses gpuq, SQLite
(bundled with Python) and SSH: **no etcd, witness, or extra network service**. With default_queue configured,
ordinary `gpuq submit` chooses a node and GPUs automatically. Master downtime pauses new dispatch;
independent training continues, and the original master resumes after recovery. There is no master failover.

## Persistent configuration

Example master a and worker b use ~/.config/gpuq/config.toml. Replace hostnames, usernames and interpreter
paths with your actual values. The master needs SSH to workers for inputs/logs; workers need SSH back to
the master for queue operations. Configure noninteractive SSH and host verification in both directions.

Master a:

```toml
[gpuq]
default_queue = "cluster"
default_project_limit = 2

[cluster]
backend = "sqlite"
node_id = "a"
namespace = "training"
work_dir = "~/.local/share/gpuq/clusters/training"
gpu_allowlist = [0, 1]

[cluster.nodes]
a = "local"
b = "worker_b"

[remotes.worker_b]
host = "user@b.internal"
command = ["~/.local/bin/gpuq"]
config = "~/.config/gpuq/config.toml"
```

Worker b:

```toml
[gpuq]
default_queue = "cluster"

[cluster]
backend = "ssh"
coordinator = "master_a"
node_id = "b"
namespace = "training"
work_dir = "~/.local/share/gpuq/clusters/training"
gpu_allowlist = [0, 1]
python = "/opt/conda/envs/train/bin/python"

[remotes.master_a]
host = "user@a.internal"
command = ["~/.local/bin/gpuq"]
config = "~/.config/gpuq/config.toml"
```

| Setting | Default / behavior |
|---|---|
| gpuq.default_queue | local; cluster routes ordinary commands and daemon to the pool |
| cluster.backend | etcd when endpoints exist, otherwise sqlite; workers explicitly use ssh |
| cluster.coordinator | Required for ssh; profile pointing to the master's config |
| cluster.node_id | Hostname; stable unique node identity |
| cluster.namespace | default; identical throughout the pool |
| cluster.work_dir | ~/.local/share/gpuq/clusters/namespace; master also stores control.sqlite3 |
| cluster.gpu_allowlist | Unset: all node GPUs; array: contributed physical indices; empty: controller only |
| cluster.nodes | Current node → local by default; submitting host lists all input replica nodes/profiles |
| cluster.lease_seconds | 15 seconds, minimum 10; session expiry, not standby election |
| cluster.python / requirements | python3 / unset; node interpreter and optional hashed snapshot-relative lock |
| gpuq.default_project_limit | 1; captured by master init for new project caps |

This same TOML file is the persistent pool configuration. You may name it pool.toml and select it with
`gpuq --config /path/pool.toml ...` or GPUQ_CONFIG. No repeated node list is needed on submit.

To add worker c, add `c="worker_c"` to master's cluster.nodes and its remote profile, configure c with
backend=ssh/coordinator/node_id/contributed GPUs, then start c's agent. New submissions include c;
existing eligible_nodes/input replicas are not expanded automatically. Agents read config at startup;
restart the relevant agent after changing contributed GPUs. Submit reads nodes afresh each time.
Do not reuse a node identity with an empty journal or share one work_dir between pools.

## One-time startup

On master a, initialize once (existing policy is never overwritten):

```bash
gpuq cluster init --max-running-jobs 2 --max-failover-retries 0
gpuq daemon --background
```

On worker b:

```bash
gpuq daemon --background
```

default_queue=cluster selects pool agents; explicit `gpuq --cluster daemon` also works. Arrange the
transition if an independent daemon already manages those GPUs. Existing services keep their startup
configuration until restarted; their GPUQ_CONFIG must also select this file. A background process lock
does not prove readiness; inspect `gpuq cluster status` for master, workers and GPUs.
This example disables node-loss retries. The existing bounded retry policy can be configured at init;
see [retry semantics](cluster_usage.en.md#automatic-node-loss-retries-duplicates-possible). It handles
worker loss, not migration of the master's database.

## Submit normally

```bash
gpuq submit --name experiment-1 -- python train.py --seed 1
gpuq submit --name experiment-2 -- python train.py --seed 2
gpuq list --json
gpuq logs 1 --tail 100
gpuq cancel 1
```

The example allows two jobs per new project and two globally. After memory, stability, exclusivity and
GPU-count checks, placement prefers GPUs with **fewer active pool tasks**, then node task count/utilization.
Assignments awaiting process startup count too. Two eligible online cards normally receive one job each;
placement does not force work onto unavailable devices or migrate running tasks. Sharing remains allowed.
A multi-GPU task stays on one node. External processes affect measured resources, not pool task counts.

Project fairness and priorities still apply. If an existing project cap is 1, its second job waits; use
`submit --project-limit 2` once to persist a new cap. Usually omit --gpu to let the pool select devices;
if supplied it restricts physical indices on all candidate nodes. --gpus 2 requests two cards; the workload
uses visible indices 0..N-1.

Input snapshots, per-node HF/Torch caches, dependency environments and explicit checkpoint inclusion follow
the [remote guide](remote_usage.en.md). All configured replica nodes receive inputs before global submit;
any transfer failure aborts submission. The default minimum is two replicas; a one-node simple pool may
initialize with --snapshot-copies 1. Outputs are not automatically returned.

## Queue routing and recovery

Default routing covers submit/list/show/logs/cancel/retry/priority/projects/gpus/doctor/daemon.
--local explicitly selects the independent local queue; --remote HOST selects an independent remote queue;
--cluster explicitly selects the pool. These flags are mutually exclusive. --state-dir does not implicitly
select local mode. Ordinary init remains local; cluster init/status always targets the pool. Existing jobs
are not imported, and job IDs remain queue-specific.

Existing configs without default_queue stay local. Existing cluster endpoints keep etcd mode.
SSH workers never create a fallback queue when the master is unreachable. Accepted work can continue and
results persist in the worker journal; reconnecting reconciles results/cancellation and resumes allocation.
Inspect list/show after an ambiguous submission instead of resubmitting. Back up control.sqlite3 using
SQLite backup facilities plus node workspaces; lost outputs/models are not recovered automatically.

Tests use temporary SQLite, real subprocesses with fake SSH, and virtual GPUs. They cover cross-node and
same-node spreading, fixed-master behavior, transactions and disconnects, not deployment on your real hosts.
