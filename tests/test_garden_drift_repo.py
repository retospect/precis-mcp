"""Git-only fixtures for the gardener's timestamped section/symbol evidence."""

from __future__ import annotations

import os
import subprocess
from datetime import UTC, datetime
from pathlib import Path

import pytest

from precis.graph.garden._drift_repo import RepoDriftResolver

TOUCHED = datetime(2026, 1, 2, tzinfo=UTC)


def _git(root: Path, *args: str, date: str | None = None) -> str:
    env = dict(os.environ)
    if date is not None:
        env.update(GIT_AUTHOR_DATE=date, GIT_COMMITTER_DATE=date)
    return subprocess.run(
        ["git", "-C", str(root), *args],
        env=env,
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=10,
    ).stdout.strip()


def _commit(root: Path, date: str) -> str:
    message = root / ".git" / "fixture-message"
    message.write_text("Drift fixture", encoding="utf-8")
    _git(root, "add", "docs/guide.md", "src/sample/__init__.py", "src/sample/mod.py")
    _git(
        root,
        "-c",
        "user.name=Drift fixture",
        "-c",
        "user.email=fixture@example.invalid",
        "-c",
        "commit.gpgsign=false",
        "commit",
        "-F",
        str(message),
        date=date,
    )
    return _git(root, "rev-parse", "HEAD")


@pytest.fixture
def drift_repo(tmp_path: Path) -> Path:
    _git(tmp_path, "init", "-b", "main")
    (tmp_path / "docs").mkdir()
    (tmp_path / "src/sample").mkdir(parents=True)
    (tmp_path / "src/sample/__init__.py").write_text("", encoding="utf-8")
    (tmp_path / "src/sample/mod.py").write_text(
        "def live():\n    return 1\n\ndef other():\n    return 3\n", encoding="utf-8"
    )
    (tmp_path / "docs/guide.md").write_text(
        "# Guide\n\n## Contract\n\nOld rule.\n\n### Nested\n\nDetail.\n\n"
        "## Other\n\nOther rule.\n",
        encoding="utf-8",
    )
    _commit(tmp_path, "2026-01-01T00:00:00Z")
    return tmp_path


def test_section_body_change_pins_both_snapshots(drift_repo: Path) -> None:
    prior = _git(drift_repo, "rev-parse", "main")
    path = drift_repo / "docs/guide.md"
    path.write_text(
        path.read_text(encoding="utf-8").replace("Old rule.", "New rule."),
        encoding="utf-8",
    )
    current = _commit(drift_repo, "2026-01-03T00:00:00Z")
    result = RepoDriftResolver(drift_repo).section("docs/guide.md", "Contract", TOUCHED)
    assert result["status"] == "changed"
    assert result["old_sha"] != result["new_sha"]
    assert result["prior_main_sha"] == prior
    assert result["main_sha"] == current
    assert "New rule." in result["excerpt"]
    assert "Detail." in result["excerpt"]
    assert "Other rule." not in result["excerpt"]


@pytest.mark.parametrize(
    "heading", ["Contract", "#contract", "~contract", "Contract for details."]
)
def test_unrelated_section_change_and_dirty_files_are_ignored(
    drift_repo: Path, heading: str
) -> None:
    path = drift_repo / "docs/guide.md"
    path.write_text(
        path.read_text(encoding="utf-8").replace("Other rule.", "Unrelated edit."),
        encoding="utf-8",
    )
    _commit(drift_repo, "2026-01-03T00:00:00Z")
    path.write_text("## Contract\n\nDirty content.\n", encoding="utf-8")
    result = RepoDriftResolver(drift_repo).section("docs/guide.md", heading, TOUCHED)
    assert result["status"] == "unchanged"
    assert result["old_sha"] == result["new_sha"]
    assert "Dirty content." not in result["excerpt"]


def test_ambiguous_heading_prefixes_are_not_checked(drift_repo: Path) -> None:
    path = drift_repo / "docs/guide.md"
    path.write_text(
        path.read_text(encoding="utf-8") + "\n## Contract details\nDetails.\n",
        encoding="utf-8",
    )
    _commit(drift_repo, "2026-01-03T00:00:00Z")
    result = RepoDriftResolver(drift_repo).section(
        "docs/guide.md", "Contract details for review", TOUCHED
    )
    assert result["status"] == "not_checked"
    assert "missing or ambiguous" in result["reason"]


def test_missing_baseline_is_not_checked(drift_repo: Path) -> None:
    result = RepoDriftResolver(drift_repo).section(
        "docs/guide.md", "Contract", datetime(2025, 1, 1, tzinfo=UTC)
    )
    assert result["status"] == "not_checked"
    assert "before memory touch" in result["reason"]
    assert "old_sha" not in result


def test_missing_or_ambiguous_section_is_not_checked(drift_repo: Path) -> None:
    path = drift_repo / "docs/guide.md"
    path.write_text("## Contract\nOne.\n## Contract\nTwo.\n", encoding="utf-8")
    _commit(drift_repo, "2026-01-03T00:00:00Z")
    resolver = RepoDriftResolver(drift_repo)
    for heading in ("Contract", "Missing"):
        result = resolver.section("docs/guide.md", heading, TOUCHED)
        assert result["status"] == "not_checked"
        assert "missing or ambiguous" in result["reason"]


@pytest.mark.parametrize(
    ("target", "detail"), [("sample.mod.live", None), ("src/sample/mod.py", "live")]
)
def test_python_symbol_changed(
    drift_repo: Path, target: str, detail: str | None
) -> None:
    path = drift_repo / "src/sample/mod.py"
    path.write_text(
        path.read_text(encoding="utf-8").replace("return 1", "return 2"),
        encoding="utf-8",
    )
    _commit(drift_repo, "2026-01-03T00:00:00Z")
    result = RepoDriftResolver(drift_repo).python(target, detail, TOUCHED)
    assert result["status"] == "changed"
    assert result["old_sha"] != result["new_sha"]
    assert "return 2" in result["excerpt"]
    assert "def other" not in result["excerpt"]


def test_python_ignores_unrelated_symbol_edits(drift_repo: Path) -> None:
    path = drift_repo / "src/sample/mod.py"
    path.write_text(
        path.read_text(encoding="utf-8").replace("return 3", "return 4"),
        encoding="utf-8",
    )
    _commit(drift_repo, "2026-01-03T00:00:00Z")
    result = RepoDriftResolver(drift_repo).python("sample.mod.live", None, TOUCHED)
    assert result["status"] == "unchanged"
    assert result["old_sha"] == result["new_sha"]


def test_shallow_history_is_not_checked(drift_repo: Path) -> None:
    (drift_repo / ".git/shallow").write_text(
        _git(drift_repo, "rev-parse", "HEAD") + "\n", encoding="utf-8"
    )
    result = RepoDriftResolver(drift_repo).section("docs/guide.md", "Contract", TOUCHED)
    assert result["status"] == "not_checked"
    assert "shallow" in result["reason"]


@pytest.mark.parametrize("kind", ["section", "document"])
def test_late_change_is_in_evidence(drift_repo: Path, kind: str) -> None:
    path = drift_repo / "docs/guide.md"
    original = "## Contract\n\n" + "B" * 1300 + "\nRuns router A.\n"
    path.write_text(original, encoding="utf-8")
    _commit(drift_repo, "2026-01-01T12:00:00Z")
    path.write_text(original.replace("router A", "router B"), encoding="utf-8")
    _commit(drift_repo, "2026-01-03T00:00:00Z")
    resolver = RepoDriftResolver(drift_repo)
    result = (
        resolver.section("docs/guide.md", "Contract", TOUCHED)
        if kind == "section"
        else resolver.document("docs/guide.md", TOUCHED)
    )
    assert result["status"] == "changed"
    assert "Runs router B." in result["excerpt"]
    assert len(result["excerpt"]) <= 1200


def test_document_change_is_bounded_and_cached(drift_repo: Path) -> None:
    path = drift_repo / "docs/guide.md"
    path.write_text("# Contract\n" + "New rule. " * 200, encoding="utf-8")
    _commit(drift_repo, "2026-01-03T00:00:00Z")
    resolver = RepoDriftResolver(drift_repo)
    result = resolver.document("docs/guide.md", TOUCHED)
    assert result["status"] == "changed"
    assert len(result["excerpt"]) == 1200
    assert result == resolver.document("docs/guide.md", TOUCHED)
    result["status"] = "caller mutation"
    assert resolver.document("docs/guide.md", TOUCHED)["status"] == "changed"


def test_missing_python_symbol_and_document_are_not_checked(drift_repo: Path) -> None:
    resolver = RepoDriftResolver(drift_repo)
    for result in (
        resolver.python("sample.mod.gone", None, TOUCHED),
        resolver.document("docs/missing.md", TOUCHED),
    ):
        assert result["status"] == "not_checked"
        assert result["reason"]
        assert "old_sha" not in result


def test_python_decorator_change_is_part_of_symbol(drift_repo: Path) -> None:
    path = drift_repo / "src/sample/mod.py"
    path.write_text("@new_contract\ndef live():\n    return 1\n", encoding="utf-8")
    _commit(drift_repo, "2026-01-03T00:00:00Z")
    result = RepoDriftResolver(drift_repo).python("sample.mod.live", None, TOUCHED)
    assert result["status"] == "changed"
    assert result["excerpt"].startswith("@new_contract\n")
