"""Incremental content-addressed uploads and isolated writable code snapshots."""
from __future__ import annotations

import fnmatch
import hashlib
import os
from pathlib import Path, PurePosixPath
import shutil
import subprocess
import tarfile
import tempfile
from typing import BinaryIO


DEFAULT_EXCLUDES = (
    ".git", ".venv", "venv", "__pycache__", "*.pyc", ".env", ".env.*", ".ssh", ".aws",
    "*.safetensors", "*.bin", "*.pt", "*.pth", "*.ckpt", "*.onnx", "*.h5",
    "checkpoints", "outputs", "wandb", "runs", ".cache",
)


def safe_relative(name: str) -> PurePosixPath:
    path = PurePosixPath(name)
    if not name or path.is_absolute() or ".." in path.parts or str(path) != name or name == ".":
        raise ValueError(f"unsafe snapshot path: {name!r}")
    return path


def snapshot_files(root: Path, excludes: list[str], includes: list[str] | None = None) -> list[Path]:
    """Select sorted project files without uploading or changing them.

    root must be resolved. Git selection honors ignore rules for untracked files;
    defaults, .gpuqignore and excludes further filter the set. Explicit relative
    includes override those exclusions, including ignored checkpoint directories.
    Missing includes and paths/symlinks escaping the root raise ValueError. This
    selection is not an atomic filesystem snapshot; transfer checks content hashes.
    """
    rules = list(DEFAULT_EXCLUDES) + list(excludes)
    ignore = root / ".gpuqignore"
    if ignore.exists():
        rules.extend(line.strip() for line in ignore.read_text().splitlines()
                     if line.strip() and not line.lstrip().startswith("#"))
    if any(rule.startswith("!") for rule in rules):
        raise ValueError(".gpuqignore supports exclude globs, not ! re-inclusion rules")

    def excluded(relative: str) -> bool:
        parts = PurePosixPath(relative).parts
        return any(
            fnmatch.fnmatchcase(relative, rule.rstrip("/"))
            or relative.startswith(rule.rstrip("/") + "/")
            or ("/" not in rule.rstrip("/") and any(
                fnmatch.fnmatchcase(part, rule.rstrip("/")) for part in parts
            )) for rule in rules
        )

    def walk(directory: Path, filter_rules: bool) -> set[str]:
        names = set()
        for current, dirs, files in os.walk(directory, followlinks=False):
            base = Path(current).relative_to(root)
            if filter_rules:
                dirs[:] = [name for name in dirs if not excluded((base / name).as_posix())]
            for name in [*files, *(name for name in dirs if (root / base / name).is_symlink())]:
                names.add((base / name).as_posix())
        return names

    if (root / ".git").exists():
        result = subprocess.run(
            ["git", "-C", str(root), "ls-files", "--cached", "--others", "--exclude-standard", "-z"],
            check=True, stdout=subprocess.PIPE,
        )
        names = set(os.fsdecode(result.stdout).split("\0")) - {""}
    else:
        names = walk(root, True)
    names = {name for name in names if not excluded(name)}
    for name in includes or []:
        safe_relative(name)
        path = root / name
        if not path.resolve().is_relative_to(root):
            raise ValueError(f"included path leaves project: {name}")
        if not path.exists():
            raise ValueError(f"included path does not exist: {name}")
        names.update(walk(path, False) if path.is_dir() and not path.is_symlink() else {name})
    selected = []
    for name in sorted(names):
        safe_relative(name)
        path = root / name
        if not path.exists() and not path.is_symlink():
            continue
        if not path.parent.resolve().is_relative_to(root):
            raise ValueError(f"snapshot parent leaves project: {name}")
        if path.is_symlink():
            if Path(os.readlink(path)).is_absolute() or not path.resolve().is_relative_to(root):
                raise ValueError(f"snapshot symlink leaves project: {name}; exclude it or use --no-sync")
        elif not path.is_file():
            raise ValueError(f"unsupported snapshot entry: {name}; exclude it or use --no-sync")
        selected.append(path)
    return selected


def digest_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def manifest_for(root: Path, files: list[Path]) -> dict[str, dict]:
    manifest = {}
    for path in files:
        if path.is_symlink():
            entry = {"link": os.readlink(path)}
        else:
            entry = {"sha256": digest_file(path), "mode": path.stat().st_mode & 0o777}
        manifest[path.relative_to(root).as_posix()] = entry
    validate_manifest(manifest)
    return manifest


def validate_manifest(manifest: dict) -> None:
    if not isinstance(manifest, dict):
        raise ValueError("invalid snapshot manifest")
    for name, entry in manifest.items():
        path = safe_relative(name)
        if any(str(parent) in manifest for parent in path.parents if str(parent) != "."):
            raise ValueError(f"snapshot entry has a file or symlink ancestor: {name}")
        if not isinstance(entry, dict):
            raise ValueError("invalid snapshot entry")
        if "link" in entry:
            link = entry["link"]
            if not isinstance(link, str) or not link or PurePosixPath(link).is_absolute():
                raise ValueError(f"unsafe snapshot symlink: {name}")
            # Resolve lexically and disallow all parent traversal for predictable extraction.
            if ".." in PurePosixPath(link).parts:
                raise ValueError(f"snapshot symlink contains parent traversal: {name}")
        else:
            digest = entry.get("sha256", "")
            if not isinstance(digest, str) or len(digest) != 64 or any(c not in "0123456789abcdef" for c in digest):
                raise ValueError("invalid snapshot content digest")
            mode = entry.get("mode")
            if type(mode) is not int or not 0 <= mode <= 0o777:
                raise ValueError("invalid snapshot file mode")


def missing_blobs(manifest: dict, cache: Path) -> list[str]:
    """Return unique missing or corrupt digests after validating the manifest.

    Creates cache if needed and reads/hashes existing blobs. No transfer occurs;
    this is a network deduplication step, not a promise of zero local disk IO.
    """
    validate_manifest(manifest)
    cache.mkdir(parents=True, exist_ok=True)
    return sorted(digest for digest in {e["sha256"] for e in manifest.values() if "sha256" in e}
                  if not (cache / digest).is_file() or digest_file(cache / digest) != digest)


def write_blobs(root: Path, manifest: dict, missing: list[str], stream: BinaryIO) -> None:
    """Stream each requested known content digest once as a tar file member.

    Unknown or duplicate requests are rejected before writing. Source files are
    read at transfer time; the receiver must verify content against manifest
    hashes to reject changes since manifest creation. Does not update local files.
    """
    sources = {entry["sha256"]: root / name for name, entry in manifest.items() if "sha256" in entry}
    if not isinstance(missing, list) or len(set(missing)) != len(missing) or not set(missing) <= sources.keys():
        raise ValueError("remote requested unknown or duplicate content")
    with tarfile.open(fileobj=stream, mode="w|") as archive:
        for digest in missing:
            archive.add(sources[digest], arcname=digest, recursive=False)


def receive_blobs(stream: BinaryIO, cache: Path, missing: list[str]) -> None:
    """Verify a streaming tar upload and atomically install requested content.

    cache must exist. Only ordinary members named by requested SHA-256 digests are
    accepted; duplicates, changed content and incomplete transfers raise ValueError.
    Each verified blob becomes reusable immediately, even if a later member fails.
    Temporary files are removed on ordinary exceptions; no rollback of valid blobs.
    """
    remaining = set(missing)
    with tarfile.open(fileobj=stream, mode="r|*") as archive:
        for member in archive:
            if member.name not in remaining or not member.isfile():
                raise ValueError("unexpected upload entry")
            source = archive.extractfile(member)
            with tempfile.NamedTemporaryFile(dir=cache, delete=False) as output:
                temporary = Path(output.name)
                try:
                    shutil.copyfileobj(source, output)
                    output.close()
                    if digest_file(temporary) != member.name:
                        raise ValueError("uploaded content changed during sync or failed checksum")
                    temporary.chmod(0o400)
                    os.replace(temporary, cache / member.name)
                    remaining.remove(member.name)
                finally:
                    temporary.unlink(missing_ok=True)
    if remaining:
        raise ValueError("upload incomplete")


def materialize(manifest: dict, cache: Path, destination: Path) -> None:
    """Populate a fresh task directory from a validated manifest and blob cache.

    The caller owns destination and cleanup on failure. Files are copied, never
    hardlinked, so training writes cannot mutate another task or cached content.
    File modes are restored per entry; relative symlinks follow regular files.
    This does not create every source empty directory or perform cache eviction.
    """
    validate_manifest(manifest)
    for name, entry in manifest.items():
        path = destination / name
        path.parent.mkdir(parents=True, exist_ok=True)
        if "sha256" in entry:
            # Never hardlink writable task files to reusable content or another task.
            shutil.copyfile(cache / entry["sha256"], path)
            path.chmod(entry["mode"])
    for name, entry in manifest.items():
        if "link" in entry:
            path = destination / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.symlink_to(entry["link"])
    for name, entry in manifest.items():
        if "link" in entry and not (destination / name).resolve().is_relative_to(destination):
            raise ValueError(f"snapshot symlink escapes destination: {name}")
