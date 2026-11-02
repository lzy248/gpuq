"""OpenSSH transport for the same gpuq CLI, with isolated project snapshots."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import socket
import subprocess
import sys
import tempfile

from .config import default_config_path, tomllib
from .projects import project_root
from .sync import (manifest_for, materialize, missing_blobs, receive_blobs, snapshot_files,
                   write_blobs)


PROTOCOL_VERSION = 2
HEADER_LIMIT = 16 * 1024 * 1024
REMOTE_FLAG = "--gpuq-remote-call"


def remote_profile(args: argparse.Namespace) -> dict:
    """Resolve a local TOML profile or literal SSH host and apply CLI overrides.

    Reads configuration only; does not connect or open a queue. ``config`` and
    ``state_dir`` in the returned profile name remote paths, while ``identity``
    names a local key. Invalid profile shapes/host/port/command raise ValueError.
    """
    config_value = args.config or os.environ.get("GPUQ_CONFIG")
    config = Path(config_value).expanduser() if config_value else default_config_path()
    profiles = {}
    if config.exists():
        with config.open("rb") as source:
            profiles = tomllib.load(source).get("remotes", {})
    if not isinstance(profiles, dict) or not isinstance(profiles.get(args.remote, {}), dict):
        raise ValueError("remotes and remote profiles must be TOML tables")
    profile = dict(profiles.get(args.remote, {}))
    for field in ("identity", "config", "state_dir", "work_dir", "cwd", "python", "requirements"):
        if field in profile and (not isinstance(profile[field], str) or not profile[field]):
            raise ValueError(f"remote {field} must be a nonempty string")
    profile.setdefault("host", args.remote)
    for name, value in (("port", args.ssh_port), ("identity", args.ssh_identity),
                        ("config", args.remote_config), ("work_dir", args.remote_workdir)):
        if value is not None:
            profile[name] = value
    if args.remote_command:
        profile["command"] = shlex.split(args.remote_command)
    host = profile["host"]
    if not isinstance(host, str) or not re.fullmatch(r"[A-Za-z0-9_.@:\[\]-]+", host) or host.startswith("-"):
        raise ValueError("remote host must be an SSH alias, IP, or user@host")
    port = profile.get("port")
    if port is not None and (not isinstance(port, int) or not 1 <= port <= 65535):
        raise ValueError("SSH port must be in [1, 65535]")
    command = profile.get("command")
    if isinstance(command, str):
        profile["command"] = shlex.split(command)
    if command is not None and (not isinstance(profile["command"], list) or not profile["command"] or not all(
        isinstance(part, str) and part for part in profile["command"]
    )):
        raise ValueError("remote command must be a nonempty argv array or command string")
    return profile


def ssh_command(profile: dict) -> list[str]:
    arguments = ["ssh", "-T", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10",
                 "-o", "ServerAliveInterval=15", "-o", "ServerAliveCountMax=3"]
    if profile.get("port") is not None:
        arguments.extend(["-p", str(profile["port"])])
    if profile.get("identity"):
        arguments.extend(["-i", str(Path(profile["identity"]).expanduser())])
    command = profile.get("command")
    if command:
        quoted = [shlex.quote(part) for part in command]
        if command[0].startswith("~/"):
            quoted[0] = '"$HOME"/' + shlex.quote(command[0][2:])
        remote = "exec " + " ".join([*quoted, REMOTE_FLAG])
    else:
        remote = (f"if command -v gpuq >/dev/null 2>&1; then exec gpuq {REMOTE_FLAG}; "
                  f'else exec "$HOME/.local/bin/gpuq" {REMOTE_FLAG}; fi')
    return [*arguments, "--", profile["host"], remote]


def strip_options(tokens: list[str], options: dict[str, bool]) -> list[str]:
    """Remove named parser options only; never inspect command remainder tokens."""
    result = []
    index = 0
    while index < len(tokens):
        token = tokens[index]
        name = token.split("=", 1)[0]
        if name in options:
            index += 1 + int(options[name] and "=" not in token)
        else:
            result.append(token)
            index += 1
    return result


def request_for(args: argparse.Namespace, argv: list[str], profile: dict) -> tuple[dict, Path | None]:
    """Build the wire request and return the local snapshot root, if syncing.

    Requires argv already accepted by the CLI parser, with global options before
    the subcommand. Transport flags are removed only before the command remainder.
    No files are sent here. Synced jobs retain source project identity rather than
    the generated remote cwd. ``python_command`` marks an explicitly substituted
    remote interpreter so path remapping cannot turn it into a snapshot path.
    """
    # Global options always precede the subcommand. Consume their values rather
    # than searching for a string that might also be a host/profile name.
    position = 0
    while argv[position].startswith("-"):
        position += 1 if "=" in argv[position] else 2
    forwarded = argv[position:]
    prefix = []
    state_dir = args.state_dir or profile.get("state_dir")
    config = profile.get("config")
    if state_dir:
        prefix.extend(["--state-dir", str(state_dir)])
    if config:
        prefix.extend(["--config", str(config)])
    request = {"protocol": PROTOCOL_VERSION, "argv": [*prefix, *forwarded], "sync": False,
               "python": profile.get("python")}
    root = None
    if args.subcommand == "submit":
        command = list(args.command)
        if not command or command == ["--"]:
            raise ValueError("submit needs a command after --")
        if args.no_sync and (args.sync_include or args.sync_exclude):
            raise ValueError("--no-sync cannot be combined with sync include/exclude options")
        options = forwarded[:-len(command)] if command else forwarded
        options = strip_options(options, {
            "--cwd": True, "--remote-cwd": True, "--no-sync": False,
            "--sync-exclude": True, "--sync-include": True, "--sync-preview": False,
        })
        local_cwd = Path(args.cwd or Path.cwd()).expanduser().resolve()
        if not args.no_sync:
            if args.remote_cwd:
                raise ValueError("--remote-cwd requires --no-sync; synced submissions use isolated snapshots")
            if not local_cwd.is_dir():
                raise ValueError(f"local working directory does not exist: {local_cwd}")
            root = project_root(local_cwd)
            request.update({
                "sync": True, "local_root": str(root),
                "relative_cwd": local_cwd.relative_to(root).as_posix(),
                "project": args.project or f"{socket.gethostname()}:{root}",
                "work_dir": profile.get("work_dir", "~/.local/share/gpuq/workspaces"),
                "requirements": profile.get("requirements"),
                "python": profile.get("python", "python3"),
            })
        else:
            if profile.get("requirements"):
                raise ValueError("managed requirements environments require sync; use profile python for --no-sync")
            remote_cwd = args.remote_cwd or profile.get("cwd")
            if not remote_cwd:
                raise ValueError("--no-sync requires --remote-cwd or a profile cwd")
            options.extend(["--cwd", str(remote_cwd)])
        if profile.get("python") and command:
            offset = int(command[0] == "--")
            if len(command) > offset and re.fullmatch(r"python(?:\d+(?:\.\d+)*)?", Path(command[offset]).name):
                request["python_command"] = True
                command[offset] = str(profile["python"])
        request["argv"] = [*prefix, *options, *command]
    return request, root


def dispatch(args: argparse.Namespace, argv: list[str]) -> int:
    """Execute one SSH request, returning its exit status (130 on interruption).

    Sync sends a bounded JSON manifest, reads the server's missing-content reply,
    then streams only requested blobs. CLI stdout stays separate from progress on
    stderr; preview performs no SSH call. Errors never fall back to local execution
    or retry submission: a lost response can follow a successful remote enqueue.
    """
    profile = remote_profile(args)
    request, root = request_for(args, argv, profile)
    files = snapshot_files(root, args.sync_exclude, args.sync_include) if root is not None else []
    if args.subcommand == "submit" and args.sync_preview:
        print(json.dumps({"host": profile["host"], "project_root": str(root) if root else None,
                          "files": [path.relative_to(root).as_posix() for path in files]},
                         ensure_ascii=False, indent=2))
        return 0
    if root is not None:
        request["manifest"] = manifest_for(root, files)
    header = json.dumps(request).encode() + b"\n"
    if len(header) > HEADER_LIMIT:
        raise ValueError("remote manifest exceeds 16 MiB; exclude unnecessary files")
    process = subprocess.Popen(ssh_command(profile), stdin=subprocess.PIPE, stdout=subprocess.PIPE)
    try:
        try:
            process.stdin.write(header)
            process.stdin.flush()
            if root is not None:
                response = process.stdout.readline(HEADER_LIMIT + 1)
                if len(response) > HEADER_LIMIT or not response.endswith(b"\n"):
                    raise ValueError("remote did not return a valid sync handshake; check remote gpuq version")
                reply = json.loads(response)
                if not isinstance(reply, dict) or reply.get("protocol") != PROTOCOL_VERSION:
                    raise ValueError("remote sync protocol mismatch")
                missing = reply["missing"]
                print(f"sync: uploading {len(missing)} new content blobs for {len(files)} files", file=sys.stderr)
                write_blobs(root, request["manifest"], missing, process.stdin)
            process.stdin.close()
        except BrokenPipeError:
            pass
        # Keep JSON stdout clean, and stream logs without buffering until exit.
        while chunk := process.stdout.read1(65536):
            sys.stdout.buffer.write(chunk)
            sys.stdout.buffer.flush()
        code = process.wait()
    except KeyboardInterrupt:
        process.terminate()
        process.wait()
        return 130
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
    if code:
        print("remote gpuq failed. No local submission or automatic resubmission was attempted; "
              "inspect the remote queue before retrying after a connection failure.", file=sys.stderr)
    return code


def remap_path(value: str, local_root: str, destination: Path) -> str:
    if value == local_root or value.startswith(local_root + "/"):
        return str(destination) + value[len(local_root):]
    if "=" in value:
        key, content = value.split("=", 1)
        if key == "PYTHONPATH":
            return key + "=" + ":".join(remap_path(part, local_root, destination)
                                       for part in content.split(":"))
        return key + "=" + remap_path(content, local_root, destination)
    return value


def serve() -> int:
    """Handle one protocol-v2 request on stdin and call the remote CLI handler.

    Sync creates a fresh cwd from verified content, prepares optional dependencies,
    and persists resolved paths/environment through normal submit. Extraction or
    environment errors remove the snapshot, but preserve reusable blobs/environments.
    Handler failures occur outside that cleanup boundary and can leave a snapshot.
    No scheduler starts implicitly; job IDs and dependencies remain queue-local.
    """
    from .cli import build_parser

    header = sys.stdin.buffer.readline(HEADER_LIMIT + 1)
    if len(header) > HEADER_LIMIT or not header.endswith(b"\n"):
        raise ValueError("invalid remote request header")
    request = json.loads(header)
    if not isinstance(request, dict):
        raise ValueError("remote request must be an object")
    if request.get("protocol") != PROTOCOL_VERSION:
        raise ValueError("remote protocol mismatch; update gpuq on both hosts")
    argv = request.get("argv")
    if not isinstance(argv, list) or not all(isinstance(part, str) for part in argv):
        raise ValueError("remote argv must be a string array")
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.remote:
        raise ValueError("nested remote forwarding is not supported")
    if request.get("sync"):
        if args.subcommand != "submit":
            raise ValueError("only submit accepts a project snapshot")
        parent = Path(request["work_dir"]).expanduser().resolve()
        parent.mkdir(parents=True, exist_ok=True)
        destination = Path(tempfile.mkdtemp(prefix="job-", dir=parent))
        try:
            manifest = request["manifest"]
            cache = parent / "blobs"
            missing = missing_blobs(manifest, cache)
            print(json.dumps({"protocol": PROTOCOL_VERSION, "missing": missing}), flush=True)
            receive_blobs(sys.stdin.buffer, cache, missing)
            materialize(manifest, cache, destination)
            relative = Path(request["relative_cwd"])
            cwd = (destination / relative).resolve()
            if relative.is_absolute() or ".." in relative.parts or not cwd.is_relative_to(destination):
                raise ValueError("snapshot working directory is missing or outside the project")
            cwd.mkdir(parents=True, exist_ok=True)
            args.cwd = str(cwd)
            args.project = request["project"]
            offset = int(bool(args.command) and args.command[0] == "--")
            args.command = [
                part if index == offset and request.get("python_command")
                else remap_path(part, request["local_root"], destination)
                for index, part in enumerate(args.command)
            ]
            args.env = [remap_path(part, request["local_root"], destination) for part in args.env]
            defaults = {"HF_HOME": str(parent / "cache/huggingface"),
                        "TORCH_HOME": str(parent / "cache/torch"),
                        "PIP_CACHE_DIR": str(parent / "cache/pip")}
            supplied = {item.split("=", 1)[0] for item in args.env}
            args.env.extend(f"{key}={os.environ.get(key, value)}" for key, value in defaults.items() if key not in supplied)
            if request.get("requirements"):
                from .environment import prepare_environment
                environment = prepare_environment(parent, destination, request["requirements"], request["python"])
                offset = int(bool(args.command) and args.command[0] == "--")
                if len(args.command) > offset and (request.get("python_command") or re.fullmatch(
                    r"python(?:\d+(?:\.\d+)*)?", Path(args.command[offset]).name
                )):
                    args.command[offset] = str(environment / "bin/python")
                args.env.extend([f"VIRTUAL_ENV={environment}",
                                 f"PATH={environment / 'bin'}:{os.environ.get('PATH', '/usr/bin:/bin')}"])
            elif request.get("python"):
                offset = int(bool(args.command) and args.command[0] == "--")
                if len(args.command) > offset:
                    args.command[offset] = os.path.expanduser(args.command[offset])
            if args.log:
                args.log = remap_path(args.log, request["local_root"], destination)
        except Exception:
            shutil.rmtree(destination)
            raise
    if args.subcommand == "submit" and request.get("python") and not request.get("requirements"):
        python = Path(request["python"]).expanduser()
        if python.is_absolute() and not any(item.startswith("PATH=") for item in args.env):
            args.env.append(f"PATH={python.parent}:{os.environ.get('PATH', '/usr/bin:/bin')}")
    if args.subcommand == "submit" and args.command:
        offset = int(args.command[0] == "--")
        if len(args.command) > offset:
            args.command[offset] = os.path.expanduser(args.command[offset])
    args.no_sync = False
    args.remote_cwd = None
    return int(args.function(args))
