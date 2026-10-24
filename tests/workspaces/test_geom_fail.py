"""gr514056 — a raise inside ``out.run`` still leaves the run record:
``trace.json`` with the error-marked frames and a ``manifest.json``
carrying ``status="failed"``; a previous good ``trace.json`` moves to
``trace.prev.json`` and stays the run diff's baseline."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from precis_geom import out as geom_out
from precis_geom import trace, trace_schema
from tests.workspaces._script import load_script

pytestmark = pytest.mark.filterwarnings("ignore")

#: ``build(rows)`` delegates to ``sheetnet.strip`` — ``rows=0`` raises
#: inside the traced call (gr513669's validation), a traced-frame error.
_SCRIPT = (
    "from hexfold.sheetnet import strip\n"
    "\n"
    "\n"
    "def build(rows):\n"
    "    strip(rows=rows, periods=3)\n"
)

#: A traced call succeeds, then user code raises bare — the exception
#: never crosses a ``@traced`` wrapper.
_RAISE_SCRIPT = (
    "from hexfold.sheetnet import strip\n"
    "\n"
    "\n"
    "def build():\n"
    "    strip(rows=4, periods=3)\n"
    "    raise ValueError('boom')\n"
)

#: A toplevel call fails — no user def frame anywhere in the chain.
_TOP_SCRIPT = "from hexfold.sheetnet import strip\n\n\nstrip(rows=0, periods=3)\n"


def _doc(out_dir: Path) -> dict:
    return trace_schema.loads((out_dir / "trace.json").read_text(encoding="utf-8"))


def _manifest(out_dir: Path) -> dict:
    return json.loads((out_dir / "manifest.json").read_text(encoding="utf-8"))


def _strip_made(out_dir: Path) -> dict:
    return next(
        f["made"] for f in _doc(out_dir)["frames"] if f["qual"].endswith("strip")
    )


def test_failure_leaves_trace_and_failed_manifest(tmp_path: Path) -> None:
    """A raise inside ``out.run`` propagates unchanged and still writes
    ``trace.json`` (the error-marked frames) + a ``status="failed"``
    manifest."""
    mod = load_script(tmp_path, _SCRIPT)
    out_dir = tmp_path / "out" / "x"
    with pytest.raises(ValueError, match="rows"):
        with geom_out.run(out_dir, work_root=tmp_path):
            mod.build(0)
    doc = _doc(out_dir)
    assert doc["run"]["status"] == "failed"
    marked = [f for f in doc["frames"] if "error" in f]
    assert len(marked) == 1
    assert marked[0]["error"] == "ValueError"
    assert marked[0]["qual"].endswith("strip")
    manifest = _manifest(out_dir)
    assert manifest["schema"] == 1 and manifest["status"] == "failed"
    assert manifest["error"]["type"] == "ValueError"
    assert "rows" in manifest["error"]["message"]
    assert manifest["error"]["at"]["file"] == "main.py"


def test_failure_preserves_the_previous_good_trace(tmp_path: Path) -> None:
    """The failed dump lands on ``trace.json`` only after a previous
    good one moves to ``trace.prev.json`` — never a silent clobber."""
    mod = load_script(tmp_path, _SCRIPT)
    out_dir = tmp_path / "out" / "x"
    with geom_out.run(out_dir, work_root=tmp_path):
        mod.build(8)
    good = (out_dir / "trace.json").read_text(encoding="utf-8")
    assert '"error"' not in good
    with pytest.raises(ValueError):
        with geom_out.run(out_dir, work_root=tmp_path):
            mod.build(0)
    assert (out_dir / "trace.prev.json").read_text(encoding="utf-8") == good
    assert any("error" in f for f in _doc(out_dir)["frames"])


def test_consecutive_failures_keep_the_last_good(tmp_path: Path) -> None:
    """A second crash must not promote a failed dump into
    ``trace.prev.json`` — it stays the last *good* run's."""
    mod = load_script(tmp_path, _SCRIPT)
    out_dir = tmp_path / "out" / "x"
    with geom_out.run(out_dir, work_root=tmp_path):
        mod.build(8)
    good = (out_dir / "trace.json").read_text(encoding="utf-8")
    for _ in range(2):
        with pytest.raises(ValueError):
            with geom_out.run(out_dir, work_root=tmp_path):
                mod.build(0)
    assert (out_dir / "trace.prev.json").read_text(encoding="utf-8") == good


def test_next_run_diffs_against_the_last_good(tmp_path: Path) -> None:
    """good → fail → good: the third run's diff compares against the
    first — ``was`` is the good run's ``made``, not the crashed run's
    truncated frame."""
    mod = load_script(tmp_path, _SCRIPT)
    out_dir = tmp_path / "out" / "x"
    with geom_out.run(out_dir, work_root=tmp_path):
        mod.build(8)
    good_made = _strip_made(out_dir)
    with pytest.raises(ValueError):
        with geom_out.run(out_dir, work_root=tmp_path):
            mod.build(0)
    with geom_out.run(out_dir, work_root=tmp_path) as r:
        mod.build(10)
    assert r.diff is not None
    assert r.diff["added"] == [] and r.diff["removed"] == []
    [changed] = [c for c in r.diff["changed"] if c["path"].endswith("sheetnet.strip")]
    assert changed["was"] == good_made
    assert changed["now"] == _strip_made(out_dir)


def test_failure_inside_a_live_tracer(tmp_path: Path) -> None:
    """``out.run`` nested in an outer ``trace.run`` uses the live
    tracer — the failure record is written the same way."""
    mod = load_script(tmp_path, _SCRIPT)
    out_dir = tmp_path / "out" / "x"
    with trace.run(work_root=tmp_path):
        with pytest.raises(ValueError):
            with geom_out.run(out_dir, work_root=tmp_path):
                mod.build(0)
    assert _manifest(out_dir)["status"] == "failed"
    assert any("error" in f for f in _doc(out_dir)["frames"])


def test_untraced_raise_marks_the_innermost_user_frame(tmp_path: Path) -> None:
    """A bare ``raise`` in the script never crossed a ``@traced``
    wrapper — ``fail()`` marks the innermost live (user) frame so the
    dump still names a culprit."""
    mod = load_script(tmp_path, _RAISE_SCRIPT)
    out_dir = tmp_path / "out" / "x"
    with pytest.raises(ValueError, match="boom"):
        with geom_out.run(out_dir, work_root=tmp_path):
            mod.build()
    marked = [f for f in _doc(out_dir)["frames"] if "error" in f]
    assert len(marked) == 1
    assert marked[0]["kind"] == "user"
    assert marked[0]["qual"] == "main.build"
    assert marked[0]["error"] == "ValueError"
    error = _manifest(out_dir)["error"]
    assert error["at"] == {"file": "main.py", "line": 6, "qual": "main.build"}
    assert "below" not in error


def test_error_anchor_is_the_nearest_user_frame(tmp_path: Path) -> None:
    """gr513668's rendered-error rule for the host loop: the manifest
    anchors the failure at the user def's call site — never a library
    file — and lists the library frames collapsed below."""
    mod = load_script(tmp_path, _SCRIPT)
    out_dir = tmp_path / "out" / "x"
    with pytest.raises(ValueError):
        with geom_out.run(out_dir, work_root=tmp_path):
            mod.build(0)
    error = _manifest(out_dir)["error"]
    at = error["at"]
    assert at["qual"] == "main.build"
    assert at["file"] == "main.py"
    assert at["line"] == 5  # the strip() call line inside build()
    assert "sheetnet" not in at["file"]
    assert [f["qual"] for f in error["below"]] == ["sheetnet.strip"]


def test_error_anchor_module_level_script(tmp_path: Path) -> None:
    """No user def anywhere — a toplevel failure anchors at the
    script's own call line (a ``<module>`` frame is a legal citation,
    just not a ``kind=user`` one) and the library frame stays below."""
    out_dir = tmp_path / "out" / "x"
    with pytest.raises(ValueError):
        with geom_out.run(out_dir, work_root=tmp_path):
            load_script(tmp_path, _TOP_SCRIPT)
    error = _manifest(out_dir)["error"]
    assert error["at"]["file"] == "main.py"
    assert error["at"]["line"] == 4
    assert "qual" not in error["at"]
    assert [f["qual"] for f in error["below"]] == ["sheetnet.strip"]


def test_user_anchor_public_seam(tmp_path: Path) -> None:
    """gr514209 — ``Tracer.user_anchor(tb)`` is the public form of the
    frame walk ``Run.fail`` needs: the traceback's innermost frame
    under ``work_root`` (``qual`` present on a named def, absent at
    ``<module>``), plus the deepest frame's site as the last-resort
    pointer."""
    mod = load_script(tmp_path, _SCRIPT)
    with trace.run(work_root=tmp_path) as tr:
        try:
            mod.build(0)
        except ValueError as exc:
            tb = exc.__traceback__
        else:
            raise AssertionError("build(0) did not raise")
    site, deepest = tr.user_anchor(tb)
    assert site == {"file": "main.py", "line": 5, "qual": "main.build"}
    assert deepest is not None and deepest["file"] == "hexfold.sheetnet"
    # A toplevel raise: the ``<module>`` frame is a legal site but
    # carries no ``qual``.
    with trace.run(work_root=tmp_path) as tr3:
        try:
            exec(compile(_TOP_SCRIPT, str(tmp_path / "main.py"), "exec"))
        except ValueError as exc:
            tb3 = exc.__traceback__
        else:
            raise AssertionError("the toplevel strip call did not raise")
    site3, _ = tr3.user_anchor(tb3)
    assert site3 == {"file": "main.py", "line": 4}
    assert "qual" not in site3


def test_failure_echoes_a_stderr_line(tmp_path: Path, capsys) -> None:
    """The failure surfaces where the script author looks — one
    stderr line naming the trace path, the error type and the
    user-code site."""
    mod = load_script(tmp_path, _SCRIPT)
    out_dir = tmp_path / "out" / "x"
    with pytest.raises(ValueError):
        with geom_out.run(out_dir, work_root=tmp_path):
            mod.build(0)
    err = capsys.readouterr().err
    assert f"out.run: failed -> {out_dir}/trace.json" in err
    assert "(error=ValueError at main.py:5)" in err
