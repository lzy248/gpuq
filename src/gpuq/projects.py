"""Stable project identity for local directories and uploaded snapshots."""
from pathlib import Path


def project_root(cwd: str | Path) -> Path:
    """Resolve cwd to its nearest Git ancestor, else package marker, else itself.

    Git markers take precedence over package markers, including worktree .git
    files. This is directory identity, not repository validation or a remote URL.
    Submit and migration persist the result; retries do not rediscover identity.
    """
    path = Path(cwd).expanduser().resolve()
    for candidate in (path, *path.parents):
        if (candidate / ".git").exists():
            return candidate
    for candidate in (path, *path.parents):
        if any((candidate / marker).is_file() for marker in ("pyproject.toml", "package.json")):
            return candidate
    return path
