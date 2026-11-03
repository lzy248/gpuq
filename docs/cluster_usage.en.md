# Shared GPU pool and master failover

This guide describes optional **etcd HA mode**. For a pool with only local/remote GPUs and no witness,
use the [SQLite + SSH simple pool](pool_usage.en.md). Existing endpoints keep etcd behavior.


[简体中文](cluster_usage.md) · [Engineering guide](README.md) · [CLI reference](cli_reference.en.md)

Explicit `--cluster` mode shares one per-user queue across local and remote GPU nodes. The initializing host
is the preferred master by default. Standbys take over after failure; the preferred host receives leadership
back after reconciliation and a stable grace period. Existing local and `--remote` queues are not migrated.
Each multi-GPU job still runs on one host; multi-host DDP is not supported.

## Three voting members

Deploy three **etcd v3 voting members** in independent failure domains, with the v3 JSON gateway enabled
(integration tests use 3.6.5). A and B run etcd plus gpuq agents; witness W runs only etcd and needs no GPU
or model cache. W is a persistent Raft member holding all control metadata, not a stateless ping service.
Three members tolerate one failure. All agents/clients need client-port access; members need peer connectivity.

Install compatible etcd versions on all three hosts. This HTTP example is for an isolated trusted network;
replace hostnames and use each host's own name and URL:

```bash
# A: a/a.internal; B: b/b.internal; W: w/w.internal.
ETCD_NAME=a
ETCD_URL=http://a.internal
etcd --name "$ETCD_NAME" --data-dir "$HOME/.local/share/gpuq-etcd" \
  --listen-client-urls http://0.0.0.0:2379 --advertise-client-urls "$ETCD_URL:2379" \
  --listen-peer-urls http://0.0.0.0:2380 --initial-advertise-peer-urls "$ETCD_URL:2380" \
  --initial-cluster 'a=http://a.internal:2380,b=http://b.internal:2380,w=http://w.internal:2380' \
  --initial-cluster-token gpuq-pool-1 --initial-cluster-state new
```

Keep data directories persistent and use a service manager. For untrusted networks, enable HTTPS and etcd
users/roles: configure `--cert-file/--key-file/--trusted-ca-file/--client-cert-auth` and corresponding
`--peer-*` flags, with URL-matching certificate SANs. Queue write access allows execution as the GPU user;
metadata includes commands and environment values. gpuq accepts CA/client credentials and existing tokens,
but does not issue certificates or renew tokens. Use etcd tooling for member changes and backup/restore;
do not clone a live data directory to add members.

## Node configuration

Install the same gpuq version on A/B. Example A configuration:

```toml
[cluster]
node_id = "a"
namespace = "training"
endpoints = ["http://a.internal:2379", "http://b.internal:2379", "http://w.internal:2379"]
work_dir = "~/.local/share/gpuq/clusters/training"
lease_seconds = 15
python = "/opt/conda/envs/train/bin/python"
# requirements = "requirements.lock"
# ca = "~/.config/gpuq/ca.pem"
# cert = "~/.config/gpuq/client.pem"
# key = "~/.config/gpuq/client-key.pem"
# token = "etcd-auth-token"

[cluster.nodes]
a = "local"
b = "gpu_b"

[remotes.gpu_b]
host = "user@b.internal"
command = ["~/.local/bin/gpuq"]
config = "~/.config/gpuq/config.toml"
```

B uses node_id b and the same namespace/endpoints. To submit from B, use `b="local"`, `a="gpu_a"`
and an SSH profile for A. Agents do not need SSH to elect or run tasks; input replication and remote logs do.
Training python/requirements come from each node's **cluster settings**, not SSH profile runtime fields.
Lock files reuse dependency environments; Python/CUDA/driver compatibility still requires validation.

| Setting | Default / behavior |
|---|---|
| backend | etcd when endpoints exist, otherwise sqlite; ssh for simple-pool workers |
| coordinator | Required for ssh, master SSH profile |
| gpu_allowlist | Unset: all local GPUs; array limits contributed cards, empty disables compute |
| node_id | Hostname; stable unique ID using letters, digits, dot, underscore or hyphen |
| namespace | default; must match across pool nodes |
| endpoints | Required nonempty URL array belonging to one etcd cluster |
| work_dir | ~/.local/share/gpuq/clusters/namespace; persistent journal, inputs, logs, caches |
| nodes | Current node_id → local only; submission needs at least two distinct real nodes |
| lease_seconds | 15 seconds, minimum 10; not a precise failure-detection guarantee |
| python | python3; training interpreter on this node |
| requirements | Unset; optional self-contained hashed lock inside the snapshot |
| ca / cert / key / token | Unset; HTTPS trust/client credentials and existing etcd token |

Settings load at agent startup. work_dir is separate from ordinary `--state-dir`. identity.json binds node
identity to its persistent journal; do not reuse a node ID with empty storage. One agent holds a workspace
lock. Do not run independent daemons against the same GPUs or duplicate a node ID across directories.

## Initialize and use

Initialize once; existing policy is never overwritten:

```bash
# On A: preferred master defaults to its cluster.node_id.
gpuq cluster init --max-running-jobs 2 --snapshot-copies 2 --failback-seconds 30
# On A and B, each with its own config:
gpuq --cluster daemon
# Alternative: gpuq --cluster daemon --background
```

```bash
gpuq cluster status
gpuq --cluster submit --project experiment --project-limit 2 --gpus 2 -- python train.py
gpuq --cluster list --json
gpuq --cluster show 1 --json
gpuq --cluster logs 1 --follow
gpuq --cluster cancel 1
gpuq --cluster submit --sync-include checkpoints/resume.ckpt -- python train.py --resume checkpoints/resume.ckpt
```

Submission validates first, replicates inputs to all configured nodes, then commits after at least
snapshot_copies acknowledgements. Any configured-node transfer failure aborts this submission; verified
cached blobs remain reusable. Only missing hashes are uploaded; model/checkpoint files are excluded by
default. Explicitly include resume checkpoints. Models downloaded through HF remain cached per node;
model caches are not automatically replicated. Each execution gets an isolated writable snapshot.
`--sync-preview` only shows the manifest. `--no-sync/--remote-cwd/--log` are unavailable in cluster mode.
Only nodes with confirmed input replicas can offer to execute a job.

Global init policy overrides each host's `[gpuq]` scheduling mode/total cap. Defaults: fair, unlimited total,
new ordinary-project cap 1. `submit --project-limit N` persistently updates the shared project cap.
With total cap 2, competing projects A/B get 1+1 ordinary slots; A alone can use its configured cap 2.
Positive priorities retain precedence; running jobs are never preempted. `--gpu` refers to physical indices
on the selected host; memory and reserve are per card. Dependencies wait for global success but **do not
transfer outputs**. Arrange shared data, durable outputs and checkpoint continuation separately.

## Election, failback and failures

etcd performs majority voting/replication; gpuq candidates acquire a leased scheduler identity using CAS.
The etcd leader and gpuq scheduling master need not be on the same host. Assignments compare the current
leader, job, project and node revisions. Accepted executions use a durable ID to deduplicate local enqueue.
After an ambiguous submit reply, inspect list/show before resubmitting: execution deduplication does not
merge separate submissions.

A returning preferred node reads authoritative state, reconciles local executions and publishes ready.
After the incumbent observes it stable for failback_seconds (default 30), leadership transfers atomically
to its lease. Training is not restarted. Lease expiry, requests and polling affect handover latency;
there is no fixed RTO promise.

Without a majority, no new executions can be authorized. Previously accepted work and independent runners
may continue, storing results locally. Disconnected assigned/starting/running jobs initially retain capacity.
The bounded retry policy below can supersede them and release logical capacity for a replacement. Jobs
with a cancellation request are not retried. This is not process migration: old training may still run.

OOM retries re-enter global admission with learned memory and cumulative attempts; fixed-mode budgets
survive node changes. Cluster workers disable local automatic orphan retries (`max_orphan_retries` is not
applied). Once the original node confirms process loss, failed/orphaned tasks can be manually retried with
`gpuq --cluster retry ID`. Unknown active work cannot be manually retried, but the policy below can replace it.
Restore the original journal to cancel superseded work; do not reuse a node ID after permanent disk loss.
Lost outputs/checkpoints are not automatically recovered.

Pending global jobs wait when no node offers resources, including insufficient capacity; one node alone
cannot declare a pool-wide job impossible. Accepted executions retain local impossible-memory checks.

## Automatic node-loss retries (duplicates possible)

New clusters default to **at-least-once** execution: retry when other runnable work drains **or** node loss
lasts 3600 seconds, at most once per global job. Policy is persisted in etcd for all masters. Older policy
records without these fields stay disabled; upgrading does not silently enable retries. Init never overwrites policy.

| cluster init option | Default | Behavior |
|---|---|---|
| --failover-after-seconds S | 3600 | Time since the master first observes the missing leased heartbeat; 0 disables timeout |
| --failover-when-idle / --no-failover-when-idle | Enabled | Retry after other runnable work drains |
| --max-failover-retries N | 1 | Persistent per-job node-loss retry budget; 0 disables entirely |

```bash
# New pool: timeout only, two hours, at most two retries.
gpuq cluster init --failover-after-seconds 7200 --no-failover-when-idle --max-failover-retries 2
# When duplicate execution is unacceptable:
gpuq cluster init --max-failover-retries 0
```

Other runnable work includes active work on online nodes and dependency-ready pending jobs with an online
input replica, even if currently waiting for GPU memory. Dependency-blocked jobs or those with all replicas
offline do not prevent the idle trigger. Idle is observed from a queue snapshot; a concurrent new submission
invalidates that retry transaction. Timeout ignores other work, but replacements still obey global/project/GPU
limits. An online alternative replica is required. Only one job is requeued per tick.

The loss timestamp survives master changes. A recovered heartbeat clears the interval; a later loss starts
a new interval. Masters use UTC wall clocks, so synchronize host clocks. Clock skew affects timeout timing.
The failover budget survives OOM, master changes and manual retry; --reset-attempts does not reset it.
OOM attempt counts also survive node changes.

Each replacement gets a new execution ID and isolated directory. History records why/when the old attempt
was superseded. Returning nodes cancel superseded local work and acknowledge cancellation; late old results
cannot overwrite the replacement. Already-completed work whose result was lost may run again. A partitioned
old process cannot be stopped immediately: there is no exactly-once guarantee.

Training receives GPUQ_CLUSTER_JOB_ID, GPUQ_EXECUTION_ID and GPUQ_FAILOVER_COUNT for output partitioning or
idempotency. Separate working directories do not isolate explicit shared absolute paths, databases or uploads;
use execution identity for those effects. Retries use the original submitted inputs, not an automatically
retrieved latest checkpoint from the lost node. Commands without resume logic restart from scratch.
`show --json` exposes failover_count, lost_since (local time with offset), and old cancellation acknowledgements.
Logical completion alone does not prove the old process stopped.

## Operations and validation

Use `KillMode=process` and ExecStart `gpuq --config /path/config.toml --cluster daemon` in a service.
The ordinary gpuqd.service does not automatically become a cluster agent. Background readiness means its
process lock is held, not that quorum/GPUs are healthy; inspect cluster status, --cluster doctor and agent.log.
Membership, old-queue migration and cache garbage collection are not automatic.

Records are limited to 1 MiB, responses to 16 MiB. Full queue scans target personal small pools, not large
clusters. Back up etcd and each work_dir; control metadata alone cannot restore checkpoints or local logs.

```bash
pytest tests/test_cluster.py
GPUQ_TEST_ETCD=/path/to/etcd pytest tests/test_cluster_etcd.py
```

Tests use temporary state and virtual GPUs. Real three-member loopback etcd tests cover member failure,
takeover/failback, stale writes, leases and quorum loss; transport tests use fake SSH with real subprocesses.
They do not replace physical-host network-failure tests or CUDA training acceptance after deployment.
