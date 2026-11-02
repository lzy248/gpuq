"""Real CLI subprocesses and transport protocol using a local fake OpenSSH executable."""
import io
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
import tarfile

import pytest

from gpuq.cli import build_parser
from gpuq.remote import request_for, ssh_command
from gpuq.sync import (manifest_for, materialize, missing_blobs, receive_blobs, snapshot_files,
                       validate_manifest, write_blobs)


@pytest.fixture
def remote_client(tmp_path):
    bindir = tmp_path / "bin"
    bindir.mkdir()
    ssh = bindir / "ssh"
    ssh.write_text("#!/bin/sh\nexec /bin/sh -c \"${!#}\"\n".replace("#!/bin/sh", "#!/bin/bash"))
    ssh.chmod(0o755)
    root = tmp_path / "project with spaces"
    root.mkdir()
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    (root / "pyproject.toml").write_text("# project marker\n")
    (root / "train.py").write_text("print('first')\n")
    state = tmp_path / "remote-state"
    workspace = tmp_path / "remote-workspace"
    config = tmp_path / "client.toml"
    config.write_text("[remotes.test]\n" + "\n".join(
        f"{key} = {json.dumps(value)}" for key, value in {
            "host": "fake-server", "command": [sys.executable, "-m", "gpuq"],
            "state_dir": str(state), "work_dir": str(workspace),
        }.items()))
    env = os.environ.copy()
    env.update(PATH=f"{bindir}:{env['PATH']}", PYTHONPATH=str(Path(__file__).resolve().parents[1] / "src"),
               GPUQ_STATE_DIR=str(tmp_path / "must-not-create-local-state"), GPUQ_CONFIG=str(config))

    def run(*arguments, success=True):
        result = subprocess.run([sys.executable, "-m", "gpuq", "--remote", "test", *arguments],
                                cwd=root, env=env, capture_output=True, text=True, timeout=30)
        if success:
            assert result.returncode == 0, result.stderr
        return result
    return run, root, state, workspace, config, ssh


def test_incremental_submit_snapshot_isolation_models_and_resume(remote_client):
    run, root, state, workspace, config, ssh = remote_client
    (root / "weights.safetensors").write_bytes(b"large model placeholder")
    (root / "resume.ckpt").write_bytes(b"checkpoint content")
    first = run("submit", "--", "python", "train.py", "--remote", "literal", "$(touch nope)")
    assert "uploading 2 new content blobs" in first.stderr
    job1 = json.loads(run("show", "1", "--json").stdout)
    snapshot1 = Path(job1["cwd"])
    assert job1["command"][-3:] == ["--remote", "literal", "$(touch nope)"]
    assert not (snapshot1 / "weights.safetensors").exists()
    assert not (snapshot1 / "resume.ckpt").exists()
    assert job1["env"]["HF_HOME"] == str(workspace / "cache/huggingface")
    assert "uploading 0 new content blobs" in run("submit", "--", "python", "train.py").stderr
    (root / "train.py").write_text("print('second')\n")
    third = run("submit", "--sync-include", "resume.ckpt", "--", "python", "train.py", "--resume", "resume.ckpt")
    assert "uploading 2 new content blobs" in third.stderr
    job3 = json.loads(run("show", "3", "--json").stdout)
    snapshot3 = Path(job3["cwd"])
    assert (snapshot1 / "train.py").read_text() == "print('first')\n"
    assert (snapshot3 / "resume.ckpt").read_bytes() == b"checkpoint content"
    assert job1["project_id"] == job3["project_id"]
    (snapshot3 / "train.py").write_text("mutated by training")
    assert "uploading 0 new content blobs" in run("submit", "--sync-include", "resume.ckpt", "--", "true").stderr
    job4 = json.loads(run("show", "4", "--json").stdout)
    assert (Path(job4["cwd"]) / "train.py").read_text() == "print('second')\n"
    assert not (state.parent / "must-not-create-local-state").exists()
    assert not (root / "nope").exists()


def test_remote_management_and_no_sync(remote_client):
    run, root, state, workspace, config, ssh = remote_client
    config.write_text(config.read_text() + '\npython = "/remote/conda/bin/python"\n')
    run("submit", "--no-sync", "--remote-cwd", str(root), "--", "python", "train.py")
    job = json.loads(run("show", "1", "--json").stdout)
    assert job["cwd"] == str(root)
    assert job["command"][0] == "/remote/conda/bin/python"
    assert not workspace.exists()
    Path(job["log_path"]).write_text("one\ntwo\n")
    assert run("logs", "1", "--tail", "1").stdout.strip() == "two"
    run("priority", "1", "9")
    run("cancel", "1")
    assert json.loads(run("show", "1", "--json").stdout)["status"] == "cancelled"
    run("retry", "1")
    assert json.loads(run("list", "--json").stdout)[0]["priority"] == 9
    assert len(json.loads(run("projects", "--json").stdout)) == 1
    assert run("submit", "--no-sync", "--", "true", success=False).returncode == 2


def test_preview_and_ssh_failure_never_submit(remote_client):
    run, root, state, workspace, config, ssh = remote_client
    preview = json.loads(run("submit", "--sync-preview", "--", "true").stdout)
    assert preview["files"] == ["pyproject.toml", "train.py"]
    assert not state.exists() and not workspace.exists()
    ssh.write_text("#!/bin/sh\nexit 255\n")
    assert run("list", success=False).returncode == 255
    assert run("submit", "--", "true", success=False).returncode != 0
    assert not state.exists()
    assert not (root.parent / "must-not-create-local-state").exists()


def test_absolute_paths_and_cache_override(remote_client):
    run, root, state, workspace, config, ssh = remote_client
    run("submit", "--env", f"PYTHONPATH={root}:{root}/src:/other", "--env", "HF_HOME=/shared/hf",
        "--", "python", str(root / "train.py"), f"--config={root}/experiment.json")
    job = json.loads(run("show", "1", "--json").stdout)
    assert job["command"][1] == job["cwd"] + "/train.py"
    assert job["command"][2] == "--config=" + job["cwd"] + "/experiment.json"
    assert job["env"]["PYTHONPATH"] == job["cwd"] + ":" + job["cwd"] + "/src:/other"
    assert job["env"]["HF_HOME"] == "/shared/hf"


def test_git_excludes_include_and_symlink(tmp_path):
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    (tmp_path / ".gitignore").write_text("ignored\n")
    (tmp_path / ".gpuqignore").write_text("omit*\n")
    for name in ["ignored", "tracked", "omit-me", ".env", "model.pt"]:
        (tmp_path / name).write_text(name)
    subprocess.run(["git", "-C", str(tmp_path), "add", "tracked"], check=True)
    (tmp_path / "link").symlink_to("tracked")
    selected = {p.name for p in snapshot_files(tmp_path, [])}
    assert selected == {".gitignore", ".gpuqignore", "tracked", "link"}
    assert "model.pt" in {p.name for p in snapshot_files(tmp_path, [], ["model.pt"])}
    (tmp_path / "bad").symlink_to("/etc/passwd")
    with pytest.raises(ValueError, match="leaves project"):
        snapshot_files(tmp_path, [])
    (tmp_path / "bad").unlink()
    (tmp_path / "tracked").unlink()
    assert "tracked" not in {p.name for p in snapshot_files(tmp_path, [])}


@pytest.mark.parametrize("manifest", [
    {"../evil": {"link": "fine"}}, {"/evil": {"link": "fine"}},
    {"a": {"link": "../outside"}}, {"a": {"link": "/outside"}},
    {"a": {"link": "b"}, "a/file": {"link": "x"}},
    {"a": {"sha256": "bad", "mode": 420}},
])
def test_reject_unsafe_manifests(manifest):
    with pytest.raises(ValueError):
        validate_manifest(manifest)


def test_transfer_rejects_changed_file_and_special_archive(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    (root / "code").write_text("one")
    manifest = manifest_for(root, [root / "code"])
    cache = tmp_path / "cache"
    missing = missing_blobs(manifest, cache)
    (root / "code").write_text("two")
    stream = io.BytesIO()
    write_blobs(root, manifest, missing, stream)
    stream.seek(0)
    with pytest.raises(ValueError, match="checksum"):
        receive_blobs(stream, cache, missing)
    assert list(cache.iterdir()) == []
    stream = io.BytesIO()
    with tarfile.open(fileobj=stream, mode="w") as tar:
        member = tarfile.TarInfo(missing[0])
        member.type = tarfile.SYMTYPE
        member.linkname = "/etc/passwd"
        tar.addfile(member)
    stream.seek(0)
    with pytest.raises(ValueError, match="unexpected"):
        receive_blobs(stream, cache, missing)


def test_mode_and_content_dedup_and_symlinks(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    for name in ["a", "b"]:
        (root / name).write_text("same contents")
    (root / "a").chmod(0o755)
    (root / "link").symlink_to("a")
    manifest = manifest_for(root, snapshot_files(root, []))
    cache = tmp_path / "cache"
    missing = missing_blobs(manifest, cache)
    assert len(missing) == 1
    stream = io.BytesIO()
    write_blobs(root, manifest, missing, stream)
    stream.seek(0)
    receive_blobs(stream, cache, missing)
    assert missing_blobs(manifest, cache) == []
    dest = tmp_path / "snapshot"
    dest.mkdir()
    materialize(manifest, cache, dest)
    assert (dest / "a").stat().st_mode & 0o777 == 0o755
    assert (dest / "link").read_text() == "same contents"
    assert (dest / "a").stat().st_ino != (cache / missing[0]).stat().st_ino


def test_quoting_and_command_remainder():
    remote = ssh_command({"host": "user@host", "command": ["~/space dir/gpuq", "$(touch /tmp/evil)"]})[-1]
    assert remote == 'exec "$HOME"/\'space dir/gpuq\' \'$(touch /tmp/evil)\' --gpuq-remote-call'
    argv = ["--remote", "submit", "submit", "--no-sync", "--remote-cwd", "/srv/code", "--", "echo", "--remote", "literal"]
    args = build_parser().parse_args(argv)
    request, root = request_for(args, argv, {"host": "submit"})
    assert root is None
    assert request["argv"] == ["submit", "--cwd", "/srv/code", "--", "echo", "--remote", "literal"]
    assert shlex.split("echo '$(touch nope)'")[1] == "$(touch nope)"


def test_remote_managed_environment_and_empty_subdirectory(remote_client):
    run, root, state, workspace, config, ssh = remote_client
    (root / "requirements.lock").write_text("# empty offline lock\n")
    (root / "empty").mkdir()
    config.write_text(config.read_text() + f'\npython = {json.dumps(sys.executable)}\nrequirements = "requirements.lock"\n')
    run("submit", "--cwd", str(root / "empty"), "--", "python", "-c", "print('ok')")
    first = json.loads(run("show", "1", "--json").stdout)
    assert Path(first["cwd"]).name == "empty"
    assert Path(first["cwd"]).is_dir()
    assert first["command"][0] == first["env"]["VIRTUAL_ENV"] + "/bin/python"
    run("submit", "--", "python", "train.py")
    second = json.loads(run("show", "2", "--json").stdout)
    assert second["env"]["VIRTUAL_ENV"] == first["env"]["VIRTUAL_ENV"]
    (root / "requirements.lock").write_text("# new lock identity\n")
    run("submit", "--", "python", "train.py")
    third = json.loads(run("show", "3", "--json").stdout)
    assert third["env"]["VIRTUAL_ENV"] != first["env"]["VIRTUAL_ENV"]
    assert Path(first["command"][0]).exists()


@pytest.mark.parametrize("header", [
    [], {"protocol": -1, "argv": ["list"]}, {"protocol": 2, "argv": [3]},
    {"protocol": 2, "argv": ["--remote", "loop", "list"]},
])
def test_invalid_internal_protocol(tmp_path, header):
    result = subprocess.run([sys.executable, "-m", "gpuq", "--gpuq-remote-call"],
                            input=json.dumps(header) + "\n", capture_output=True, text=True,
                            env={**os.environ, "GPUQ_STATE_DIR": str(tmp_path / "unused")})
    assert result.returncode == 2
    assert "Traceback" not in result.stderr
    assert not (tmp_path / "unused").exists()


def test_detached_remote_daemon_survives_transport_exit(remote_client):
    import signal
    import time
    from gpuq.background import daemon_running
    run, root, state, workspace, config, ssh = remote_client
    remote_config = root.parent / "remote.toml"
    remote_config.write_text('[gpuq]\nnvidia_smi = "/bin/true"\npoll_seconds = 0.05\n')
    config.write_text(config.read_text() + f'\nconfig = {json.dumps(str(remote_config))}\n')
    pid = None
    try:
        assert "daemon running" in run("daemon", "--background", "--max-running-jobs", "2").stdout
        pid = int((state / "gpuqd.lock").read_text())
        assert daemon_running(state / "gpuqd.lock")
        assert "already running" in run("daemon", "--background").stdout
        assert (state / "daemon.log").exists()
    finally:
        if pid is not None:
            os.kill(pid, signal.SIGTERM)
            deadline = time.monotonic() + 5
            while daemon_running(state / "gpuqd.lock") and time.monotonic() < deadline:
                time.sleep(0.05)
            assert not daemon_running(state / "gpuqd.lock")
