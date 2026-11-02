"""Exercise documentation gates in disposable repositories, including failure paths."""
import importlib.util
import json
from pathlib import Path
import subprocess
import sys

import pytest


spec = importlib.util.spec_from_file_location(
    "check_docs", Path(__file__).resolve().parents[1] / "scripts/check_docs.py",
)
checker = importlib.util.module_from_spec(spec)
spec.loader.exec_module(checker)


@pytest.fixture
def documented_repo(tmp_path):
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    (tmp_path / "docs").mkdir()
    (tmp_path / "feature.py").write_text("# a maintained module\n")
    (tmp_path / "AGENTS.md").write_text("[guide](docs/README.md)\n")
    for name in ("README.md", "README.zh-CN.md"):
        (tmp_path / name).write_text("[guide](docs/README.md)\n")
    (tmp_path / "docs/README.md").write_text("[module](feature.md)\n")
    (tmp_path / "docs/feature.md").write_text(
        "# Feature\n[guide](README.md)\n[source](../feature.py)\n"
        "[manifest](module-map.json)\n"
        + "\n".join(f"## {title}\nA concrete contract.\n" for title in checker.SECTIONS)
    )
    (tmp_path / "docs/module-map.json").write_text(json.dumps({
        "schema_version": 1,
        "modules": [{"doc": "docs/feature.md", "files": ["feature.py", "docs/module-map.json"]}],
    }))
    return tmp_path


def test_complete_docs_pass_without_staging_or_importing_source(documented_repo):
    (documented_repo / "feature.py").write_text("raise RuntimeError('must not import source')\n")
    errors, files, modules = checker.check(documented_repo)
    assert errors == []
    assert files == 7
    assert modules == 1


@pytest.mark.parametrize("problem", ["file", "anchor", "escape"])
def test_rejects_broken_links(documented_repo, problem):
    links = {"file": "absent.md", "anchor": "feature.md#missing", "escape": "../../outside"}
    with (documented_repo / "docs/README.md").open("a") as handle:
        handle.write(f"[bad]({links[problem]})\n")
    errors, _, _ = checker.check(documented_repo)
    assert any("broken" in error or "escapes repository" in error for error in errors)


def test_rejects_new_unowned_files(documented_repo):
    (documented_repo / "new.py").write_text("# new module\n")
    errors, _, _ = checker.check(documented_repo)
    assert "unowned file: new.py" in errors


def test_rejects_missing_contract_section(documented_repo):
    path = documented_repo / "docs/feature.md"
    path.write_text(path.read_text().replace("## 失败与限制", "## Unrelated heading"))
    errors, _, _ = checker.check(documented_repo)
    assert any("missing or empty section 失败与限制" in error for error in errors)


def test_rejects_duplicate_ownership(documented_repo):
    path = documented_repo / "docs/module-map.json"
    data = json.loads(path.read_text())
    data["modules"][0]["files"].append("feature.py")
    path.write_text(json.dumps(data))
    errors, _, _ = checker.check(documented_repo)
    assert any("duplicate owner for feature.py" in error for error in errors)


def test_rejects_orphaned_markdown(documented_repo):
    (documented_repo / "docs/lost.md").write_text("# Lost\n")
    errors, _, _ = checker.check(documented_repo)
    assert "unreachable document: docs/lost.md" in errors


def test_valid_anchors_and_fenced_examples(documented_repo):
    with (documented_repo / "docs/feature.md").open("a") as handle:
        handle.write("\n## Repeat\nOne.\n## Repeat\nTwo.\n"
                     "[repeat](#repeat-1)\n[Chinese](#接口与数据)\n"
                     "```md\n[example](does-not-exist.md)\n```\n")
    assert checker.check(documented_repo)[0] == []


def test_rejects_module_missing_from_main_navigation(documented_repo):
    (documented_repo / "docs/README.md").write_text("# Guide\n")
    errors, _, _ = checker.check(documented_repo)
    assert "docs/README.md: module not linked: docs/feature.md" in errors


def test_cli_failure_exit_code(documented_repo):
    (documented_repo / "undocumented.py").write_text("# missing contract\n")
    result = subprocess.run(
        [sys.executable, str(Path(checker.__file__)), "--root", str(documented_repo)],
        capture_output=True, text=True,
    )
    assert result.returncode == 1
    assert "unowned file: undocumented.py" in result.stderr
