from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
import shlex
from typing import Any

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - exercised on Python 3.10 CI
    import tomli as tomllib


@dataclass(frozen=True)
class Settings:
    state_dir: Path
    poll_seconds: float = 10.0
    default_reserve_mb: int = 1024
    default_stable_samples: int = 3
    auto_probe_hold_seconds: float = 300.0
    auto_probe_stable_samples: int = 12
    auto_probe_stability_tolerance_mb: int = 128
    auto_probe_release_margin_mb: int = 1024
    exclusive_max_used_mb: int = 768
    exclusive_max_utilization: int = 5
    exclusive_shared_gpus: tuple[int, ...] = ()
    max_orphan_retries: int = 0
    scheduling_mode: str = "fair"
    max_running_jobs: int = 0
    default_project_limit: int = 1
    nvidia_smi: str = "nvidia-smi"
    notify_command: tuple[str, ...] | None = None
    config_path: Path | None = None

    @property
    def database_path(self) -> Path:
        return self.state_dir / "queue.sqlite3"

    @property
    def logs_dir(self) -> Path:
        return self.state_dir / "logs"

    @property
    def lock_path(self) -> Path:
        return self.state_dir / "gpuqd.lock"


def default_state_dir() -> Path:
    override = os.environ.get("GPUQ_STATE_DIR")
    if override:
        return Path(override).expanduser().resolve()
    return (Path.home() / ".local/state/gpuq").resolve()


def default_config_path() -> Path:
    override = os.environ.get("GPUQ_CONFIG")
    if override:
        return Path(override).expanduser().resolve()
    return (Path.home() / ".config/gpuq/config.toml").resolve()


def _positive_int(value: Any, name: str) -> int:
    result = int(value)
    if result < 1:
        raise ValueError(f"{name} must be positive")
    return result


def _nonnegative_int(value: Any, name: str) -> int:
    result = int(value)
    if result < 0:
        raise ValueError(f"{name} must be nonnegative")
    return result


def load_settings(
    *, state_dir: str | Path | None = None, config_path: str | Path | None = None
) -> Settings:
    path = Path(config_path).expanduser().resolve() if config_path else default_config_path()
    data: dict[str, Any] = {}
    if path.exists():
        with path.open("rb") as handle:
            loaded = tomllib.load(handle)
        data = dict(loaded.get("gpuq", loaded))
    root = (
        Path(state_dir).expanduser().resolve()
        if state_dir
        else Path(data.get("state_dir", default_state_dir())).expanduser().resolve()
    )
    poll = float(data.get("poll_seconds", 10.0))
    if poll <= 0:
        raise ValueError("poll_seconds must be positive")
    auto_probe_hold = float(data.get("auto_probe_hold_seconds", 300.0))
    if auto_probe_hold < 0:
        raise ValueError("auto_probe_hold_seconds must be nonnegative")
    notify = data.get("notify_command")
    scheduling_mode = str(data.get("scheduling_mode", "fair"))
    if scheduling_mode not in {"fair", "fifo"}:
        raise ValueError("scheduling_mode must be fair or fifo")
    notify_command: tuple[str, ...] | None
    if notify is None:
        notify_command = None
    elif isinstance(notify, str):
        notify_command = tuple(shlex.split(notify))
    else:
        notify_command = tuple(str(part) for part in notify)
    return Settings(
        state_dir=root,
        scheduling_mode=scheduling_mode,
        max_running_jobs=_nonnegative_int(data.get("max_running_jobs", 0), "max_running_jobs"),
        default_project_limit=_positive_int(
            data.get("default_project_limit", 1), "default_project_limit"
        ),
        poll_seconds=poll,
        default_reserve_mb=_nonnegative_int(
            data.get("default_reserve_mb", 1024), "default_reserve_mb"
        ),
        default_stable_samples=_positive_int(
            data.get("default_stable_samples", 3), "default_stable_samples"
        ),
        auto_probe_hold_seconds=auto_probe_hold,
        auto_probe_stable_samples=_positive_int(
            data.get("auto_probe_stable_samples", 12),
            "auto_probe_stable_samples",
        ),
        auto_probe_stability_tolerance_mb=_nonnegative_int(
            data.get("auto_probe_stability_tolerance_mb", 128),
            "auto_probe_stability_tolerance_mb",
        ),
        auto_probe_release_margin_mb=_nonnegative_int(
            data.get("auto_probe_release_margin_mb", 1024),
            "auto_probe_release_margin_mb",
        ),
        exclusive_max_used_mb=int(data.get("exclusive_max_used_mb", 768)),
        max_orphan_retries=_nonnegative_int(
            data.get("max_orphan_retries", 0), "max_orphan_retries"
        ),
        exclusive_max_utilization=int(data.get("exclusive_max_utilization", 5)),
        exclusive_shared_gpus=tuple(
            _nonnegative_int(index, "exclusive_shared_gpus")
            for index in data.get("exclusive_shared_gpus", [])
        ),
        nvidia_smi=str(data.get("nvidia_smi", "nvidia-smi")),
        notify_command=notify_command,
        config_path=path if path.exists() or config_path is not None else None,
    )


def ensure_state_dirs(settings: Settings) -> None:
    settings.state_dir.mkdir(parents=True, exist_ok=True)
    settings.logs_dir.mkdir(parents=True, exist_ok=True)
