from __future__ import annotations

import argparse
from collections import deque
from datetime import datetime
import json
import math
import os
from pathlib import Path
import re
import select
import shlex
import sys
import termios
import time
import tty
import uuid

from . import __version__
from .config import Settings, ensure_state_dirs, load_settings
from .db import QueueDB
from .gpu import NvidiaSMIBackend
from .models import Job, JobStatus
from .scheduler import cancel_job, run_daemon


def _parse_env(values: list[str]) -> dict[str, str]:
    result: dict[str, str] = {}
    for value in values:
        if "=" not in value:
            raise ValueError(f"environment must be KEY=VALUE: {value!r}")
        key, content = value.split("=", 1)
        if not key or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key):
            raise ValueError(f"invalid environment key: {key!r}")
        result[key] = content
    return result


def _parse_gpus(values: list[str] | None) -> tuple[int, ...] | None:
    if not values:
        return None
    result: list[int] = []
    for value in values:
        for item in value.split(","):
            index = int(item.strip())
            if index < 0:
                raise ValueError("GPU indices cannot be negative")
            if index not in result:
                result.append(index)
    return tuple(result)


def _parse_memory(value: str) -> int | str:
    if value.strip().lower() == "auto":
        return "auto"
    try:
        memory_mb = int(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError("memory must be a positive MB value or 'auto'") from error
    if memory_mb < 1:
        raise argparse.ArgumentTypeError("memory must be a positive MB value or 'auto'")
    return memory_mb


def _positive_int(value: str) -> int:
    try:
        result = int(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError("value must be a positive integer") from error
    if result < 1:
        raise argparse.ArgumentTypeError("value must be a positive integer")
    return result


def _nonnegative_int(value: str) -> int:
    try:
        result = int(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError("value must be a non-negative integer") from error
    if result < 0:
        raise argparse.ArgumentTypeError("value must be a non-negative integer")
    return result


def _slug(value: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9_.-]+", "-", value).strip("-.")
    return cleaned[:60] or "job"


def _settings(args: argparse.Namespace) -> Settings:
    return load_settings(state_dir=args.state_dir, config_path=args.config)


def _db(args: argparse.Namespace) -> tuple[Settings, QueueDB]:
    settings = _settings(args)
    ensure_state_dirs(settings)
    return settings, QueueDB(settings.database_path)


def _job_line(job: Job) -> str:
    gpu = ",".join(map(str, job.assigned_gpus)) or "-"
    if job.is_auto_memory:
        estimate = "probe" if job.oom_count == 0 else f">={job.memory_mb}"
        resources = f"auto:{estimate}+{job.reserve_mb}MB"
        attempts = f"{job.attempt}/∞"
    else:
        resources = f"{job.memory_mb}+{job.reserve_mb}MB"
        attempts = f"{job.attempt}/{job.max_retries + 1}"
    if job.gpu_count > 1:
        resources = f"{job.gpu_count}x{resources}"
    return (
        f"{job.id:5d}  {job.status:18s}  p={job.priority:4d}  gpu={gpu:>2s}  "
        f"mem={resources:>22s}  try={attempts:>7s}  {job.name}"
    )


def _list_header() -> str:
    return "   ID  STATUS              PRIORITY/GPU/MEMORY                         NAME"


def _job_page(jobs: list[Job], page: int, page_size: int) -> str:
    pages = max(1, math.ceil(len(jobs) / page_size))
    page = min(max(0, page), pages - 1)
    start = page * page_size
    lines = [_list_header(), *(_job_line(job) for job in jobs[start : start + page_size])]
    lines.extend(
        [
            "",
            f"第 {page + 1}/{pages} 页（每页 {page_size} 条）  ←/→ 翻页，q 或 Esc 退出",
        ]
    )
    return "\n".join(lines)


def _read_pager_key(file_descriptor: int) -> str:
    first = os.read(file_descriptor, 1)
    if first in {b"q", b"Q"}:
        return "quit"
    if first != b"\x1b":
        return "other"
    ready, _, _ = select.select([file_descriptor], [], [], 0.05)
    if not ready:
        return "quit"
    second = os.read(file_descriptor, 1)
    if second != b"[":
        return "quit"
    ready, _, _ = select.select([file_descriptor], [], [], 0.05)
    if not ready:
        return "quit"
    third = os.read(file_descriptor, 1)
    if third == b"D":
        return "left"
    if third == b"C":
        return "right"
    return "other"


def _interactive_job_list(jobs: list[Job], page_size: int) -> None:
    file_descriptor = sys.stdin.fileno()
    original = termios.tcgetattr(file_descriptor)
    page = 0
    pages = max(1, math.ceil(len(jobs) / page_size))
    try:
        tty.setcbreak(file_descriptor)
        # Use the terminal's alternate screen so paging never pollutes the
        # caller's scrollback.  Restore both screen and cursor in ``finally``.
        sys.stdout.write("\x1b[?1049h\x1b[?25l")
        while True:
            sys.stdout.write("\x1b[2J\x1b[H" + _job_page(jobs, page, page_size))
            sys.stdout.flush()
            key = _read_pager_key(file_descriptor)
            if key == "quit":
                break
            if key == "left":
                page = max(0, page - 1)
            elif key == "right":
                page = min(pages - 1, page + 1)
    finally:
        termios.tcsetattr(file_descriptor, termios.TCSADRAIN, original)
        sys.stdout.write("\x1b[?25h\x1b[?1049l")
        sys.stdout.flush()


def command_init(args: argparse.Namespace) -> int:
    settings, _ = _db(args)
    config_path = Path(args.config).expanduser().resolve() if args.config else (
        Path.home() / ".config/gpuq/config.toml"
    )
    if not config_path.exists():
        config_path.parent.mkdir(parents=True, exist_ok=True)
        config_path.write_text(
            "[gpuq]\n"
            f"state_dir = {json.dumps(str(settings.state_dir))}\n"
            "poll_seconds = 10\n"
            "default_reserve_mb = 1024\n"
            "default_stable_samples = 3\n"
            "auto_probe_hold_seconds = 300\n"
            "auto_probe_stable_samples = 12\n"
            "auto_probe_stability_tolerance_mb = 128\n"
            "auto_probe_release_margin_mb = 1024\n"
            "exclusive_max_used_mb = 768\n"
            "exclusive_max_utilization = 5\n"
            "# notify_command = [\"notify-send\", \"{title}\", \"{message}\"]\n",
            encoding="utf-8",
        )
        print(f"created {config_path}")
    else:
        print(f"kept existing {config_path}")
    print(f"state: {settings.state_dir}")
    print(f"database: {settings.database_path}")
    return 0


def command_submit(args: argparse.Namespace) -> int:
    settings, db = _db(args)
    command = list(args.command)
    if command and command[0] == "--":
        command.pop(0)
    if not command:
        raise ValueError("submit needs a command after --")
    cwd = Path(args.cwd or os.getcwd()).expanduser().resolve()
    if not cwd.is_dir():
        raise ValueError(f"working directory does not exist: {cwd}")
    name = args.name or Path(command[0]).name
    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    log_path = (
        Path(args.log).expanduser().resolve()
        if args.log
        else settings.logs_dir / f"{timestamp}-{_slug(name)}-{uuid.uuid4().hex[:8]}.log"
    )
    reserve = settings.default_reserve_mb if args.reserve_mb is None else args.reserve_mb
    stable = settings.default_stable_samples if args.stable_samples is None else args.stable_samples
    memory_mode = "auto" if args.memory_mb == "auto" else "fixed"
    memory_mb = 1 if memory_mode == "auto" else int(args.memory_mb)
    job = db.submit(
        name=name,
        command=command,
        cwd=str(cwd),
        env=_parse_env(args.env),
        gpu_allowlist=_parse_gpus(args.gpu),
        gpu_count=args.gpu_count,
        memory_mb=memory_mb,
        memory_mode=memory_mode,
        reserve_mb=reserve,
        max_utilization=args.max_utilization,
        stable_samples=stable,
        priority=args.priority,
        exclusive=args.exclusive,
        dependency_id=args.after,
        max_retries=args.max_retries,
        retry_on_oom=(True if memory_mode == "auto" else args.retry_on_oom),
        log_path=str(log_path),
        notify=not args.no_notify,
    )
    print(f"submitted job {job.id}: {job.name}")
    print(f"command: {shlex.join(job.command)}")
    if job.gpu_count > 1:
        print(f"GPUs: {job.gpu_count}; memory and reserve below are per GPU")
    if job.is_auto_memory:
        print(
            f"requested: auto probe + {job.reserve_mb}MB reserve; "
            "only recognized OOMs learn and retry without a count limit"
        )
    else:
        print(f"requested: {job.memory_mb}MB + {job.reserve_mb}MB reserve")
    print(f"log: {job.log_path}")
    return 0


def command_list(args: argparse.Namespace) -> int:
    _, db = _db(args)
    jobs = db.list(statuses=args.status, limit=args.limit)
    if args.json:
        print(json.dumps([job.as_dict() for job in jobs], ensure_ascii=False, indent=2))
        return 0
    if not jobs:
        print("queue is empty")
        return 0
    if (
        not args.no_pager
        and len(jobs) > args.page_size
        and sys.stdin.isatty()
        and sys.stdout.isatty()
    ):
        _interactive_job_list(jobs, args.page_size)
        return 0
    print(_list_header())
    for job in jobs:
        print(_job_line(job))
    return 0


def command_show(args: argparse.Namespace) -> int:
    _, db = _db(args)
    job = db.get(args.job_id)
    if args.json:
        print(json.dumps(job.as_dict(), ensure_ascii=False, indent=2))
        return 0
    for key, value in job.as_dict().items():
        if key == "command":
            value = shlex.join(value)
        print(f"{key:20s}: {value}")
    return 0


def command_cancel(args: argparse.Namespace) -> int:
    _, db = _db(args)
    job = cancel_job(db, args.job_id, force_after=args.force_after)
    print(f"job {job.id}: cancel requested; current status={db.get(job.id).status}")
    return 0


def command_retry(args: argparse.Namespace) -> int:
    _, db = _db(args)
    memory_mode = None
    memory_mb = None
    if args.memory_mb == "auto":
        memory_mode = "auto"
        memory_mb = 1
    elif args.memory_mb is not None:
        memory_mode = "fixed"
        memory_mb = int(args.memory_mb)
    job = db.retry(
        args.job_id,
        memory_mb=memory_mb,
        memory_mode=memory_mode,
        reset_attempts=args.reset_attempts,
    )
    memory_text = "auto" if job.is_auto_memory else f"{job.memory_mb}MB"
    print(f"job {job.id} requeued with memory={memory_text}")
    return 0


def command_priority(args: argparse.Namespace) -> int:
    _, db = _db(args)
    job = db.set_priority(args.job_id, args.value)
    print(f"job {job.id} priority={job.priority}")
    return 0


def _tail_lines(path: Path, lines: int) -> str:
    if lines < 0:
        raise ValueError("lines must be a non-negative integer")
    if lines == 0 or not path.exists():
        return ""
    with path.open("r", encoding="utf-8", errors="replace") as handle:
        content = deque(handle, maxlen=lines)
    return "\n".join(line.rstrip("\r\n") for line in content)


def command_logs(args: argparse.Namespace) -> int:
    _, db = _db(args)
    job = db.get(args.job_id)
    path = Path(job.log_path)
    initial = _tail_lines(path, args.lines)
    if initial:
        print(initial)
    if not args.follow:
        return 0
    position = path.stat().st_size if path.exists() else 0
    try:
        while True:
            if path.exists():
                with path.open("r", encoding="utf-8", errors="replace") as handle:
                    handle.seek(position)
                    chunk = handle.read()
                    position = handle.tell()
                if chunk:
                    print(chunk, end="", flush=True)
            current = db.get(args.job_id)
            if current.status in {
                JobStatus.COMPLETED.value,
                JobStatus.FAILED.value,
                JobStatus.CANCELLED.value,
                JobStatus.BLOCKED_DEPENDENCY.value,
            }:
                break
            time.sleep(1.0)
    except KeyboardInterrupt:
        return 130
    return 0


def command_gpus(args: argparse.Namespace) -> int:
    settings = _settings(args)
    states = NvidiaSMIBackend(settings.nvidia_smi).sample()
    if args.json:
        print(
            json.dumps(
                [
                    {
                        "index": state.index,
                        "uuid": state.uuid,
                        "name": state.name,
                        "total_memory_mb": state.total_memory_mb,
                        "used_memory_mb": state.used_memory_mb,
                        "free_memory_mb": state.free_memory_mb,
                        "utilization_percent": state.utilization_percent,
                        "temperature_c": state.temperature_c,
                        "processes": [process.__dict__ for process in state.processes],
                    }
                    for state in states
                ],
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0
    print("GPU  FREE/TOTAL(MB)  UTIL  TEMP  COMPUTE PROCESSES  NAME")
    for state in states:
        process_text = ",".join(
            f"{process.pid}:{process.used_memory_mb}MB" for process in state.processes
        ) or "-"
        print(
            f"{state.index:3d}  {state.free_memory_mb:5d}/{state.total_memory_mb:<5d}  "
            f"{state.utilization_percent:3d}%  {state.temperature_c:3d}C  "
            f"{process_text:20.20s}  {state.name}"
        )
    return 0


def command_daemon(args: argparse.Namespace) -> int:
    settings = _settings(args)
    if args.poll_seconds is not None:
        settings = Settings(
            state_dir=settings.state_dir,
            poll_seconds=args.poll_seconds,
            default_reserve_mb=settings.default_reserve_mb,
            default_stable_samples=settings.default_stable_samples,
            auto_probe_hold_seconds=settings.auto_probe_hold_seconds,
            auto_probe_stable_samples=settings.auto_probe_stable_samples,
            auto_probe_stability_tolerance_mb=(settings.auto_probe_stability_tolerance_mb),
            auto_probe_release_margin_mb=settings.auto_probe_release_margin_mb,
            exclusive_max_used_mb=settings.exclusive_max_used_mb,
            exclusive_max_utilization=settings.exclusive_max_utilization,
            nvidia_smi=settings.nvidia_smi,
            notify_command=settings.notify_command,
            config_path=settings.config_path,
        )
    run_daemon(settings, once=args.once, dry_run=args.dry_run)
    return 0


def command_doctor(args: argparse.Namespace) -> int:
    settings, db = _db(args)
    print(f"state_dir: {settings.state_dir} [ok]")
    print(f"database: {db.path} [ok]")
    try:
        states = NvidiaSMIBackend(settings.nvidia_smi).sample()
    except Exception as error:
        print(f"GPU monitor: FAILED: {type(error).__name__}: {error}")
        return 1
    print(f"GPU monitor: {len(states)} device(s) [ok]")
    print(f"database jobs: {len(db.list(limit=10000))}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="gpuq",
        description="非托管多人 GPU 服务器上的个人机会式任务队列",
    )
    parser.add_argument("--state-dir", help="覆盖状态目录，默认 ~/.local/state/gpuq")
    parser.add_argument("--config", help="覆盖配置文件，默认 ~/.config/gpuq/config.toml")
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    sub = parser.add_subparsers(dest="subcommand", required=True)

    init = sub.add_parser("init", help="初始化 SQLite 状态目录和示例配置")
    init.set_defaults(function=command_init)

    submit = sub.add_parser("submit", help="提交一个等待 GPU 资源的任务")
    submit.add_argument("--name", help="任务显示名称；默认取可执行文件名")
    submit.add_argument(
        "--memory-mb",
        "--memory",
        type=_parse_memory,
        default="auto",
        help="每张 GPU 的预计峰值显存（MB），或 auto；默认 auto，仅在 OOM 时学习重试",
    )
    submit.add_argument("--reserve-mb", "--reserve", type=int, help="每张 GPU 的额外安全余量；默认读取配置")
    submit.add_argument("--gpu", action="append", help="允许的物理 GPU，例如 --gpu 0,1；可重复")
    submit.add_argument(
        "--gpus", "--gpu-count", dest="gpu_count", type=_positive_int, default=1,
        help="同一任务同时使用的 GPU 数量；默认 1，全部满足条件后一起启动",
    )
    submit.add_argument(
        "--max-utilization",
        type=int,
        help="启动时允许的最大 GPU 利用率 0–100；省略则不设硬上限，选卡仍优先低利用率",
    )
    submit.add_argument("--stable-samples", type=int, help="必须连续满足条件的采样次数")
    submit.add_argument("--priority", type=int, default=0, help="优先级；数值越大越先调度")
    submit.add_argument("--exclusive", action="store_true", help="要求 GPU 无 compute 进程且接近空闲")
    submit.add_argument("--cwd", help="任务工作目录；默认当前目录")
    submit.add_argument("--env", action="append", default=[], metavar="KEY=VALUE", help="附加环境变量；可重复")
    submit.add_argument("--after", type=int, help="仅在指定任务成功后运行")
    submit.add_argument(
        "--max-retries",
        type=int,
        default=0,
        help="固定显存模式下，初次运行后的 OOM 自动重试次数；auto 模式忽略",
    )
    submit.add_argument(
        "--retry-on-oom",
        action="store_true",
        help="固定显存模式下检测到 OOM 时提高估计并重排；auto 模式始终启用",
    )
    submit.add_argument("--log", help="日志路径；默认位于状态目录 logs/")
    submit.add_argument("--no-notify", action="store_true", help="禁止本任务完成/失败通知")
    submit.add_argument("command", nargs=argparse.REMAINDER, help="在 -- 之后给出原始命令和参数")
    submit.set_defaults(function=command_submit)

    listing = sub.add_parser("list", aliases=["ls"], help="查看队列和运行历史")
    listing.add_argument("--status", action="append", help="按状态过滤；可重复")
    listing.add_argument("--limit", type=int, default=200)
    listing.add_argument("--json", action="store_true")
    listing.add_argument(
        "--page-size",
        type=_positive_int,
        default=20,
        help="交互列表每页条数；默认 20",
    )
    listing.add_argument("--no-pager", action="store_true", help="即使在终端中也禁用交互翻页")
    listing.set_defaults(function=command_list)

    show = sub.add_parser("show", help="查看一个任务的完整字段")
    show.add_argument("job_id", type=int)
    show.add_argument("--json", action="store_true")
    show.set_defaults(function=command_show)

    cancel = sub.add_parser("cancel", help="取消排队或运行中的任务")
    cancel.add_argument("job_id", type=int)
    cancel.add_argument("--force-after", type=float, default=10.0, help="SIGTERM 后多少秒升级为 SIGKILL")
    cancel.set_defaults(function=command_cancel)

    retry = sub.add_parser("retry", help="手动重新排队一个终态任务")
    retry.add_argument("job_id", type=int)
    retry.add_argument("--memory-mb", "--memory", type=_parse_memory, help="覆盖显存估计，或切换为 auto")
    retry.add_argument("--reset-attempts", action="store_true", help="将 attempt 计数清零")
    retry.set_defaults(function=command_retry)

    priority = sub.add_parser("priority", help="修改任务优先级")
    priority.add_argument("job_id", type=int)
    priority.add_argument("value", type=int)
    priority.set_defaults(function=command_priority)

    logs = sub.add_parser("logs", aliases=["log"], help="查看或跟踪任务 stdout/stderr")
    logs.add_argument("job_id", type=int)
    logs.add_argument("-f", "--follow", action="store_true")
    logs.add_argument(
        "-n", "--lines", "--tail", dest="lines", type=_nonnegative_int, default=100,
        metavar="N", help="首次显示最后 N 行；默认 100，0 表示不显示已有日志",
    )
    logs.set_defaults(function=command_logs)

    gpus = sub.add_parser("gpus", help="显示调度器看到的 GPU 状态")
    gpus.add_argument("--json", action="store_true")
    gpus.set_defaults(function=command_gpus)

    daemon = sub.add_parser("daemon", help="启动常驻调度循环")
    daemon.add_argument("--poll-seconds", type=float, help="覆盖监控间隔")
    daemon.add_argument("--once", action="store_true", help="只执行一次调度 tick")
    daemon.add_argument("--dry-run", action="store_true", help="计算可运行任务但不启动")
    daemon.set_defaults(function=command_daemon)

    doctor = sub.add_parser("doctor", help="检查数据库、状态目录和 GPU 监控")
    doctor.set_defaults(function=command_doctor)
    return parser


def main(argv: list[str] | None = None) -> None:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        code = int(args.function(args))
    except (ValueError, KeyError, RuntimeError) as error:
        parser.error(str(error))
        return
    raise SystemExit(code)


def daemon_entrypoint() -> None:
    main(["daemon", *sys.argv[1:]])
