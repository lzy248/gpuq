"""Replicate content-addressed cluster inputs over SSH; never enqueue during transfer."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile

from .remote import HEADER_LIMIT, remote_profile, ssh_command
from .sync import manifest_for, missing_blobs, receive_blobs, snapshot_files, write_blobs

CLUSTER_FLAG = "--gpuq-cluster-call"


def snapshot(root: Path, relative_cwd: str, excludes: list[str], includes: list[str]) -> tuple[str, dict]:
    """Return an immutable manifest identity; blob bytes remain verified on receipt."""
    manifest = {"files": manifest_for(root, snapshot_files(root, excludes, includes)),
                "relative_cwd": relative_cwd, "source_root": str(root)}
    identity = hashlib.sha256(json.dumps(manifest, sort_keys=True).encode()).hexdigest()
    return identity, manifest


def save_manifest(work_dir: Path, identity: str, manifest: dict) -> None:
    expected = hashlib.sha256(json.dumps(manifest, sort_keys=True).encode()).hexdigest()
    if expected != identity:
        raise ValueError("cluster manifest checksum mismatch")
    directory = work_dir / "manifests"
    directory.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(mode="w", dir=directory, delete=False) as output:
        path = Path(output.name)
        try:
            json.dump(manifest, output)
            output.close()
            path.replace(directory / f"{identity}.json")
        finally:
            path.unlink(missing_ok=True)


def prepare_local(work_dir: Path, root: Path, identity: str, manifest: dict) -> None:
    """Cache verified blobs and then commit the manifest; no task side effects."""
    cache = work_dir / "blobs"
    missing = missing_blobs(manifest["files"], cache)
    with tempfile.TemporaryFile() as archive:
        write_blobs(root, manifest["files"], missing, archive)
        archive.seek(0)
        receive_blobs(archive, cache, missing)
    save_manifest(work_dir, identity, manifest)


def profile_for(name: str, config_path: Path | None) -> dict:
    from .cli import build_parser
    args = build_parser().parse_args(["--remote", name, "list"])
    args.config = str(config_path) if config_path else None
    return remote_profile(args)


def call(profile: dict, request: dict, *, root: Path | None = None, manifest: dict | None = None) -> dict:
    """One bounded SSH exchange; failed/ambiguous transfers are not job submissions."""
    command = ssh_command(profile)
    command[-1] = command[-1].replace("--gpuq-remote-call", CLUSTER_FLAG)
    request = {**request, "config": profile.get("config")}
    header = json.dumps(request).encode() + b"\n"
    if len(header) > HEADER_LIMIT:
        raise ValueError("cluster manifest exceeds transport limit")
    process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE)
    try:
        process.stdin.write(header)
        process.stdin.flush()
        if root is not None:
            reply = json.loads(process.stdout.readline(HEADER_LIMIT + 1))
            write_blobs(root, manifest["files"], reply["missing"], process.stdin)
        process.stdin.close()
        data = process.stdout.read(HEADER_LIMIT + 1)
        if len(data) > HEADER_LIMIT:
            raise ValueError("cluster response exceeds transport limit")
        if process.wait() != 0:
            raise RuntimeError("cluster node SSH request failed")
        return json.loads(data)
    except BaseException:
        process.terminate()
        process.wait()
        raise
    finally:
        process.stdout.close()
        try:
            process.stdin.close()
        except BrokenPipeError:
            pass


def serve() -> int:
    """Stage inputs or read one execution log; cluster election is never handled here."""
    from .cluster import load_cluster
    from .config import load_settings
    from .db import QueueDB

    header = sys.stdin.buffer.readline(HEADER_LIMIT + 1)
    if len(header) > HEADER_LIMIT or not header.endswith(b"\n"):
        raise ValueError("invalid cluster request")
    request = json.loads(header)
    settings = load_settings(config_path=request.get("config"))
    config = load_cluster(settings)
    if request.get("namespace") != config.namespace:
        raise ValueError("SSH node belongs to a different cluster namespace")
    if request.get("operation") == "prepare":
        manifest, identity = request["manifest"], request["identity"]
        missing = missing_blobs(manifest["files"], config.work_dir / "blobs")
        print(json.dumps({"missing": missing}), flush=True)
        receive_blobs(sys.stdin.buffer, config.work_dir / "blobs", missing)
        save_manifest(config.work_dir, identity, manifest)
        response = {"node": config.node_id, "identity": identity, "namespace": config.namespace}
    elif request.get("operation") == "logs":
        response = read_log(QueueDB(config.work_dir / "queue.sqlite3"), request)
    else:
        raise ValueError("unknown cluster node operation")
    print(json.dumps(response), flush=True)
    return 0


def read_log(db, request: dict) -> dict:
    """Read a bounded log chunk for a known idempotent execution, never an arbitrary path."""
    from .cli import _tail_lines
    job = db.by_submission_key(request["execution"])
    if job is None:
        return {"text": "", "offset": 0}
    path = Path(job.log_path)
    offset = request.get("offset")
    if offset is None:
        text = _tail_lines(path, int(request.get("lines", 100)))
        return {"text": text + ("\n" if text else ""), "offset": path.stat().st_size if path.exists() else 0}
    if int(offset) < 0:
        raise ValueError("log offset must be nonnegative")
    if not path.exists():
        return {"text": "", "offset": 0}
    with path.open("rb") as handle:
        handle.seek(min(int(offset), path.stat().st_size))
        data = handle.read(256 * 1024)
        return {"text": data.decode(errors="replace"), "offset": handle.tell()}
