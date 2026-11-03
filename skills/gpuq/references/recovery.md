# Diagnosis and recovery

Read for waiting/failed tasks, reconnects, cancellations, and restarts. Use the same queue prefix
throughout. Start with show ID --json, a bounded logs ID --tail 100, gpus --json and projects --json.
Avoid using a normal daemon --dry-run as a read-only probe: reconciliation can change job state.

| Observed state | Inspect / next action |
|---|---|
| queued / waiting_memory | Scheduler availability, dependency, project/total caps, GPU count/allowlist, per-card memory+reserve, stability samples and exclusive policy |
| assigned (cluster) | Assigned node heartbeat/session, input availability and current leader; do not submit another copy |
| starting / running | Runner/worker state and latest logs; a quiet log alone does not prove a hung process |
| retry_wait | OOM evidence, learned memory, attempt count and capacity; this is already pending retry |
| blocked_dependency | Parent's state/error; fix or explicitly retry the dependency chain rather than bypassing it |
| failed | failure_kind, exit_code and current-attempt log tail; distinguish code errors, OOM, impossible capacity, launch/preparation failures and orphaned execution |
| cancelled / completed | Terminal record; verify required outputs separately if the user requested them |

Auto OOM retry is unbounded; fixed-memory retry uses max_retries. A repeatedly failing auto task should
be diagnosed for real capacity/model issues, not left looping as a substitute for fixing the workload.
Do not reset learning or attempts simply to make counters look new. For a user-requested terminal retry:

```bash
gpuq retry 42                    # preserve memory estimate/attempt count
gpuq retry 42 --memory auto      # explicitly recalibrate automatic memory
```

A retry reruns the stored command. Editing local source does not update an already-uploaded SSH/cluster
snapshot. After a code fix, submit the new snapshot as a new job when requested and record the relationship.
Neither retry nor --after automatically restores training state; the command must explicitly load an
available checkpoint.

Local/remote orphan recovery uses configured max_orphan_retries, default 0 (off), with a separate durable
budget. Keep it distinct from cluster's node-loss failover budget. Restarting the scheduler normally keeps
live independent runners. An absent process/heartbeat is not sufficient evidence to kill unrelated PIDs,
delete queue files, or clear execution identity.

Cancel via `gpuq cancel ID`; default --force-after is 10 seconds. In a cluster, an active cancellation
awaits the node's acknowledgement, so a lost node can retain capacity. Check current job and history
before saying cancellation completed. In a simple pool, only the original SQLite master resumes dispatch; workers do not elect a replacement master.
For cluster failover, consult [Shared pool](cluster.md).

Treat transient SSH/etcd failures as ambiguous results. Re-read the selected queue when reachable;
never blindly repeat submit. Report the last confirmed state and outstanding uncertainty if reconnecting
is not possible. show/list JSON timestamps are local with offsets; stored/log timestamps remain UTC.
