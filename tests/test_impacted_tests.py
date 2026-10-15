"""Tests for ``scripts/impacted-tests`` — the one changed-files -> tests
selector behind ``scripts/ship --checked`` and ``scripts/test --selected``
(the release lane it was designed for is deferred).

Each test builds a tiny throwaway git repo (one src package, one tests/
dir) and asserts on the *selection set*: which rules fired, that nothing
below the always-include floor was narrowed away, and that the FULL
triggers (harness files) decline to pick a subset at all.
"""

from __future__ import annotations

import hashlib
import importlib.machinery
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import ModuleType

import pytest

REPO = Path(__file__).resolve().parents[1]
SCRIPT = REPO / "scripts" / "impacted-tests"


def _load() -> ModuleType:
    # Loaded from a `.py`-suffixed copy, not the dotless executable: testmon
    # fingerprints by extension and IndexErrors on a dotless file (gr450298)
    # — same convention as tests/test_last_gated_main_sha.py.
    tmp_copy = Path(tempfile.mkdtemp(prefix="impacted_tests_")) / "impacted_tests.py"
    shutil.copyfile(SCRIPT, tmp_copy)
    loader = importlib.machinery.SourceFileLoader("impacted_tests", str(tmp_copy))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    assert spec is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules["impacted_tests"] = mod
    loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def mod() -> ModuleType:
    return _load()


def _git(repo: Path, *args: str) -> str:
    env = {
        **os.environ,
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_CONFIG_SYSTEM": os.devnull,
    }
    cp = subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        env=env,
        check=True,
    )
    return cp.stdout.strip()


def _write(repo: Path, rel: str, text: str) -> None:
    p = repo / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")


@pytest.fixture(scope="module")
def toy_repo(tmp_path_factory: pytest.TempPathFactory, mod: ModuleType) -> Path:
    """Two-commit repo: v1 then a v2 touching one src fn + one test."""
    repo = tmp_path_factory.mktemp("selrepo")
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "t@example.invalid")
    _git(repo, "config", "user.name", "t")

    _write(
        repo,
        "src/pkg/__init__.py",
        "",
    )
    _write(
        repo,
        "src/pkg/core.py",
        "def compute(x):\n    return x + 1\n\n\ndef other(y):\n    return y\n",
    )
    _write(
        repo,
        "src/pkg/data/notes.txt",
        "v1 notes\n",
    )
    _write(repo, "src/pkg/migrations/0001_init.sql", "create table t (i int);\n")
    _write(
        repo,
        "tests/__init__.py",
        "",
    )
    _write(
        repo,
        "tests/test_core.py",
        "from pkg.core import compute\n\n\ndef test_compute():\n    assert compute(1) == 2\n",
    )
    _write(repo, "tests/test_other.py", "def test_other():\n    assert True\n")
    _write(repo, "pyproject.toml", "[project]\nname='x'\n")
    _write(repo, "docs/README.md", "# docs\n")
    # The rule-based sets (smoke, doc hygiene, migration battery) are
    # filtered to files that exist — provide them so the union is visible.
    for rel in (
        *mod.SMOKE_TESTS,
        *mod._DOCS_HYGIENE,
    ):
        _write(repo, rel, "def test_x():\n    pass\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "v1")
    return repo


def _commit(repo: Path, edits: dict[str, str]) -> str:
    for rel, text in edits.items():
        _write(repo, rel, text)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "v2")
    return _git(repo, "rev-parse", "HEAD")


def _sel(mod: ModuleType, repo: Path, base: str, head: str) -> dict:
    return mod.select(repo, base, head, None)


def test_changed_test_file_is_selected(mod: ModuleType, toy_repo: Path) -> None:
    head = _commit(
        toy_repo,
        {
            "tests/test_other.py": "def test_other():\n    assert 1 == 1\n",
        },
    )
    res = _sel(mod, toy_repo, f"{head}^", head)
    assert res["mode"] == "selected"
    assert "tests/test_other.py" in res["files"]
    assert "tests/test_core.py" not in res["files"]


def test_src_change_selects_importer_and_named_tests(
    mod: ModuleType, toy_repo: Path
) -> None:
    head = _commit(
        toy_repo,
        {
            "src/pkg/core.py": "def compute(x):\n    return x + 2\n\n\ndef other(y):\n    return y\n",
        },
    )
    res = _sel(mod, toy_repo, f"{head}^", head)
    assert res["mode"] == "selected"
    assert "tests/test_core.py" in res["files"]


def test_full_trigger_runs_everything(mod: ModuleType, toy_repo: Path) -> None:
    head = _commit(toy_repo, {"pyproject.toml": "[project]\nname='y'\n"})
    res = _sel(mod, toy_repo, f"{head}^", head)
    assert res["mode"] == "full"
    assert any("pyproject.toml" in r for r in res["full_because"])


def test_conftest_is_a_full_trigger(mod: ModuleType, toy_repo: Path) -> None:
    head = _commit(toy_repo, {"tests/conftest.py": "# x\n"})
    res = _sel(mod, toy_repo, f"{head}^", head)
    assert res["mode"] == "full"


def test_migration_selects_migration_set(mod: ModuleType, toy_repo: Path) -> None:
    _write(toy_repo, "tests/test_migrate.py", "def test_m():\n    pass\n")
    _git(toy_repo, "add", "-A")
    _git(toy_repo, "commit", "-qm", "add mig test")
    head = _commit(
        toy_repo, {"src/pkg/migrations/0002_more.sql": "alter table t add j int;\n"}
    )
    res = _sel(mod, toy_repo, f"{head}^", head)
    assert res["mode"] == "selected"
    assert "tests/test_migrate.py" in res["files"]
    assert "tests/test_core.py" not in res["files"]


def test_docs_only_selects_hygiene(mod: ModuleType, toy_repo: Path) -> None:
    head = _commit(toy_repo, {"docs/guide.md": "# g\n"})
    res = _sel(mod, toy_repo, f"{head}^", head)
    assert res["mode"] == "selected"
    # Doc hygiene plus the contract invariants (filtered to files that
    # exist in the toy repo) — docs can be corpus-coupled too.
    allowed = set(mod._DOCS_HYGIENE) | {
        t for t in mod.CONTRACT if (toy_repo / t.split("::", 1)[0]).is_file()
    }
    assert set(res["files"]) <= allowed
    assert res["files"]


def test_unknown_data_file_gets_smoke_floor(mod: ModuleType, toy_repo: Path) -> None:
    head = _commit(toy_repo, {"src/pkg/data/blob.bin": "\x00\x01"})
    res = _sel(mod, toy_repo, f"{head}^", head)
    assert res["mode"] == "selected"
    assert set(mod.SMOKE_TESTS) <= set(res["files"])


def test_text_scan_finds_fixture_consumers(mod: ModuleType, toy_repo: Path) -> None:
    _write(
        toy_repo,
        "tests/test_fixture_reader.py",
        "def test_fx():\n    assert open('src/pkg/data/notes.txt').read()\n",
    )
    _git(toy_repo, "add", "-A")
    _git(toy_repo, "commit", "-qm", "reader")
    head = _commit(toy_repo, {"src/pkg/data/notes.txt": "v2 notes\n"})
    res = _sel(mod, toy_repo, f"{head}^", head)
    assert "tests/test_fixture_reader.py" in res["files"]


def test_output_is_deterministic(mod: ModuleType, toy_repo: Path) -> None:
    head = _commit(
        toy_repo,
        {
            "src/pkg/core.py": "def compute(x):\n    return x + 3\n\n\ndef other(y):\n    return y\n"
        },
    )
    a = _sel(mod, toy_repo, f"{head}^", head)
    b = _sel(mod, toy_repo, f"{head}^", head)
    assert a["files"] == b["files"]


def test_stale_coverage_db_falls_back(mod: ModuleType, toy_repo: Path) -> None:
    """A manifest older than the base commit must not feed measured hits."""
    db = toy_repo / ".coverage"
    db.write_bytes(b"")
    old = (datetime.now(UTC) - timedelta(days=30)).isoformat().replace("+00:00", "Z")
    (toy_repo / ".coverage.precis.json").write_text(
        json.dumps({"created_at": old, "files": {"x": "y"}}), encoding="utf-8"
    )
    head = _commit(
        toy_repo,
        {
            "src/pkg/core.py": "def compute(x):\n    return x + 4\n\n\ndef other(y):\n    return y\n"
        },
    )
    cdb = mod.load_coverage(toy_repo, db, f"{head}^")
    assert cdb.state == "stale"


# ---------------------------------------------------------------------------
# Worktree family (the --checked / --selected protocol)
# ---------------------------------------------------------------------------


def test_stdin_mode_emits_testmon_protocol(
    mod: ModuleType, toy_repo: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    """--stdin speaks the checked wire protocol: `testmon=N` then targets."""
    import io

    monkeypatch.setattr(sys, "stdin", io.StringIO("tests/test_core.py\n"))
    assert mod.main(["--root", str(toy_repo), "--stdin"]) == 0
    lines = capsys.readouterr().out.splitlines()
    assert lines[0] == "testmon=0"
    assert "tests/test_core.py" in lines[1:]


def test_stdin_mode_full_is_bare(
    mod: ModuleType, toy_repo: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    """A harness change in --stdin mode prints FULL alone — the caller
    (ship --checked) decides what refusing looks like."""
    import io

    monkeypatch.setattr(sys, "stdin", io.StringIO("pyproject.toml\n"))
    assert mod.main(["--root", str(toy_repo), "--stdin"]) == 0
    assert capsys.readouterr().out.strip() == "FULL"


def test_worktree_selector_picks_changed_test_and_map(
    mod: ModuleType, toy_repo: Path
) -> None:
    """Selector.select: changed test file + PATH_MAP coupling surface in the
    (targets, testmon) worktree answer."""
    sel = mod.Selector(toy_repo)
    targets, testmon = sel.select(["tests/test_core.py"])
    assert "tests/test_core.py" in targets
    assert testmon is False  # the toy repo carries no .testmondata map


def test_worktree_selector_smoke_floor(mod: ModuleType, toy_repo: Path) -> None:
    sel = mod.Selector(toy_repo)
    targets, _ = sel.select(["src/pkg/data/blob.bin"])
    assert set(mod.SMOKE_TESTS) <= set(targets)


def test_migration_path_map_via_worktree_mode(mod: ModuleType, toy_repo: Path) -> None:
    """**/migrations/** selects the PATH_MAP couplings plus the migration
    battery, whichever exist in the tree."""
    _write(toy_repo, "tests/test_migrate.py", "def test_m():\n    pass\n")
    sel = mod.Selector(toy_repo)
    targets, _ = sel.select(["src/pkg/migrations/0009_x.sql"])
    assert "tests/test_migrate.py" in targets


def test_measured_lookup_maps_lines_to_test_files(mod: ModuleType) -> None:
    """contexts_by_lineno -> test files; ``lines=None`` means the whole file."""

    class _FakeData:
        def contexts_by_lineno(self, path: str) -> dict[int, list[str]]:
            return {
                2: ["tests/test_a.py::test_x|"],
                9: ["tests/test_b.py::test_y|setup"],
            }

    cdb = mod.CoverageDB("ok", "fake", data=_FakeData())
    assert mod.measured_tests(cdb, "src/pkg/core.py", {2}) == {"tests/test_a.py"}
    assert mod.measured_tests(cdb, "src/pkg/core.py", None) == {
        "tests/test_a.py",
        "tests/test_b.py",
    }
    assert mod.measured_tests(cdb, "src/pkg/core.py", {99}) == set()


def test_verify_file_matches_base_blob_hash(mod: ModuleType, toy_repo: Path) -> None:
    """Manifest hash vs the BASE blob — a file that moved since the run is
    unverified and goes static."""
    head = _git(toy_repo, "rev-parse", "HEAD")
    raw = mod._blob(toy_repo, head, "src/pkg/core.py")
    assert raw is not None
    good = hashlib.sha256(raw).hexdigest()
    cdb = mod.CoverageDB(
        "ok",
        "fake",
        manifest={"src/pkg/core.py": good, "src/pkg/other.py": "deadbeef"},
    )
    assert mod.verify_file(cdb, toy_repo, head, "src/pkg/core.py") == "src/pkg/core.py"
    assert mod.verify_file(cdb, toy_repo, head, "src/pkg/other.py") is None
    assert mod.verify_file(cdb, toy_repo, head, "src/pkg/absent.py") is None
