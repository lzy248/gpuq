from pathlib import Path
import subprocess
import sys

import pytest

from gpuq.environment import prepare_environment
from gpuq.background import daemon_running, start_daemon
from gpuq.config import Settings


def test_environment_reuse_changes_and_failure_cleanup(tmp_path, monkeypatch):
    snapshot = tmp_path / "snapshot"
    snapshot.mkdir()
    lock = snapshot / "requirements.lock"
    lock.write_text("demo==1 --hash=sha256:" + "a" * 64)
    calls = []
    monkeypatch.setattr("gpuq.environment.subprocess.check_output", lambda args: b"python identity")

    def run(args, **kwargs):
        calls.append((args, kwargs))
        if args[1:3] == ["-m", "venv"]:
            Path(args[3]).mkdir()
    monkeypatch.setattr("gpuq.environment.subprocess.run", run)
    first = prepare_environment(tmp_path, snapshot, "requirements.lock", "python3")
    assert "--require-hashes" in calls[1][0]
    assert calls[1][1]["cwd"] == snapshot
    assert calls[1][1]["env"]["PIP_CACHE_DIR"] == str(tmp_path / "cache/pip")
    assert prepare_environment(tmp_path, snapshot, "requirements.lock", "python3") == first
    assert len(calls) == 2
    lock.write_text("demo==2 --hash=sha256:" + "b" * 64)
    second = prepare_environment(tmp_path, snapshot, "requirements.lock", "python3")
    assert first != second and (first / ".gpuq-ready").exists()
    assert len(calls) == 4
    lock.write_text("broken")
    def fail(args, **kwargs):
        raise subprocess.CalledProcessError(1, args)
    monkeypatch.setattr("gpuq.environment.subprocess.run", fail)
    with pytest.raises(subprocess.CalledProcessError):
        prepare_environment(tmp_path, snapshot, "requirements.lock", "python3")
    assert len(list((tmp_path / "environments").glob("*/.gpuq-ready"))) == 2


@pytest.mark.parametrize("contents", ["-r other.txt", "--requirement=other", "-e .", "-c constraints"])
def test_reject_non_self_contained_lock(tmp_path, contents):
    (tmp_path / "requirements.lock").write_text(contents)
    with pytest.raises(ValueError, match="self-contained"):
        prepare_environment(tmp_path, tmp_path, "requirements.lock", sys.executable)


def test_environment_real_empty_lock_offline(tmp_path, monkeypatch):
    # Actual venv + pip, but no packages and no package index/network access.
    monkeypatch.setenv("PIP_NO_INDEX", "1")
    snapshot = tmp_path / "snapshot"
    snapshot.mkdir()
    (snapshot / "requirements.lock").write_text("# empty, offline test\n")
    environment = prepare_environment(tmp_path, snapshot, "requirements.lock", sys.executable)
    result = subprocess.check_output([str(environment / "bin/python"), "-c", "import sys; print(sys.prefix)"], text=True)
    assert result.strip() == str(environment)
    assert prepare_environment(tmp_path, snapshot, "requirements.lock", sys.executable) == environment


def test_background_lock_and_reuse(tmp_path, capsys):
    import fcntl
    settings = Settings(state_dir=tmp_path)
    with settings.lock_path.open("a") as lock:
        assert not daemon_running(settings.lock_path)
        fcntl.flock(lock, fcntl.LOCK_EX)
        assert daemon_running(settings.lock_path)
        start_daemon(settings)
        assert "already running" in capsys.readouterr().out
    assert not daemon_running(settings.lock_path)
