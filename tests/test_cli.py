from pathlib import Path
import os

import pytest

from gpuq.cli import (
    _job_page,
    _parse_env,
    _parse_gpus,
    _read_pager_key,
    _tail_lines,
    build_parser,
    main,
)
from gpuq import __version__
from gpuq.config import load_settings, tomllib
from gpuq.db import QueueDB

from conftest import submit_job


@pytest.mark.parametrize("command", ["logs", "log"])
@pytest.mark.parametrize("option", ["-n", "--lines", "--tail"])
@pytest.mark.parametrize("count", [0, 8])
def test_log_tail_argument_aliases(command: str, option: str, count: int) -> None:
    args = build_parser().parse_args([command, "21", option, str(count), "-f"])
    assert args.job_id == 21
    assert args.lines == count
    assert args.follow


@pytest.mark.parametrize("count", ["-1", "1.5", "all"])
def test_log_tail_rejects_invalid_count(count: str) -> None:
    with pytest.raises(SystemExit) as error:
        build_parser().parse_args(["logs", "21", "--tail", count])
    assert error.value.code == 2


def test_log_tail_defaults_to_100_lines() -> None:
    assert build_parser().parse_args(["logs", "21"]).lines == 100


@pytest.mark.parametrize(
    "content,count,expected",
    [
        (b"one\ntwo\nthree\n", 2, "two\nthree"),
        (b"one\ntwo\nthree", 2, "two\nthree"),
        (b"one\r\ntwo\rthree\n", 2, "two\nthree"),
        (b"one\n\n", 2, "one\n"),
        ("第一行\n第二行\n".encode(), 1, "第二行"),
        (b"one\n\xff\n", 1, "\ufffd"),
        (b"one\n", 10, "one"),
        (b"one\n", 0, ""),
        (b"", 2, ""),
    ],
)
def test_tail_lines_handles_boundaries(
    tmp_path: Path, content: bytes, count: int, expected: str
) -> None:
    path = tmp_path / "job.log"
    path.write_bytes(content)
    assert _tail_lines(path, count) == expected


def test_tail_lines_handles_missing_file_and_invalid_count(tmp_path: Path) -> None:
    path = tmp_path / "missing.log"
    assert _tail_lines(path, 8) == ""
    with pytest.raises(ValueError, match="non-negative"):
        _tail_lines(path, -1)


def test_tail_lines_streams_instead_of_loading_entire_log(tmp_path: Path, monkeypatch) -> None:
    path = tmp_path / "job.log"
    with path.open("w") as handle:
        for index in range(10_000):
            handle.write(f"line {index}\n")

    def reject_read_text(*args, **kwargs):
        pytest.fail("tail must not load the complete log with read_text")

    monkeypatch.setattr(Path, "read_text", reject_read_text)
    assert _tail_lines(path, 2) == "line 9998\nline 9999"


@pytest.mark.parametrize("count,expected", [(2, "two\nthree\n"), (0, "")])
def test_log_tail_outputs_requested_lines(
    db: QueueDB, settings, tmp_path: Path, monkeypatch, capsys, count: int, expected: str
) -> None:
    job = submit_job(db, tmp_path)
    Path(job.log_path).write_text("one\ntwo\nthree\n", encoding="utf-8")
    monkeypatch.setattr("gpuq.cli._db", lambda args: (settings, db))
    with pytest.raises(SystemExit) as error:
        main(["log", str(job.id), "--tail", str(count)])
    assert error.value.code == 0
    assert capsys.readouterr().out == expected


def test_log_tail_zero_follow_only_prints_new_content(
    db: QueueDB, settings, tmp_path: Path, monkeypatch, capsys
) -> None:
    job = submit_job(db, tmp_path)
    path = Path(job.log_path)
    path.write_text("old content\n", encoding="utf-8")
    monkeypatch.setattr("gpuq.cli._db", lambda args: (settings, db))

    def append_then_interrupt(_seconds):
        if "new content" in path.read_text(encoding="utf-8"):
            raise KeyboardInterrupt
        with path.open("a", encoding="utf-8") as handle:
            handle.write("new content\n")

    monkeypatch.setattr("gpuq.cli.time.sleep", append_then_interrupt)
    with pytest.raises(SystemExit) as error:
        main(["logs", str(job.id), "--tail", "0", "-f"])
    assert error.value.code == 130
    assert capsys.readouterr().out == "new content\n"


def test_resource_argument_aliases_and_command_remainder() -> None:
    parser = build_parser()
    args = parser.parse_args(
        [
            "submit",
            "--memory",
            "4096",
            "--reserve",
            "512",
            "--gpu",
            "1,2",
            "--env",
            "A=B",
            "--",
            "python",
            "train.py",
            "--epochs",
            "3",
        ]
    )
    assert args.memory_mb == 4096
    assert args.reserve_mb == 512
    assert args.command[-2:] == ["--epochs", "3"]
    assert _parse_gpus(args.gpu) == (1, 2)
    assert _parse_env(args.env) == {"A": "B"}


def test_submit_defaults_to_auto_memory() -> None:
    parser = build_parser()
    args = parser.parse_args(["submit", "--", "python", "train.py"])
    assert args.memory_mb == "auto"


def test_version_flag(capsys) -> None:
    parser = build_parser()
    with pytest.raises(SystemExit) as error:
        parser.parse_args(["--version"])
    assert error.value.code == 0
    assert capsys.readouterr().out.strip() == f"gpuq {__version__}"
    with (Path(__file__).resolve().parents[1] / "pyproject.toml").open("rb") as source:
        assert tomllib.load(source)["project"]["version"] == __version__


def test_default_auto_submit_is_persisted(tmp_path: Path) -> None:
    state = tmp_path / "state"
    try:
        main(
            [
                "--state-dir",
                str(state),
                "submit",
                "--no-notify",
                "--",
                "python",
                "train.py",
            ]
        )
    except SystemExit as error:
        assert error.code == 0
    job = QueueDB(state / "queue.sqlite3").list()[0]
    assert job.is_auto_memory
    assert job.memory_mb == 1
    assert job.retry_on_oom


def test_explicit_auto_memory_and_list_pager_arguments() -> None:
    parser = build_parser()
    submit = parser.parse_args(["submit", "--memory", "auto", "--", "python", "x.py"])
    assert submit.memory_mb == "auto"
    listing = parser.parse_args(["list", "--page-size", "20", "--no-pager"])
    assert listing.page_size == 20
    assert listing.no_pager


def test_list_pager_defaults_to_twenty_rows() -> None:
    parser = build_parser()
    listing = parser.parse_args(["list"])
    assert listing.page_size == 20


def test_job_page_shows_twenty_rows_and_navigation_hint(
    db: QueueDB, tmp_path: Path
) -> None:
    jobs = [submit_job(db, tmp_path, name=f"job-{index}") for index in range(22)]
    first = _job_page(jobs, 0, 20)
    second = _job_page(jobs, 1, 20)
    assert "job-0" in first and "job-19" in first
    assert "job-20" not in first
    assert "第 1/2 页" in first
    assert "job-20" in second and "job-21" in second
    assert "←/→ 翻页" in second


def test_pager_reads_arrow_keys_and_quit() -> None:
    reader, writer = os.pipe()
    try:
        os.write(writer, b"\x1b[C")
        assert _read_pager_key(reader) == "right"
        os.write(writer, b"q")
        assert _read_pager_key(reader) == "quit"
    finally:
        os.close(reader)
        os.close(writer)


def test_init_writes_parseable_config_and_allows_zero_reserve(
    tmp_path: Path, monkeypatch
) -> None:
    state = tmp_path / "state"
    config = tmp_path / "config.toml"
    with monkeypatch.context() as scope:
        scope.setattr(
            "sys.argv",
            [
                "gpuq",
                "--state-dir",
                str(state),
                "--config",
                str(config),
                "init",
            ],
        )
        try:
            main()
        except SystemExit as error:
            assert error.code == 0
    text = config.read_text(encoding="utf-8").replace(
        "default_reserve_mb = 1024", "default_reserve_mb = 0"
    )
    config.write_text(text, encoding="utf-8")
    settings = load_settings(config_path=config)
    assert settings.default_reserve_mb == 0
    assert settings.auto_probe_hold_seconds == 300
    assert settings.auto_probe_stable_samples == 12
    assert settings.auto_probe_stability_tolerance_mb == 128
    assert settings.auto_probe_release_margin_mb == 1024


def test_show_and_list_localize_time_without_mutating_database(db, settings, tmp_path, monkeypatch, capsys):
    import json
    import time
    from gpuq.cli import command_list, command_show
    from gpuq.db import utcnow
    from datetime import datetime, timedelta

    job = submit_job(db, tmp_path)
    original_tz = os.environ.get("TZ")
    try:
        monkeypatch.setenv("TZ", "Asia/Shanghai")
        time.tzset()
        assert datetime.fromisoformat(utcnow()).utcoffset() == timedelta(0)
        stored = "2026-11-01T20:30:00+00:00"
        with db.connect() as connection:
            connection.execute("UPDATE jobs SET created_at=?, updated_at=?, started_at=? WHERE id=?",
                               (stored, stored, "2026-11-01T15:30:00-05:00", job.id))
        monkeypatch.setattr("gpuq.cli._db", lambda args: (settings, db))
        command_show(build_parser().parse_args(["show", str(job.id)]))
        output = capsys.readouterr().out
        assert output.count("2026-11-02T04:30:00+08:00") == 3
        assert "finished_at" in output and "None" in output
        command_show(build_parser().parse_args(["show", str(job.id), "--json"]))
        decoded = json.loads(capsys.readouterr().out)
        assert decoded["created_at"] == "2026-11-02T04:30:00+08:00"
        assert decoded["started_at"] == "2026-11-02T04:30:00+08:00"
        assert decoded["finished_at"] is None
        command_list(build_parser().parse_args(["list", "--json"]))
        assert json.loads(capsys.readouterr().out)[0] == decoded
        assert db.get(job.id).created_at == stored
        assert db.get(job.id).started_at == "2026-11-01T15:30:00-05:00"
    finally:
        if original_tz is None:
            os.environ.pop("TZ", None)
        else:
            os.environ["TZ"] = original_tz
        time.tzset()


@pytest.mark.parametrize("value", [None, "", "unknown", "2026-11-02T04:30:00"])
def test_display_timestamp_preserves_unknown_timezone(value):
    from gpuq.cli import _display_timestamp
    assert _display_timestamp(value) == value
