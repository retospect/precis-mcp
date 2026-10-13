"""Tests for the python kind's ``view='tests'`` — evidence-labelled test
discovery (slice 2 of python navigation context+impact).

DB-free except for the measured-coverage tests, which build a real
``.coverage`` SQLite DB via the ``coverage`` package (a test dep) and a
manifest via ``scripts/lib/coverage_manifest.py`` (loaded by path, same as
``test_mutate_driver.py``).
"""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
import textwrap
from pathlib import Path
from typing import Any

import coverage
import pytest

from precis.dispatch import Hub
from precis.errors import BadInput, NotFound
from precis.handlers.python import PythonHandler
from tests.hintcheck import assert_hints_round_trip

_MANIFEST_SCRIPT = (
    Path(__file__).resolve().parents[1] / "scripts" / "lib" / "coverage_manifest.py"
)


def _load_manifest_mod() -> Any:
    spec = importlib.util.spec_from_file_location("coverage_manifest", _MANIFEST_SCRIPT)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


def _write(repo: Path, rel: str, content: str) -> Path:
    f = repo / rel
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(textwrap.dedent(content).lstrip("\n"), encoding="utf-8")
    return f


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """A repo with one target symbol and tests evidencing it five ways."""
    _write(tmp_path, "pkg/__init__.py", "")
    _write(
        tmp_path,
        "pkg/core.py",
        """
        def target(x: int) -> int:
            return x + 1


        def helper(x: int) -> int:
            return target(x) * 2
        """,
    )
    _write(
        tmp_path,
        "tests/test_direct.py",
        """
        from pkg.core import target


        def test_target_direct():
            assert target(1) == 2
        """,
    )
    _write(
        tmp_path,
        "tests/test_indirect.py",
        """
        from pkg.core import helper


        def test_via_helper():
            assert helper(1) == 4
        """,
    )
    _write(
        tmp_path,
        "tests/conftest.py",
        """
        import pytest
        from pkg.core import target


        @pytest.fixture
        def target_fixture():
            return target(0)
        """,
    )
    _write(
        tmp_path,
        "tests/test_fixture.py",
        """
        def test_uses_fixture(target_fixture):
            assert target_fixture == 1
        """,
    )
    _write(
        tmp_path,
        "tests/test_imports_only.py",
        """
        from pkg import core


        def test_something_else():
            assert core.__name__ == "pkg.core"
        """,
    )
    _write(
        tmp_path,
        "tests/test_named.py",
        """
        def test_target_naming_only():
            assert True
        """,
    )
    _write(tmp_path, "tests/test_unrelated.py", "def test_unrelated():\n    pass\n")
    return tmp_path


@pytest.fixture
def handler(repo: Path) -> PythonHandler:
    return PythonHandler(hub=Hub(), roots={"r": repo})


def _measured_db(repo: Path, rel_file: str, lines: set[int], ctx: str) -> None:
    """Write a real per-test-context .coverage DB covering `lines`."""
    db = repo / ".coverage"
    data = coverage.CoverageData(basename=str(db))
    if db.exists():
        data.read()
    data.set_context(ctx)
    data.add_lines({str(repo / rel_file): lines})
    data.write()


# ---------------------------------------------------------------------------
# Static evidence
# ---------------------------------------------------------------------------


def test_tests_view_labels_each_evidence_tier(handler: PythonHandler) -> None:
    body = handler.get(id="r::pkg.core.target", view="tests").body
    # Direct static call from a test function.
    assert "tests/test_direct.py::test_target_direct" in body
    assert "[strong]" in body
    # Through a non-test helper (pkg.core.helper -> target).
    assert "tests/test_indirect.py::test_via_helper" in body
    assert "[medium]" in body
    # Fixture injection (no call edge; signature param match).
    assert "tests/test_fixture.py::test_uses_fixture" in body
    assert "fixture parameter `target_fixture`" in body
    # Imports-only module import.
    assert "tests/test_imports_only.py" in body
    assert "[low]" in body and "imports module" in body
    # Naming-only match.
    assert "tests/test_named.py::test_target_naming_only" in body
    assert "[lowest]" in body
    # No evidence at all.
    assert "test_unrelated" not in body


def test_tests_view_confidence_order(handler: PythonHandler) -> None:
    body = handler.get(id="r::pkg.core.target", view="tests").body
    rows = [line for line in body.splitlines() if "tests/test_" in line and "[" in line]
    order = [
        ("[strong]", "[medium]", "[low]", "[lowest]").index(
            next(t for t in ("[strong]", "[medium]", "[low]", "[lowest]") if t in line)
        )
        for line in rows
    ]
    assert order == sorted(order), body
    assert order[0] == 0  # strongest evidence first


def test_tests_view_reports_absent_coverage(handler: PythonHandler) -> None:
    body = handler.get(id="r::pkg.core.target", view="tests").body
    assert "coverage: absent" in body
    assert "static evidence only" in body
    assert "scripts/test --coverage" in body


def test_tests_view_prints_run_invocation(handler: PythonHandler) -> None:
    body = handler.get(id="r::pkg.core.target", view="tests").body
    assert "scripts/test tests/test_direct.py::test_target_direct" in body


def test_tests_view_hints_round_trip(handler: PythonHandler) -> None:
    body = handler.get(id="r::pkg.core.target", view="tests").body
    assert_hints_round_trip(body, lambda verb, kw: getattr(handler, verb)(**kw).body)


def test_tests_view_never_executes(
    handler: PythonHandler, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Navigation must not run tests/source: subprocesses are git-only."""
    real_run = subprocess.run

    def spy(cmd, *args, **kwargs):
        argv0 = cmd[0] if isinstance(cmd, (list, tuple)) else cmd
        # git is the provenance wrapper's separately-observed read; nothing
        # else (pytest, python, coverage) may be spawned by this view.
        assert argv0 == "git", f"unexpected subprocess: {cmd}"
        return real_run(cmd, *args, **kwargs)

    def _boom(*args, **kwargs):
        raise AssertionError("navigation executed code")

    monkeypatch.setattr(subprocess, "run", spy)
    monkeypatch.setattr(sys, "setprofile", _boom)
    body = handler.get(id="r::pkg.core.target", view="tests").body
    assert "test_target_direct" in body


def test_tests_view_file_id_and_depth_bound(handler: PythonHandler) -> None:
    # File id resolves to the module symbol: every member counts, so
    # test_via_helper's call into pkg.core.helper is a direct hit.
    body = handler.get(id="r/pkg/core.py", view="tests").body
    assert "test_target_direct" in body
    assert "test_via_helper" in body
    # depth=1 on the function must NOT reach test_via_helper (a 2-hop
    # path through pkg.core.helper); depth=2 does, labelled indirect.
    shallow = handler.get(id="r::pkg.core.target", view="tests", depth=1).body
    assert "test_via_helper" not in shallow
    deep = handler.get(id="r::pkg.core.target", view="tests", depth=2).body
    assert "tests/test_indirect.py::test_via_helper" in deep
    assert "[medium]" in deep


def test_tests_view_bounds(handler: PythonHandler) -> None:
    with pytest.raises(BadInput):
        handler.get(id="r::pkg.core.target", view="tests", depth=0)
    with pytest.raises(BadInput):
        handler.get(id="r::pkg.core.target", view="tests", depth=99)
    with pytest.raises(BadInput):
        handler.get(id="r::pkg.core.target", view="tests", max_results=0)


def test_tests_view_max_results_caps_rows(repo: Path) -> None:
    for i in range(8):
        _write(
            repo,
            f"tests/test_n{i}.py",
            f"from pkg.core import target\n\n\ndef test_n{i}():\n    assert target({i})\n",
        )
    handler = PythonHandler(hub=Hub(), roots={"r": repo})
    body = handler.get(id="r::pkg.core.target", view="tests", max_results=3).body
    assert "… (" in body and "more; raise max_results" in body
    assert len([ln for ln in body.splitlines() if "[strong]" in ln]) == 3


def test_tests_view_unknown_symbol(handler: PythonHandler) -> None:
    with pytest.raises(NotFound):
        handler.get(id="r::pkg.core.nope", view="tests")


def test_tests_view_bare_alias_rejected(handler: PythonHandler) -> None:
    with pytest.raises(BadInput):
        handler.get(id="r", view="tests")


# ---------------------------------------------------------------------------
# Measured coverage
# ---------------------------------------------------------------------------


def _cover_target_lines(repo: Path) -> set[int]:
    """Line range of `def target` in the fixture's pkg/core.py."""
    text = (repo / "pkg" / "core.py").read_text(encoding="utf-8")
    start = next(
        i for i, ln in enumerate(text.splitlines(), 1) if ln.startswith("def target")
    )
    end = (
        next(
            i
            for i, ln in enumerate(text.splitlines(), 1)
            if ln.startswith("def helper")
        )
        - 2
    )
    return set(range(start, end + 1))


def test_tests_view_measured_coverage_when_verified(repo: Path) -> None:
    handler = PythonHandler(hub=Hub(), roots={"r": repo})
    _measured_db(
        repo,
        "pkg/core.py",
        _cover_target_lines(repo),
        "tests/test_realcov.py::test_measured|run",
    )
    _measured_db(
        repo,
        "pkg/core.py",
        {1},
        "tests/test_direct.py::test_target_direct|run",
    )
    _load_manifest_mod().write_manifest(repo / ".coverage", repo)

    body = handler.get(id="r::pkg.core.target", view="tests").body
    assert "coverage: ok" in body
    assert "verified" in body
    assert "tests/test_realcov.py::test_measured" in body
    assert "[measured]" in body
    # Measured rows rank above static ones.
    rows = [ln for ln in body.splitlines() if "tests/test_" in ln and "[" in ln]
    assert "[measured]" in rows[0]


def test_tests_view_unverified_db_falls_back(repo: Path) -> None:
    """A .coverage with contexts but no manifest is not 'measured'."""
    handler = PythonHandler(hub=Hub(), roots={"r": repo})
    _measured_db(repo, "pkg/core.py", _cover_target_lines(repo), "tests/x.py::t|run")
    # No manifest written.
    body = handler.get(id="r::pkg.core.target", view="tests").body
    assert "coverage: manifest-missing" in body
    assert "[measured]" not in body
    assert "static evidence only" in body
    # Static fallback still reports the direct caller.
    assert "test_target_direct" in body


def test_tests_view_stale_manifest_falls_back(repo: Path) -> None:
    handler = PythonHandler(hub=Hub(), roots={"r": repo})
    _measured_db(repo, "pkg/core.py", _cover_target_lines(repo), "t.py::t|run")
    _load_manifest_mod().write_manifest(repo / ".coverage", repo)
    # Mutate the measured file so its indexed bytes no longer match.
    (repo / "pkg" / "core.py").write_text(
        (repo / "pkg" / "core.py").read_text(encoding="utf-8") + "\nX = 1\n",
        encoding="utf-8",
    )
    body = handler.get(id="r::pkg.core.target", view="tests").body
    assert "coverage: manifest-stale" in body
    assert "[measured]" not in body


def test_tests_view_contextless_db_says_so(repo: Path) -> None:
    handler = PythonHandler(hub=Hub(), roots={"r": repo})
    # A plain gate .coverage: measured lines, no per-test contexts.
    data = coverage.CoverageData(basename=str(repo / ".coverage"))
    data.add_lines({str(repo / "pkg" / "core.py"): _cover_target_lines(repo)})
    data.write()
    _load_manifest_mod().write_manifest(repo / ".coverage", repo)
    body = handler.get(id="r::pkg.core.target", view="tests").body
    assert "coverage: no-contexts" in body
    assert "[measured]" not in body


def test_manifest_writer(tmp_path: Path) -> None:
    """The manifest records sha256 of measured bytes + a UTC timestamp."""
    src = _write(tmp_path, "pkg/x.py", "X = 1\n")
    _measured_db(tmp_path, "pkg/x.py", {1}, "t::t|run")
    manifest = _load_manifest_mod().write_manifest(tmp_path / ".coverage", tmp_path)
    import hashlib

    # Manifest keys are the DB-recorded paths (absolute when the DB was
    # written without relative_files).
    key = next(k for k in manifest["files"] if k.endswith("pkg/x.py"))
    assert manifest["files"][key] == hashlib.sha256(src.read_bytes()).hexdigest()
    assert manifest["contexts"] == 1
    assert manifest["created_at"].endswith("Z")
    assert (
        json.loads((tmp_path / ".coverage.precis.json").read_text(encoding="utf-8"))[
            "version"
        ]
        == 1
    )
