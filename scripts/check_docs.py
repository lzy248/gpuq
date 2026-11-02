"""Validate documentation ownership/navigation without importing application code."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import re
import subprocess
import sys
from urllib.parse import unquote, urlsplit


SECTIONS = (
    "职责与边界", "接口与数据", "关键流程与不变量", "失败与限制", "修改与验证",
)
ENTRYPOINTS = ("AGENTS.md", "README.md", "README.zh-CN.md", "docs/README.md")


def prose(text: str) -> str:
    """Exclude fenced examples from structural Markdown inspection."""
    lines = []
    fence = None
    for line in text.splitlines():
        match = re.match(r"^\s*(`{3,}|~{3,})", line)
        if match:
            marker = match.group(1)
            if fence is None:
                fence = marker
            elif marker[0] == fence[0] and len(marker) >= len(fence):
                fence = None
            continue
        if fence is None:
            lines.append(line)
    return "\n".join(lines)


def anchors(text: str) -> set[str]:
    result = set()
    counts: dict[str, int] = {}
    for title in re.findall(r"^#{1,6}\s+(.+?)\s*#*\s*$", prose(text), re.MULTILINE):
        title = re.sub(r"\[([^]]+)\]\([^)]*\)", r"\1", title)
        title = re.sub(r"<[^>]+>", "", title).lower()
        slug = re.sub(r"[^\w\-\s]", "", title).replace(" ", "-")
        number = counts.get(slug, 0)
        counts[slug] = number + 1
        result.add(f"{slug}-{number}" if number else slug)
    result.update(re.findall(r'\b(?:id|name)=[\'"]([^\'"]+)[\'"]', prose(text)))
    return result


def links(text: str) -> list[str]:
    text = prose(text)
    targets = re.findall(r"!?\[[^\]\n]*\]\(\s*<?([^\s)>]+)>?(?:\s+\"[^\"]*\")?\s*\)", text)
    targets += re.findall(r'\b(?:href|src)=[\'"]([^\'"]+)[\'"]', text)
    return targets


def safe_path(root: Path, value: str) -> Path:
    path = (root / value).resolve()
    if not path.is_relative_to(root) or Path(value).is_absolute():
        raise ValueError(f"path escapes repository: {value}")
    return path


def check(root: Path) -> tuple[list[str], int, int]:
    root = root.resolve()
    errors: list[str] = []
    try:
        inventory = subprocess.run(
            ["git", "ls-files", "--cached", "--others", "--exclude-standard", "-z"],
            cwd=root, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
    except (OSError, subprocess.CalledProcessError) as error:
        return [f"cannot enumerate Git worktree: {error}"], 0, 0
    files = set(inventory.stdout.decode().rstrip("\0").split("\0")) - {""}
    try:
        manifest = json.loads((root / "docs/module-map.json").read_text())
        if manifest["schema_version"] != 1 or not isinstance(manifest["modules"], list):
            raise ValueError("expected schema_version=1 and modules array")
    except (OSError, ValueError, KeyError, TypeError) as error:
        return [f"invalid docs/module-map.json: {error}"], len(files), 0

    texts = {}
    for name in sorted(files):
        try:
            path = safe_path(root, name)
            if not path.is_file():
                errors.append(f"missing maintained file: {name}")
            elif path.suffix.lower() == ".md":
                texts[name] = path.read_text(encoding="utf-8")
        except (OSError, ValueError) as error:
            errors.append(f"{name}: {error}")

    graph: dict[str, set[str]] = {name: set() for name in texts}
    for name, text in texts.items():
        for href in links(text):
            try:
                url = urlsplit(href)
                if url.scheme or url.netloc:
                    continue
                candidate = (Path(name).parent / unquote(url.path)) if url.path else Path(name)
                target = safe_path(root, str(candidate))
                relative = target.relative_to(root).as_posix()
                if not target.exists():
                    errors.append(f"{name}: broken link {href}")
                    continue
                graph[name].add(relative)
                if url.fragment and target.suffix.lower() == ".md":
                    content = texts.get(relative)
                    if content is None:
                        content = target.read_text(encoding="utf-8")
                    if unquote(url.fragment) not in anchors(content):
                        errors.append(f"{name}: broken anchor {href}")
            except (OSError, ValueError) as error:
                errors.append(f"{name}: invalid link {href}: {error}")

    owners: dict[str, str] = {}
    documents: set[str] = set()
    for module in manifest["modules"]:
        if not isinstance(module, dict):
            errors.append("invalid module entry: expected object")
            continue
        doc, members = module.get("doc"), module.get("files")
        if not isinstance(doc, str) or not isinstance(members, list) or not members:
            errors.append(f"invalid module entry: {module}")
            continue
        if doc in documents:
            errors.append(f"duplicate module document: {doc}")
        documents.add(doc)
        if doc not in texts:
            errors.append(f"missing module document: {doc}")
        else:
            headings = re.split(r"^##\s+", prose(texts[doc]), flags=re.MULTILINE)[1:]
            sections = dict(part.split("\n", 1) if "\n" in part else (part, "") for part in headings)
            for title in SECTIONS:
                if not sections.get(title, "").strip():
                    errors.append(f"{doc}: missing or empty section {title}")
        if doc not in graph.get("docs/README.md", set()):
            errors.append(f"docs/README.md: module not linked: {doc}")
        if "docs/README.md" not in graph.get(doc, set()):
            errors.append(f"{doc}: missing link back to docs/README.md")
        for member in members:
            if not isinstance(member, str):
                errors.append(f"{doc}: invalid file entry {member!r}")
                continue
            if member in owners:
                errors.append(f"duplicate owner for {member}: {owners[member]}, {doc}")
            owners[member] = doc
            if member not in files:
                errors.append(f"{doc}: unknown or ignored file {member}")
            if member not in graph.get(doc, set()):
                errors.append(f"{doc}: missing source link for {member}")

    for name in sorted(files - owners.keys()):
        if Path(name).suffix.lower() != ".md":
            errors.append(f"unowned file: {name}")
    for name in ENTRYPOINTS:
        if name not in texts:
            errors.append(f"missing documentation entrypoint: {name}")
    reachable = set()
    pending = list(ENTRYPOINTS)
    while pending:
        name = pending.pop()
        if name in reachable:
            continue
        reachable.add(name)
        pending.extend(graph.get(name, set()) - reachable)
    for name in sorted(texts.keys() - reachable):
        errors.append(f"unreachable document: {name}")
    return errors, len(files), len(documents)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    args = parser.parse_args()
    errors, files, modules = check(args.root)
    if errors:
        print("Documentation validation failed:", file=sys.stderr)
        for error in errors:
            print(f"- {error}", file=sys.stderr)
        return 1
    print(f"Documentation OK: {files} maintained files, {modules} module documents.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
