# Shared GPU pool

Read when operating --cluster or diagnosing master/standby and disconnected nodes.

## Existing pool first

Use `gpuq --config /path/client.toml cluster status` and the matching --cluster list/show/gpus/doctor
commands. Ordinary --remote queues are independent, not automatically part of the pool. Do not initialize
or deploy a cluster simply to satisfy a request to submit into an existing one.

Two backends exist: newer source supports a fixed SQLite master with SSH workers, without etcd or a witness;
0.3.0 only supports etcd mode. For simple pools set backend="sqlite" on the master and backend="ssh",
coordinator="master_profile" on each worker. Worker-to-master SSH is required as well as input transfer
from submitter to workers. Only the fixed master dispatches; its downtime pauses new assignments while
training continues. Contributed GPU indices are configured by cluster.gpu_allowlist on each node.
[Simple pool setup](https://github.com/lzy248/gpuq/blob/main/docs/pool_usage.en.md) includes complete TOML examples.
Set [gpuq].default_queue="cluster" for ordinary submit/list/daemon routing; --local selects the independent queue.
Pool offers prefer fewer active tasks per GPU, subject to resource checks/project caps; newly assigned jobs count.

Optional HA mode needs three independent etcd voting members. The witness is a durable etcd member with full queue
metadata; it needs no GPU agent or model cache. GPU nodes run `gpuq --cluster daemon`. Namespace/endpoints
must match; node IDs must be unique and remain bound to their persistent work_dir journal. Do not delete
identity.json or reuse a node ID with empty storage to bypass registration failures.

--cluster and --remote cannot be combined. [cluster.nodes] maps node IDs to SSH profiles or local for input
replication/log retrieval. Each node's [cluster].python/requirements controls its training environment.
Normal --state-dir does not move cluster.work_dir. Shared admission policy is initialized in the master SQLite store or etcd;
--cluster daemon rejects local mode/total-cap overrides. gpuqd's default entry point is a local daemon,
so use the explicit gpuq --cluster daemon form for agents.

If cluster setup itself is requested, use the
[deployment guide](https://github.com/lzy248/gpuq/blob/v0.3.0/docs/cluster_usage.en.md)
for node config, etcd persistence/TLS, and service setup. `cluster init` does not overwrite existing policy.
Missing endpoints/SSH profiles are configuration work, not permission to invent hosts or copy credentials.

## Submissions and inspection

```bash
gpuq --cluster submit --sync-preview -- python train.py
gpuq --cluster submit --project experiment --project-limit 2 --gpus 2 -- python train.py
gpuq --cluster list --json
gpuq --cluster show 42 --json
gpuq --cluster logs 42 --tail 100
```

Use the real config prefix when needed. The example multi-GPU command must itself support multiple GPUs.
Input snapshots are staged to configured nodes before global submission, with at least two confirmed
replicas by default. A configured-node transfer failure aborts submission but preserves reusable cached
content. --no-sync, --remote-cwd and custom --log paths are unsupported. Model caches remain per-node;
resume checkpoints must be included or reachable on all eligible nodes.

Global IDs differ from node-local SQLite IDs. show includes node, execution, history, eligible_nodes,
failover_count and lost_since; logs reads the current execution, not a selectable historical execution.
`assigned` means globally assigned but not accepted yet. assigned/starting/running count against caps.

## Elections and retry policy

In etcd mode, lease/quorum protection elects a scheduler. Simple pools have only a fixed master. The initializing host is preferred by default; after it
returns and remains ready for the configured grace (default 30 seconds), leadership transfers back.
Missing quorum prevents new authorization; previously accepted work and independent runners can continue.
Never force a new master by editing etcd records or the local SQLite queue during ordinary recovery.

New 0.3.0 clusters default to at-least-once node-loss retry: other runnable work drains **or** loss lasts
one hour, at most once per task. Original nodes might still train during a partition. Keep the user's
chosen policy; do not enable retries or lower the timeout merely because a task is waiting.

| Init option | Default | Effect |
|---|---|---|
| --failover-after-seconds S | 3600 | 0 disables timeout trigger |
| --failover-when-idle / --no-failover-when-idle | enabled | Other runnable work drains trigger |
| --max-failover-retries N | 1 | Persistent per-job retry budget; 0 disables automatic node-loss retries |

Idle includes waiting-memory jobs whose dependencies succeeded and have online input replicas. A timeout
may requeue while other work runs, but the replacement still obeys admission limits. At least one other
input replica must be online. Loss timing is persisted; returning heartbeats reset the current interval.
Nodes should have synchronized clocks. Cancellation-requested tasks are not automatically requeued.

Replacements get separate execution IDs/directories. Returning nodes cancel superseded work and record
cancellation_ack; old results cannot replace the current result. Separate cwd does not isolate shared
absolute outputs, databases, or uploads. Use GPUQ_EXECUTION_ID for such output partitioning when the
workload supports it; GPUQ_CLUSTER_JOB_ID and GPUQ_FAILOVER_COUNT are also supplied.

A replacement uses original inputs, not the newest checkpoint on a lost host. Manual retry accepts only
terminal jobs; --reset-attempts does not reset failover_count. Missing-heartbeat active jobs must not be
manually declared complete or retried blindly. Report an exhausted retry budget, unavailable replica,
or missing quorum with the evidence rather than repeatedly submitting new jobs.
