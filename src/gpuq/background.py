"""Explicit detached daemon startup for local and SSH clients."""
from __future__ import annotations

import fcntl
from pathlib import Path
import subprocess
import sys
import time

from .config import Settings, ensure_state_dirs


def daemon_running(path: Path) -> bool:
    """Probe the state-directory flock without retaining it.

    The parent directory must exist; opening the lock may create the file. True
    means another process holds the lock, not that GPU monitoring is healthy.
    """
    with path.open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return True
        return False


def start_daemon(settings: Settings) -> None:
    """Start a detached scheduler unless its state-directory lock is already held.

    Existing instances retain their configuration. The child receives explicit
    state/config paths and CLI overrides; output appends to daemon.log. Wait up to
    ten seconds for the lock, not GPU readiness. Startup failure/timeout raises
    RuntimeError; timeout does not kill a child that may still become ready.
    """
    ensure_state_dirs(settings)
    if daemon_running(settings.lock_path):
        print("daemon already running; existing settings retained")
        return
    command = [sys.executable] if getattr(sys, "frozen", False) else [sys.executable, "-m", "gpuq"]
    command += ["--local", "--state-dir", str(settings.state_dir)]
    if settings.config_path:
        command += ["--config", str(settings.config_path)]
    command += ["daemon", "--poll-seconds", str(settings.poll_seconds), "--max-running-jobs",
                str(settings.max_running_jobs), "--scheduling", settings.scheduling_mode]
    log_path = settings.state_dir / "daemon.log"
    with log_path.open("ab") as log:
        process = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=log, stderr=log,
                                   start_new_session=True)
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        if daemon_running(settings.lock_path):
            print(f"daemon running; log: {log_path}")
            return
        if process.poll() is not None:
            raise RuntimeError(f"daemon startup failed; inspect {log_path}")
        time.sleep(0.05)
    raise RuntimeError(f"daemon readiness not confirmed; inspect {log_path} before retrying")
