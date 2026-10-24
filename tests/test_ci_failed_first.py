"""``scripts/ci-failed-first`` — the release gate's failed_ids plan input.

Pins the junit -> node-id extraction (the ``failed-first`` job re-runs
exactly these), the complete-shard-artifact rule, and the cap that sends an
over-large failure set back to the full matrix. Release train spec §3.
"""

from __future__ import annotations

import importlib.util
import io
import json
import shutil
import tempfile
import zipfile
from pathlib import Path
from types import ModuleType

import pytest

SCRIPT = Path(__file__).parent.parent / "scripts" / "ci-failed-first"


def _load() -> ModuleType:
    # `.py` copy: testmon IndexErrors on dotless files (see test_last_gated_main_sha).
    tmp = Path(tempfile.mkdtemp(prefix="ci_failed_first_")) / "ci_failed_first.py"
    shutil.copyfile(SCRIPT, tmp)
    spec = importlib.util.spec_from_file_location("ci_failed_first", tmp)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


_MOD = _load()

JUNIT = """<?xml version="1.0" encoding="utf-8"?>
<testsuite name="pytest" tests="4" failures="1" errors="1" skipped="1">
  <testcase classname="tests.test_foo" name="test_ok" time="0.1"/>
  <testcase classname="tests.test_foo" name="test_bad" time="0.1">
    <failure message="assert 1 == 2">boom</failure>
  </testcase>
  <testcase classname="tests.test_foo.TestThing" name="test_err[p 1]" time="0.2">
    <error message="fixture failed">oops</error>
  </testcase>
  <testcase classname="tests.test_foo" name="test_skip" time="0.0">
    <skipped message="db"/>
  </testcase>
</testsuite>
"""


@pytest.fixture()
def root(tmp_path: Path) -> Path:
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_foo.py").write_text("", encoding="utf-8")
    (tmp_path / "tests" / "test_bar.py").write_text("", encoding="utf-8")
    return tmp_path


def test_node_ids_extracts_failed_and_errored(root: Path) -> None:
    ids = _MOD.node_ids(JUNIT, root)
    assert "tests/test_foo.py::test_bad" in ids
    # the CamelCase tail resolves to the class, not a module
    assert "tests/test_foo.py::TestThing::test_err[p 1]" in ids
    assert len(ids) == 2  # passing and skipped testcases donate nothing


def test_node_ids_drops_ids_whose_file_is_gone(root: Path) -> None:
    """A deleted/renamed file makes the id stale: emit nothing for it rather
    than exit-4 the failed-first job forever (it uploads no junit, so the
    next push would re-derive the same stale id)."""
    xml = JUNIT.replace("tests.test_foo.TestThing", "tests.test_gone.TestOld")
    ids = _MOD.node_ids(xml, root)
    assert not any("test_gone" in i for i in ids)
    assert "tests/test_foo.py::test_bad" in ids


def test_complete_shard_count() -> None:
    assert _MOD.complete_shard_count([f"junit-10-{k}" for k in range(1, 11)]) == 10
    assert _MOD.complete_shard_count(["junit-10-1", "junit-10-2"]) is None
    # foreign artifacts do not confuse the grouping
    assert _MOD.complete_shard_count(["precis-coverage-db", "junit-12-7"]) is None


def test_emit_caps_at_200() -> None:
    ids = [f"tests/test_x.py::test_{i}" for i in range(_MOD.CAP + 1)]
    assert _MOD.emit(ids) == ""
    assert json.loads(_MOD.emit(ids[: _MOD.CAP])) == ids[: _MOD.CAP]
    assert _MOD.emit([]) == ""


def _zip(xml: str) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("junit.xml", xml)
    return buf.getvalue()


def test_failed_ids_walks_newest_red_run(
    root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Newest completed red run on the branch with a complete junit set;
    an older red with a hole (timed-out shard) is skipped over."""
    calls: list[str] = []

    def fake_gh(*args: str, timeout: int = 60) -> str | None:
        calls.append(args[0])
        if "runs?branch=" in args[0]:
            return "1002\n1001"
        if "runs/1002/artifacts" in args[0]:
            return "1\tjunit-10-1\n2\tjunit-10-2\n3\tjunit-10-3"
        if "runs/1001/artifacts" in args[0]:
            return "9\tjunit-2-1\n8\tjunit-2-2"
        return None

    dup = JUNIT.replace("test_bad", "test_bad_dup")  # shard 2 of the old run
    zips = {9: _zip(JUNIT), 8: _zip(dup)}
    monkeypatch.setattr(_MOD, "_gh", fake_gh)
    monkeypatch.setattr(
        _MOD, "_gh_zip", lambda path, timeout=120: zips.get(int(path.rsplit("/", 2)[1]))
    )
    ids = _MOD.failed_ids("o/r", "release/r9", root)
    assert sorted(ids) == [
        "tests/test_foo.py::TestThing::test_err[p 1]",
        "tests/test_foo.py::test_bad",
        "tests/test_foo.py::test_bad_dup",
    ]
    assert "runs/1002/artifacts" in calls[1]  # newest tried first, has a hole


def test_failed_ids_empty_without_a_red_run(
    root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(_MOD, "_gh", lambda *a, timeout=60: "")
    assert _MOD.failed_ids("o/r", "release/r9", root) == []
