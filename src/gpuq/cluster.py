"""Shared GPU queue with a fixed SQLite/SSH master or optional etcd election."""
from __future__ import annotations

from dataclasses import asdict, dataclass, fields, replace
from datetime import datetime
import fcntl
import hashlib
import json
import math
import os
from pathlib import Path
import re
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
import uuid

from .config import Settings, default_config_path, tomllib
from .coordination import EtcdStore, Record, Unavailable
from .db import QueueDB, utcnow
from .models import Job, TERMINAL_STATUSES
from .scheduler import Scheduler, cancel_job, order_project_jobs
from .sync import materialize

ACTIVE = {"assigned", "starting", "running"}
PENDING = {"queued", "waiting_memory", "retry_wait"}


@dataclass(frozen=True)
class ClusterConfig:
    node_id: str
    namespace: str
    endpoints: tuple[str, ...]
    work_dir: Path
    nodes: dict[str, str]
    lease_seconds: int = 15
    python: str = "python3"
    requirements: str | None = None
    ca: str | None = None
    cert: str | None = None
    key: str | None = None
    token: str | None = None
    backend: str = "etcd"
    coordinator: str | None = None
    config_path: Path | None = None
    gpu_allowlist: tuple[int, ...] | None = None

    def store(self):
        if self.backend == "sqlite":
            from .pool_store import SQLiteStore
            return SQLiteStore(self.work_dir / "control.sqlite3")
        if self.backend == "ssh":
            from .pool_store import SSHStore
            from .cluster_transfer import profile_for
            return SSHStore(profile_for(self.coordinator, self.config_path), self.namespace)
        return EtcdStore(list(self.endpoints), f"/gpuq/{self.namespace}",
                         ca=self.ca, cert=self.cert, key=self.key, token=self.token)


def load_cluster(settings: Settings) -> ClusterConfig:
    """Read persistent pool topology; endpoints retain legacy etcd mode, otherwise SQLite.

    SSH workers point coordinator at a profile for the sole master's configuration.
    gpu_allowlist restricts this node's contribution, not other applications' GPUs.
    """
    path = settings.config_path or default_config_path()
    with path.open("rb") as source:
        values = tomllib.load(source).get("cluster", {})
    endpoints = values.get("endpoints", [])
    backend = values.get("backend", "etcd" if endpoints else "sqlite")
    if backend not in {"sqlite", "ssh", "etcd"}:
        raise ValueError("cluster backend must be sqlite, ssh or etcd")
    if backend == "etcd" and (not isinstance(endpoints, list) or not endpoints or not all(isinstance(e, str) for e in endpoints)):
        raise ValueError("configure [cluster].endpoints with etcd client URLs first")
    if backend != "etcd" and endpoints:
        raise ValueError("endpoints only apply to the etcd backend")
    coordinator = values.get("coordinator")
    if backend == "ssh" and (not isinstance(coordinator, str) or not coordinator):
        raise ValueError("SSH pool workers require a coordinator profile")
    allowlist = values.get("gpu_allowlist")
    if allowlist is not None and (not isinstance(allowlist, list) or any(type(g) is not int or g < 0 for g in allowlist)):
        raise ValueError("cluster.gpu_allowlist must contain nonnegative GPU indices")
    node = values.get("node_id", socket.gethostname())
    namespace = values.get("namespace", "default")
    if not all(isinstance(v, str) and re.fullmatch(r"[A-Za-z0-9_.-]+", v) for v in (node, namespace)):
        raise ValueError("cluster node_id/namespace must use letters, digits, dot, underscore or hyphen")
    nodes = values.get("nodes", {node: "local"})
    if not isinstance(nodes, dict) or not all(
        isinstance(k, str) and re.fullmatch(r"[A-Za-z0-9_.-]+", k) and isinstance(v, str) and v
        for k, v in nodes.items()
    ):
        raise ValueError("cluster.nodes must map node IDs to SSH profiles, or local")
    ttl = int(values.get("lease_seconds", 15))
    if ttl < 10:
        raise ValueError("cluster lease_seconds must be at least 10")
    return ClusterConfig(
        node, namespace, tuple(endpoints),
        Path(values.get("work_dir", f"~/.local/share/gpuq/clusters/{namespace}")).expanduser().resolve(),
        nodes, ttl, str(values.get("python", "python3")), values.get("requirements"),
        *(str(Path(values[k]).expanduser()) if values.get(k) else None for k in ("ca", "cert", "key")),
        values.get("token"),
        backend, coordinator, path, tuple(sorted(set(allowlist))) if allowlist is not None else None,
    )


def as_job(value: dict) -> Job:
    """Decode the canonical job portion while ignoring cluster routing metadata."""
    values = {field.name: value[field.name] for field in fields(Job) if field.name in value}
    for key in ("command", "assigned_gpus"):
        if key in values:
            values[key] = tuple(values[key])
    if values.get("gpu_allowlist") is not None:
        values["gpu_allowlist"] = tuple(values["gpu_allowlist"])
    values["launch_free_memory_by_gpu"] = {int(k): v for k, v in values.get("launch_free_memory_by_gpu", {}).items()}
    return Job(**values)


def job_key(job_id: int) -> str:
    return f"jobs/{job_id:012d}"


def project_key(project: str) -> str:
    return "projects/" + hashlib.sha256(project.encode()).hexdigest()


def gpu_loads(records, node):
    """Count assigned and accepted pool tasks per physical GPU, including startup."""
    counts = {}
    for record in records.values():
        value = record.value
        if value["node"] == node and value["job"]["status"] in ACTIVE:
            for gpu in value["job"]["assigned_gpus"]:
                key = str(gpu)
                counts[key] = counts.get(key, 0) + 1
    return counts


class ClusterQueue:
    """Canonical queue operations; no SQLite replication or physical process control."""

    def __init__(self, store):
        self.store = store

    def policy(self) -> Record:
        record = self.store.get("config")
        if record is None:
            raise ValueError("cluster is not initialized; run gpuq cluster init")
        return record

    def initialize(self, preferred: str, *, limit: int, mode: str, copies: int, failback: float,
                   failover_after: float = 3600, failover_when_idle: bool = True,
                   failover_retries: int = 1, default_project_limit: int = 1) -> None:
        single_master = getattr(self.store, "single_master", False)
        if not single_master and self.store.members() < 3:
            raise ValueError("safe automatic takeover requires at least three voting etcd members")
        if limit < 0 or copies < (1 if single_master else 2) or not math.isfinite(failback) or failback < 0 or mode not in {"fair", "fifo"}:
            raise ValueError("invalid cluster policy or insufficient snapshot copies")
        if not math.isfinite(failover_after) or failover_after < 0 or failover_retries < 0 or default_project_limit < 1:
            raise ValueError("failover timeout and retry limit must be nonnegative and finite")
        policy = {"preferred_node": preferred, "max_running_jobs": limit, "scheduling_mode": mode,
                  "snapshot_copies": copies, "failback_seconds": failback, "default_project_limit": default_project_limit,
                  "failover_after_seconds": failover_after, "failover_when_idle": failover_when_idle,
                  "max_failover_retries": failover_retries}
        policy["single_master"] = single_master
        if not self.store.txn({"config": None, "counter": None}, {"config": policy, "counter": {"value": 0}}):
            raise ValueError("cluster already initialized; existing policy was not overwritten")

    def jobs(self) -> dict[str, Record]:
        return self.store.scan("jobs/")

    def submit(self, job: Job, values: dict, manifest: str, nodes: list[str], *, project_limit=None) -> int:
        """Commit a job only after enough input replicas; one transaction allocates its ID."""
        config = self.policy()
        if len(set(nodes)) < config.value["snapshot_copies"]:
            raise ValueError("not enough confirmed input replicas; no global task submitted")
        if values.get("dependency_id") is not None and self.store.get(job_key(values["dependency_id"])) is None:
            raise ValueError("cluster dependency does not exist")
        for _ in range(20):
            counter = self.store.get("counter")
            pk = project_key(job.project_id)
            project = self.store.get(pk)
            policy = dict(project.value) if project else {"project_id": job.project_id,
                                                        "limit": config.value.get("default_project_limit", 1), "turn": 0}
            if project_limit is not None:
                policy["limit"] = project_limit
            job_id = counter.value["value"] + 1
            spec = job.as_dict()
            spec.update(id=job_id, dependency_id=values.get("dependency_id"))
            record = {"job": spec, "submission": values, "manifest": manifest, "eligible_nodes": sorted(set(nodes)),
                      "node": None, "execution": None, "generation": 0, "executions": []}
            if self.store.txn({"counter": counter, pk: project, "config": config},
                              {"counter": {"value": job_id}, pk: policy, job_key(job_id): record}):
                return job_id
        raise RuntimeError("cluster submit contention; inspect queue before retrying")

    def update(self, job_id: int, change) -> dict:
        for _ in range(20):
            key = job_key(job_id)
            old = self.store.get(key)
            if old is None:
                raise KeyError(f"cluster job {job_id} not found")
            value = json.loads(json.dumps(old.value))
            change(value)
            value["job"]["updated_at"] = utcnow()
            if self.store.txn({key: old}, {key: value}):
                return value
        raise RuntimeError("cluster job changed repeatedly; retry after inspecting it")

    def dispatch_one(self, leader: Record, nodes: dict[str, Record], settings: Settings) -> int | None:
        """Plan from authoritative records, then fence the claim by leader/job/policy revisions."""
        config = self.policy()
        records = self.jobs()
        jobs = [as_job(r.value["job"]) for r in records.values()]
        by_id = {j.id: j for j in jobs}
        pending = []
        for job in jobs:
            if job.status not in PENDING or job.cancel_requested:
                continue
            if job.dependency_id is not None:
                dependency = by_id.get(job.dependency_id)
                if dependency is None or dependency.status in {"failed", "cancelled", "blocked_dependency"}:
                    key = job_key(job.id)
                    old = records[key]
                    value = json.loads(json.dumps(old.value))
                    value["job"].update(status="blocked_dependency", updated_at=utcnow())
                    self.store.txn({"leader": leader, key: old}, {key: value})
                    continue
                if dependency.status != "completed":
                    continue
            pending.append(job)
        projects = self.store.scan("projects/")
        policies = {r.value["project_id"]: (r.value["limit"], r.value["turn"]) for r in projects.values()}
        policy_settings = replace(settings, scheduling_mode=config.value["scheduling_mode"],
                                  max_running_jobs=config.value["max_running_jobs"])
        ordered = order_project_jobs(pending, [j for j in jobs if j.status in ACTIVE], policies, policy_settings)
        for job in ordered:
            key = job_key(job.id)
            old = records[key]
            candidates = []
            for nk, node in nodes.items():
                offer = node.value.get("offers", {}).get(str(job.id))
                if (node.value["node"] in old.value["eligible_nodes"] and offer
                        and offer["revision"] == old.revision):
                    loads = gpu_loads(records, node.value["node"])
                    if offer.get("gpu_loads", loads) != loads:
                        continue  # Refresh device selection after any other pool assignment.
                    count = max(loads.get(str(g), 0) for g in offer["gpus"])
                    node_active = sum(r.value["node"] == node.value["node"] and r.value["job"]["status"] in ACTIVE
                                      for r in records.values())
                    candidates.append((count, node_active, offer["utilization"], nk, node, offer))
            if not candidates:
                continue
            _, _, _, nk, node, offer = min(candidates, key=lambda item: item[:4])
            pk = project_key(job.project_id)
            project = projects[pk]
            # etcd revision is a persistent, strictly ordered admission turn.
            value = json.loads(json.dumps(old.value))
            value.update(node=node.value["node"], node_session=node.value["session"],
                         leader_token=leader.value["token"], execution=uuid.uuid4().hex,
                         generation=value["generation"] + 1, attempt_base=job.attempt)
            value["job"].update(status="assigned", updated_at=utcnow(), assigned_gpu=offer["gpus"][0],
                                assigned_gpus=offer["gpus"], cancel_requested=False)
            value["executions"].append({"execution": value["execution"], "node": value["node"]})
            policy = {**project.value, "turn": max(r.revision for r in [old, project, leader, config, node])}
            if self.store.txn({"leader": leader, key: old, pk: project, nk: node, "config": config},
                              {key: value, pk: policy}):
                return job.id
        return None

    def requeue_lost(self, leader: Record, nodes: dict[str, Record], now: float) -> bool:
        """Bounded at-least-once failover after idle/timeout; old work may still run.

        Persist loss observation and superseded execution tombstones under the leader
        fence. Returning workers cancel tombstoned local work; old results cannot
        overwrite the replacement. This is not physical process fencing.
        """
        config = self.policy()
        policy = config.value
        if not policy.get("max_failover_retries", 0):
            return False
        counter = self.store.get("counter")
        records = self.jobs()
        online = {record.value["node"] for record in nodes.values()}
        by_id = {r.value["job"]["id"]: r.value["job"] for r in records.values()}
        busy = False
        for record in records.values():
            value, job = record.value, record.value["job"]
            if job["status"] in ACTIVE and value["node"] in online:
                busy = True
            if job["status"] in PENDING and not job["cancel_requested"]:
                dependency = by_id.get(job["dependency_id"])
                ready = job["dependency_id"] is None or (dependency and dependency["status"] == "completed")
                if ready and online.intersection(value["eligible_nodes"]):
                    busy = True
        for key, record in records.items():
            value, job = record.value, record.value["job"]
            if job["status"] not in ACTIVE:
                continue
            node_key = "nodes/" + value["node"]
            checks = {"leader": leader, "config": config, key: record, node_key: nodes.get(node_key)}
            replacement = json.loads(json.dumps(value))
            if value["node"] in online:
                if value.get("lost_since") is not None:
                    replacement.pop("lost_since", None)
                    self.store.txn(checks, {key: replacement})
                continue
            lost_since = value.get("lost_since", now)
            replacement["lost_since"] = lost_since
            timeout = policy.get("failover_after_seconds", 0)
            timed_out = timeout > 0 and now - lost_since >= timeout
            idle = policy.get("failover_when_idle", False) and not busy
            can_retry = (not job["cancel_requested"]
                         and value.get("failover_count", 0) < policy["max_failover_retries"]
                         and online.intersection(value["eligible_nodes"]))
            if can_retry and (timed_out or idle):
                for execution in replacement["executions"]:
                    if execution["execution"] == value["execution"]:
                        execution.update(superseded=True, reason="node_timeout" if timed_out else "pool_idle",
                                         superseded_at=utcnow(), cancellation_ack=False)
                replacement.update(node=None, execution=None, lost_since=None,
                                   failover_count=value.get("failover_count", 0) + 1)
                replacement.pop("lost_since", None)
                replacement.pop("runtime", None)
                # Count a possibly launched but not yet reported attempt conservatively.
                attempt = job["attempt"] if job["status"] == "assigned" else max(
                    job["attempt"], value["attempt_base"] + 1)
                replacement["job"].update(status="queued", attempt=attempt, assigned_gpu=None,
                                           assigned_gpus=[], started_at=None, finished_at=None,
                                           error=None, failure_kind=None, exit_code=None, updated_at=utcnow())
                checks["counter"] = counter  # A concurrent new submission invalidates the idle observation.
                if self.store.txn(checks, {key: replacement}):
                    return True
            elif value.get("lost_since") is None:
                self.store.txn(checks, {key: replacement})
        return False


class WorkerDB:
    """Expose only currently authorized executions to the existing GPU scheduler."""
    def __init__(self, db: QueueDB):
        self.db = db
        self.allowed: set[int] = set()

    def __getattr__(self, name):
        return getattr(self.db, name)

    def runnable(self):
        return [job for job in self.db.runnable() if job.id in self.allowed]


class ClusterAgent:
    """One node: leased candidacy, durable executions, offers and fenced dispatch.

    Loss of quorum stops new authorization and global writes. Independent runners
    continue and retain results locally. Configured node-loss retries are explicitly
    at-least-once; superseded results are fenced and old executions cancelled on return.
    """
    def __init__(self, config: ClusterConfig, settings: Settings, *, store=None, backend=None,
                 launcher=None, clock=time.monotonic, wall_clock=time.time):
        self.config, self.settings = config, settings
        config.work_dir.mkdir(parents=True, exist_ok=True)
        self.store = store or config.store()
        self.queue = ClusterQueue(self.store)
        self.clock = clock
        self.wall_clock = wall_clock
        self.session = uuid.uuid4().hex
        self.lease = None
        self.preferred_seen = None
        self.storage_path = config.work_dir / "identity.json"
        if self.storage_path.exists():
            self.storage_id = json.loads(self.storage_path.read_text())["identity"]
        else:
            self.storage_id = uuid.uuid4().hex
            self.storage_path.write_text(json.dumps({"identity": self.storage_id}))
        self.db = QueueDB(config.work_dir / "queue.sqlite3")
        self.worker_db = WorkerDB(self.db)
        # Never infer a global retry from a missing process on a disconnected node.
        # Confirmed local orphan failures require explicit global retry instead.
        worker_settings = replace(settings, state_dir=config.work_dir, scheduling_mode="fifo",
                                  max_running_jobs=0, max_orphan_retries=0)
        self.scheduler = Scheduler(settings=worker_settings, db=self.worker_db, backend=backend, launcher=launcher)
        self.offers = Scheduler(settings=worker_settings, db=self.db, backend=self.scheduler.backend)

    def ensure_session(self):
        key = "registry/" + self.config.node_id
        old = self.store.get(key)
        if old is not None and old.value["storage"] != self.storage_id:
            raise ValueError("node ID already belongs to different storage; restore its journal or choose a new ID")
        if old is None and not self.store.txn({key: None}, {key: {"storage": self.storage_id}}):
            raise Unavailable("node registration changed; retry next tick")
        if self.lease is None:
            self.lease = self.store.grant(self.config.lease_seconds)
            self.session = uuid.uuid4().hex
        else:
            self.store.keepalive(self.lease)

    @property
    def node_key(self):
        return "nodes/" + self.config.node_id

    def publish(self, records: dict[str, Record]):
        """Publish only offers backed by this node's actual resource checks and input cache."""
        offers = {}
        states = []
        error = None
        try:
            states = self.scheduler.backend.sample()
            if self.config.gpu_allowlist is not None:
                states = [state for state in states if state.index in self.config.gpu_allowlist]
            loads = gpu_loads(records, self.config.node_id)
            candidates = [as_job(r.value["job"]) for r in records.values()
                          if r.value["job"]["status"] in PENDING and not r.value["job"]["cancel_requested"]
                          and self.config.node_id in r.value["eligible_nodes"]
                          and (self.config.work_dir / "manifests" / f'{r.value["manifest"]}.json').is_file()]
            policy = self.queue.policy().value
            projects = {r.value["project_id"]: (r.value["limit"], r.value["turn"])
                        for r in self.store.scan("projects/").values()}
            active = [as_job(r.value["job"]) for r in records.values() if r.value["job"]["status"] in ACTIVE]
            candidates = order_project_jobs(candidates, active, projects, replace(
                self.settings, scheduling_mode=policy["scheduling_mode"], max_running_jobs=policy["max_running_jobs"]))
            for decision in self.offers.decisions(candidates, states, gpu_loads=loads)[:256]:
                record = records[job_key(decision.job_id)]
                offers[str(decision.job_id)] = {"revision": record.revision, "gpus": decision.gpu_indices,
                                                "utilization": decision.utilization_percent, "gpu_loads": loads}
        except (OSError, subprocess.SubprocessError) as exc:
            error = type(exc).__name__
        node = {"node": self.config.node_id, "session": self.session, "lease": self.lease,
                "ready": True, "offers": offers, "active": len(self.db.active()), "gpu_error": error,
                "gpus": [asdict(state) for state in states]}
        self.store.txn({}, {self.node_key: node}, leases={self.node_key: self.lease})

    def election(self) -> Record | None:
        """Claim the fixed master lease, or elect/fail back when using an etcd quorum."""
        config = self.queue.policy()
        leader = self.store.get("leader")
        if config.value.get("single_master") and self.config.node_id != config.value["preferred_node"]:
            return leader
        preferred_key = "nodes/" + config.value["preferred_node"]
        preferred = self.store.get(preferred_key)
        own = {"node": self.config.node_id, "token": self.session}
        if leader is None:
            if preferred and preferred.value["node"] != self.config.node_id:
                return None
            self.store.txn({"leader": None}, {"leader": own}, leases={"leader": self.lease})
            leader = self.store.get("leader")
        if leader and leader.value == own and preferred and preferred.value["node"] != self.config.node_id:
            identity = preferred.value["session"]
            if not preferred.value.get("ready"):
                self.preferred_seen = None
            elif self.preferred_seen is None or self.preferred_seen[0] != identity:
                self.preferred_seen = (identity, self.clock())
            elif self.clock() - self.preferred_seen[1] >= config.value["failback_seconds"]:
                target = {"node": preferred.value["node"], "token": identity}
                self.store.txn({"leader": leader, preferred_key: preferred, "config": config},
                               {"leader": target}, leases={"leader": preferred.value["lease"]})
                leader = self.store.get("leader")
        else:
            self.preferred_seen = None
        return leader

    def _prepare_execution(self, value: dict) -> Job:
        """Idempotently materialize/record an accepted execution before any process starts."""
        execution = value["execution"]
        old = self.db.by_submission_key(execution)
        if old:
            return old
        from .environment import prepare_environment
        from .remote import remap_path
        manifest = json.loads((self.config.work_dir / "manifests" / f'{value["manifest"]}.json').read_text())
        parent = self.config.work_dir / "executions"
        parent.mkdir(exist_ok=True)
        destination = parent / execution
        if not destination.exists():
            temporary = Path(tempfile.mkdtemp(prefix="preparing-", dir=parent))
            try:
                materialize(manifest["files"], self.config.work_dir / "blobs", temporary)
                temporary.rename(destination)
            except BaseException:
                shutil.rmtree(temporary, ignore_errors=True)
                raise
        cwd = (destination / manifest["relative_cwd"]).resolve()
        if not cwd.is_relative_to(destination):
            raise ValueError("execution cwd escapes snapshot")
        cwd.mkdir(parents=True, exist_ok=True)
        submission = dict(value["submission"])
        submission.update(cwd=str(cwd), dependency_id=None, submission_key=execution,
                          gpu_allowlist=value["job"]["assigned_gpus"], project_limit=None,
                          log_path=str(self.config.work_dir / "logs" / f"{execution}.log"),
                          memory_mb=value["job"]["memory_mb"], memory_mode=value["job"]["memory_mode"])
        # A new node-local row must not reset the global fixed-memory OOM budget.
        submission["max_retries"] = max(0, submission["max_retries"] - value["attempt_base"])
        command = [remap_path(part, manifest["source_root"], destination) for part in submission["command"]]
        env = {k: remap_path(k + "=" + v, manifest["source_root"], destination).split("=", 1)[1]
               for k, v in submission["env"].items()}
        env.update(GPUQ_CLUSTER_JOB_ID=str(value["job"]["id"]), GPUQ_EXECUTION_ID=execution,
                   GPUQ_FAILOVER_COUNT=str(value.get("failover_count", 0)))
        for key, subdir in (("HF_HOME", "huggingface"), ("TORCH_HOME", "torch"), ("PIP_CACHE_DIR", "pip")):
            env.setdefault(key, os.environ.get(key, str(self.config.work_dir / "cache" / subdir)))
        python = Path(self.config.python).expanduser()
        if self.config.requirements:
            environment = prepare_environment(self.config.work_dir, destination, self.config.requirements, str(python))
            python = environment / "bin/python"
            env["VIRTUAL_ENV"] = str(environment)
            env["PATH"] = f"{python.parent}:{os.environ.get('PATH', '/usr/bin:/bin')}"
        elif python.is_absolute():
            env.setdefault("PATH", f"{python.parent}:{os.environ.get('PATH', '/usr/bin:/bin')}")
        if re.fullmatch(r"python(?:\d+(?:\.\d+)*)?", Path(command[0]).name):
            command[0] = str(python)
        submission.update(command=command, env=env)
        return self.db.submit(**submission)

    def reconcile(self, records: dict[str, Record], leader: Record | None):
        """Fence new starts; report known attempts without requiring this node to be leader."""
        self.worker_db.allowed = set()
        for key, record in records.items():
            value = record.value
            # Superseded attempts may still be running after a network partition.
            # Cancel only this journal's owned execution; never touch the new attempt.
            acknowledgements = {}
            for execution in value["executions"]:
                if (execution["node"] == self.config.node_id and execution.get("superseded")
                        and not execution.get("cancellation_ack")):
                    old = self.db.by_submission_key(execution["execution"])
                    if old is not None and old.status not in TERMINAL_STATUSES:
                        old = cancel_job(self.db, old.id, force_after=10)
                    if old is None or old.status in TERMINAL_STATUSES:
                        acknowledgements[execution["execution"]] = old.status if old else "never_started"
            if acknowledgements:
                updated = json.loads(json.dumps(value))
                for execution in updated["executions"]:
                    if execution["execution"] in acknowledgements:
                        execution.update(cancellation_ack=True, old_status=acknowledgements[execution["execution"]])
                if not self.store.txn({key: record}, {key: updated}):
                    continue
                record = self.store.get(key)
                value = record.value
            spec = value["job"]
            if value["node"] != self.config.node_id or spec["status"] not in ACTIVE:
                continue
            if spec["status"] == "assigned":
                if spec["cancel_requested"]:
                    cancelled = json.loads(json.dumps(value))
                    cancelled["job"].update(status="cancelled", finished_at=utcnow(), updated_at=utcnow())
                    self.store.txn({key: record}, {key: cancelled})
                    continue
                if (leader is None or value["leader_token"] != leader.value["token"]
                        or value["node_session"] != self.session):
                    continue
                accepted = json.loads(json.dumps(value))
                accepted["job"].update(status="starting", updated_at=utcnow())
                if not self.store.txn({"leader": leader, key: record}, {key: accepted}):
                    continue
                record = self.store.get(key)
                value, spec = record.value, record.value["job"]
            try:
                local = self._prepare_execution(value)
            except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as error:
                failed = json.loads(json.dumps(value))
                failed["job"].update(status="failed", failure_kind="cluster_prepare", error=str(error),
                                     finished_at=utcnow(), updated_at=utcnow())
                self.store.txn({key: record}, {key: failed})
                continue
            if spec["cancel_requested"]:
                local = cancel_job(self.db, local.id, force_after=value.get("cancel_force_after", 10))
            elif local.status in PENDING:
                # Carry learned bounds into each new cluster execution before first claim.
                if local.attempt == 0:
                    with self.db.connect() as connection:
                        connection.execute("UPDATE jobs SET oom_count=?,peak_memory_mb=? WHERE id=?",
                                           (spec["oom_count"], spec["peak_memory_mb"], local.id))
                self.worker_db.allowed.add(local.id)
            local = self.db.get(local.id)
            reported = json.loads(json.dumps(value))
            status = "starting" if local.status in {"queued", "waiting_memory"} else local.status
            for field in ("status", "memory_mb", "memory_mode", "oom_count", "peak_memory_mb", "exit_code",
                          "error", "failure_kind", "started_at", "finished_at", "assigned_gpu", "assigned_gpus"):
                reported["job"][field] = local.as_dict()[field]
            if local.status in {"queued", "waiting_memory"}:
                reported["job"].update(assigned_gpu=spec["assigned_gpu"], assigned_gpus=spec["assigned_gpus"])
            reported["job"].update(status=status, attempt=value["attempt_base"] + local.attempt, updated_at=utcnow())
            reported["runtime"] = local.as_dict()
            # Read the latest revision before launching: cancellation/updates must not be overwritten.
            if not self.store.txn({key: record}, {key: reported}):
                self.worker_db.allowed.discard(local.id)
            if status == "retry_wait":
                self.worker_db.allowed.discard(local.id)

    def adopt_assignments(self, leader: Record, nodes: dict[str, Record]):
        """A new leader reauthorizes the same node/attempt, never duplicates unknown work."""
        for key, record in self.queue.jobs().items():
            value = record.value
            node = nodes.get("nodes/" + str(value["node"]))
            if value["job"]["status"] != "assigned" or node is None:
                continue
            if value["leader_token"] == leader.value["token"] and value["node_session"] == node.value["session"]:
                continue
            replacement = {**value, "leader_token": leader.value["token"], "node_session": node.value["session"]}
            self.store.txn({"leader": leader, key: record, "nodes/" + value["node"]: node}, {key: replacement})

    def tick(self, *, dry_run=False):
        if dry_run:
            return {"leader": self.store.get("leader"), "jobs": len(self.queue.jobs())}
        try:
            self.ensure_session()
            # Classify pre-reboot/missing processes before acting on historical PIDs.
            # This also reports a confirmed orphan without inventing a cross-node retry.
            self.scheduler.reconcile_active()
            records = self.queue.jobs()
            leader = self.store.get("leader")
            self.reconcile(records, leader)
            try:
                self.scheduler.tick()
            except (OSError, subprocess.SubprocessError):
                pass  # A CPU-only controller can still lead; publish marks GPU failure.
            records = self.queue.jobs()
            self.publish(records)
            leader = self.election()
            if leader and leader.value == {"node": self.config.node_id, "token": self.session}:
                nodes = self.store.scan("nodes/")
                self.queue.requeue_lost(leader, nodes, self.wall_clock())
                self.adopt_assignments(leader, nodes)
                return self.queue.dispatch_one(leader, nodes, self.settings)
        except Unavailable:
            self.lease = None
            self.preferred_seen = None
            self.worker_db.allowed = set()
            raise
        return None


def run_agent(config: ClusterConfig, settings: Settings, *, once=False, dry_run=False) -> None:
    """Hold a node-local process lock; continue polling through temporary quorum loss."""
    config.work_dir.mkdir(parents=True, exist_ok=True)
    with (config.work_dir / "agent.lock").open("a+") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise RuntimeError("cluster agent already running for this node workspace") from error
        lock.seek(0)
        lock.truncate()
        lock.write(str(os.getpid()))
        lock.flush()
        agent = ClusterAgent(config, settings)
        stop = False

        def request_stop(_signum, _frame):
            nonlocal stop
            stop = True
        signal.signal(signal.SIGTERM, request_stop)
        signal.signal(signal.SIGINT, request_stop)
        while not stop:
            try:
                agent.tick(dry_run=dry_run)
            except (Unavailable, OSError, subprocess.SubprocessError) as error:
                print(f"cluster tick deferred: {error}", file=sys.stderr, flush=True)
            if once:
                break
            deadline = time.monotonic() + min(settings.poll_seconds, config.lease_seconds / 3)
            while not stop and time.monotonic() < deadline:
                time.sleep(max(0, min(0.25, deadline - time.monotonic())))


def start_agent(config: ClusterConfig, settings: Settings):
    from .background import daemon_running
    config.work_dir.mkdir(parents=True, exist_ok=True)
    path = config.work_dir / "agent.lock"
    if daemon_running(path):
        print("cluster agent already running; existing settings retained")
        return
    command = [sys.executable] if getattr(sys, "frozen", False) else [sys.executable, "-m", "gpuq"]
    command += ["--cluster", "--state-dir", str(settings.state_dir)]
    if settings.config_path:
        command += ["--config", str(settings.config_path)]
    command += ["daemon", "--poll-seconds", str(settings.poll_seconds)]
    with (config.work_dir / "agent.log").open("ab") as output:
        process = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=output, stderr=output,
                                   start_new_session=True)
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        if daemon_running(path):
            print(f"cluster agent running; log: {config.work_dir / 'agent.log'}")
            return
        if process.poll() is not None:
            raise RuntimeError("cluster agent startup failed; inspect agent.log")
        time.sleep(0.05)
    raise RuntimeError("cluster agent readiness not confirmed; inspect agent.log")


def submit_cluster(args, settings: Settings, config: ClusterConfig, queue: ClusterQueue):
    """Validate locally, replicate inputs, then atomically publish a global job."""
    from .cli import submission_values
    from .cluster_transfer import call, prepare_local, profile_for, snapshot
    from .projects import project_root
    if args.no_sync or args.remote_cwd or args.log:
        raise ValueError("cluster submit requires replicated snapshots and managed logs; no --no-sync/--remote-cwd/--log")
    values = submission_values(args, settings)
    root = project_root(values["cwd"])
    project = args.project or str(root)
    values["project_id"] = project
    after = values["dependency_id"]
    # Reuse exactly the local submit validators without touching a user's queue.
    with tempfile.TemporaryDirectory(prefix="gpuq-validate-") as directory:
        validation = QueueDB(Path(directory) / "queue.sqlite3")
        job = validation.submit(**{**values, "dependency_id": None})
    values["dependency_id"] = after
    identity, manifest = snapshot(root, Path(values["cwd"]).relative_to(root).as_posix(),
                                  args.sync_exclude, args.sync_include)
    if args.sync_preview:
        print(json.dumps({"project_root": str(root), "manifest": identity, "files": sorted(manifest["files"]),
                          "nodes": list(config.nodes)}, ensure_ascii=False, indent=2))
        return
    policy = queue.policy()
    if len(config.nodes) < policy.value["snapshot_copies"]:
        raise ValueError("configure enough cluster.nodes for independent input replicas")
    if after is not None and queue.store.get(job_key(after)) is None:
        raise ValueError("cluster dependency does not exist")
    replicas = []
    for node, profile_name in config.nodes.items():
        if profile_name == "local":
            if node != config.node_id:
                raise ValueError("local transfer node ID must match cluster.node_id")
            prepare_local(config.work_dir, root, identity, manifest)
        else:
            response = call(profile_for(profile_name, settings.config_path),
                            {"operation": "prepare", "identity": identity, "manifest": manifest, "namespace": config.namespace},
                            root=root, manifest=manifest)
            if response != {"node": node, "identity": identity, "namespace": config.namespace}:
                raise ValueError("SSH profile points to a different cluster node or manifest")
        replicas.append(node)
    job_id = queue.submit(job, values, identity, replicas, project_limit=args.project_limit)
    print(f"submitted cluster job {job_id}; inputs replicated to {', '.join(replicas)}")


def dispatch(args) -> int:
    """Route explicit/default pool commands to one authoritative shared queue."""
    from .cli import _display_timestamp, _job_display_dict, _job_line
    from .cluster_transfer import call, profile_for, read_log
    from .config import load_settings
    settings = load_settings(state_dir=args.state_dir, config_path=args.config)
    if any(getattr(args, key, None) for key in (
        "ssh_port", "ssh_identity", "remote_command", "remote_config", "remote_workdir",
    )):
        raise ValueError("cluster SSH settings belong in named remote profiles")
    config = load_cluster(settings)
    queue = ClusterQueue(config.store())
    if args.subcommand == "cluster":
        if args.cluster_command == "init":
            if config.backend == "ssh":
                raise ValueError("initialize a simple pool on its SQLite master")
            if config.backend == "sqlite" and args.preferred_node not in {None, config.node_id}:
                raise ValueError("a SQLite pool's master must be the initializing node")
            queue.initialize(args.preferred_node or config.node_id, limit=args.max_running_jobs,
                             mode=args.scheduling_mode, copies=args.snapshot_copies, failback=args.failback_seconds,
                             failover_after=args.failover_after_seconds, failover_when_idle=args.failover_when_idle,
                             failover_retries=args.max_failover_retries,
                             default_project_limit=settings.default_project_limit)
            print(f"initialized cluster {config.namespace}; preferred master: {args.preferred_node or config.node_id}")
            if args.max_failover_retries:
                print("node-loss retries enabled: old and replacement training may overlap until reconnection")
        else:
            leader = queue.store.get("leader")
            print(json.dumps({"leader": leader.value if leader else None,
                              "backend": config.backend,
                              "policy": queue.policy().value,
                              "nodes": [r.value for r in queue.store.scan('nodes/').values()]}, indent=2))
        return 0
    if args.subcommand == "submit":
        submit_cluster(args, settings, config, queue)
        return 0
    if args.subcommand == "daemon":
        if args.max_running_jobs is not None or args.scheduling_mode is not None:
            raise ValueError("cluster scheduling policy is shared; configure it with cluster init")
        if args.poll_seconds is not None:
            if args.poll_seconds <= 0:
                raise ValueError("poll interval must be positive")
            settings = replace(settings, poll_seconds=args.poll_seconds)
        if args.background:
            if args.once or args.dry_run:
                raise ValueError("background is incompatible with once/dry-run")
            start_agent(config, settings)
        else:
            run_agent(config, settings, once=args.once, dry_run=args.dry_run)
        return 0
    if args.subcommand in {"list", "ls"}:
        records = [r.value for r in queue.jobs().values()]
        records.sort(key=lambda v: (v['job']['status'] not in ACTIVE, -v['job']['priority'], v['job']['id']))
        if args.status:
            records = [v for v in records if v['job']['status'] in args.status]
        if args.limit >= 0:
            records = records[:args.limit]
        if args.json:
            print(json.dumps([{**_job_display_dict(as_job(v['job'])), 'node': v['node'],
                               'execution': v['execution']} for v in records], ensure_ascii=False, indent=2))
        else:
            for value in records:
                print(_job_line(as_job(value['job'])) + f" node={value['node'] or '-'}")
        return 0
    if args.subcommand == "projects":
        jobs = [r.value['job'] for r in queue.jobs().values()]
        rows = [{"project_id": r.value['project_id'], "concurrency_limit": r.value['limit'],
                 "last_served": r.value['turn'],
                 "active": sum(j['project_id'] == r.value['project_id'] and j['status'] in ACTIVE for j in jobs),
                 "pending": sum(j['project_id'] == r.value['project_id'] and j['status'] in PENDING for j in jobs)}
                for r in queue.store.scan('projects/').values()]
        print(json.dumps(rows, ensure_ascii=False, indent=2))
        return 0
    if args.subcommand == "gpus":
        print(json.dumps([r.value for r in queue.store.scan('nodes/').values()], indent=2))
        return 0
    if args.subcommand == "doctor":
        print(json.dumps({"voting_members": queue.store.members(), "policy": queue.policy().value,
                          "leader": bool(queue.store.get('leader'))}, indent=2))
        return 0
    if args.subcommand in {"show", "logs", "log", "cancel", "retry", "priority"}:
        key = job_key(args.job_id)
        record = queue.store.get(key)
        if record is None:
            raise KeyError(f"cluster job {args.job_id} not found")
        if args.subcommand == "show":
            value = record.value
            history = [{**entry, **({"superseded_at": _display_timestamp(entry['superseded_at'])}
                                    if entry.get('superseded_at') else {})} for entry in value['executions']]
            lost_since = value.get('lost_since')
            output = {**_job_display_dict(as_job(value['job'])), "node": value['node'],
                      "execution": value['execution'], "executions": history,
                      "manifest": value['manifest'], "eligible_nodes": value['eligible_nodes'],
                      "failover_count": value.get('failover_count', 0),
                      "lost_since": datetime.fromtimestamp(lost_since).astimezone().isoformat()
                      if lost_since is not None else None}
            if args.json:
                print(json.dumps(output, ensure_ascii=False, indent=2))
            else:
                for k, v in output.items():
                    print(f"{k:20s}: {v}")
        elif args.subcommand in {"logs", "log"}:
            offset = None
            execution = None
            try:
                while True:
                    value = queue.store.get(key).value
                    if value['execution']:
                        if execution != value['execution']:
                            offset, execution = None, value['execution']
                        request = {"operation": "logs", "execution": execution, "offset": offset, "lines": args.lines,
                                   "namespace": config.namespace}
                        profile = config.nodes.get(value['node'])
                        if profile is None:
                            raise ValueError("configure the execution node's SSH profile to read logs")
                        response = (read_log(QueueDB(config.work_dir / 'queue.sqlite3'), request)
                                    if profile == 'local' else call(profile_for(profile, settings.config_path), request))
                        print(response['text'], end='', flush=True)
                        offset = response['offset']
                    if not args.follow or value['job']['status'] in TERMINAL_STATUSES:
                        break
                    time.sleep(1)
            except KeyboardInterrupt:
                return 130
        else:
            def change(value):
                job = value['job']
                if args.subcommand == 'priority':
                    job['priority'] = args.value
                elif args.subcommand == 'cancel':
                    job['cancel_requested'] = True
                    value['cancel_force_after'] = args.force_after
                    if job['status'] in PENDING:
                        job.update(status='cancelled', finished_at=utcnow())
                else:
                    if job['status'] not in TERMINAL_STATUSES:
                        raise ValueError("cannot retry an active or uncertain cluster execution")
                    job.update(status='queued', cancel_requested=False, assigned_gpu=None, assigned_gpus=[],
                               started_at=None, finished_at=None, failure_kind=None, error=None, exit_code=None)
                    if args.reset_attempts:
                        job['attempt'] = 0
                    if args.memory_mb is not None:
                        auto = args.memory_mb == 'auto'
                        if not auto and int(args.memory_mb) <= 0:
                            raise ValueError("memory must be positive or auto")
                        job.update(memory_mode='auto' if auto else 'fixed', memory_mb=1 if auto else int(args.memory_mb),
                                   oom_count=0, peak_memory_mb=0)
                        value['submission']['retry_on_oom'] = auto or value['submission']['retry_on_oom']
                        job['retry_on_oom'] = value['submission']['retry_on_oom']
                    value.update(node=None, execution=None)
            value = queue.update(args.job_id, change)
            print(f"cluster job {args.job_id}: {value['job']['status']}")
        return 0
    raise ValueError(f"{args.subcommand} is not available in cluster mode")
