"""Reusable remote virtualenvs keyed by a self-contained hashed requirements lock."""
from __future__ import annotations

import fcntl
import hashlib
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys

from .sync import safe_relative


def prepare_environment(workspace: Path, snapshot: Path, requirements: str, python: str) -> Path:
    """Return a reusable remote venv for a self-contained hashed pip lock.

    Identity combines lock bytes with the remote interpreter version, real path
    and machine architecture. A per-identity flock serializes creation; .gpuq-ready
    commits successful installation. A changed lock creates a different environment.
    Preparation runs before enqueueing and may download packages; subprocess errors
    propagate after removing incomplete environments. Existing ready environments
    are trusted, not re-audited or made immutable. Drivers/system libraries are not
    managed here; python names the remote interpreter, never a copied local venv.
    """
    safe_relative(requirements)
    lock = snapshot / requirements
    if not lock.resolve().is_relative_to(snapshot) or not lock.is_file():
        raise ValueError("requirements lock must be a synced file inside the project")
    content = lock.read_bytes()
    # Includes/editables make one lock file insufficient as an environment identity.
    if re.search(rb"(?m)^\s*(?:-r|-c|-e|--requirement|--constraint|--editable)(?:\s|=|\S)", content):
        raise ValueError("remote requirements must be a self-contained lock without includes/editables")
    python = os.path.expanduser(python)
    identity = subprocess.check_output([
        python, "-c", "import sys,os,platform; print(sys.version, os.path.realpath(sys.executable), platform.machine())",
    ])
    key = hashlib.sha256(identity + b"\0" + content).hexdigest()
    parent = workspace / "environments"
    parent.mkdir(parents=True, exist_ok=True)
    environment = parent / key
    with (parent / f"{key}.lock").open("a") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        if not (environment / ".gpuq-ready").exists():
            if environment.exists():
                shutil.rmtree(environment)
            try:
                subprocess.run([python, "-m", "venv", str(environment)], check=True,
                               stdout=sys.stderr, stderr=sys.stderr)
                env = os.environ.copy()
                env["PIP_CACHE_DIR"] = str(workspace / "cache" / "pip")
                subprocess.run([
                    str(environment / "bin/python"), "-m", "pip", "install", "--require-hashes",
                    "--disable-pip-version-check", "--no-input", "-r", str(lock),
                ], cwd=snapshot, env=env, check=True, stdout=sys.stderr, stderr=sys.stderr)
                (environment / ".gpuq-ready").write_bytes(content)
            except BaseException:
                shutil.rmtree(environment, ignore_errors=True)
                raise
    return environment
