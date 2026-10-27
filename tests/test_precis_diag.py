"""``precis_diag`` — core types, the Finding round trip, the renderer
golden, and the ``schema_version`` reader rule (accept older, refuse
newer with a next step)."""

from __future__ import annotations

import pytest

import precis_diag
from hexfold.diag import finding_from_diag
from precis_diag import Anchor, Diag, FrameSite, RunResult, Status, render


def test_render_golden() -> None:
    """The one text renderer: status-first sort, the anchor's
    ``file:line (frame …)`` site, ``next:``/``refs:`` continuations."""
    diags = [
        Diag(status=Status.INFO, code="geom.summary", message="24 atoms"),
        Diag(
            status=Status.ERROR,
            code="cad.run",
            message="manifest.json: run failed — ValueError: rows must be >= 1",
            anchor=Anchor(frame_key="ab12cd34ef56", file="main.py", line=30),
            next="fix the script and re-run",
            refs=("block:tube",),
        ),
        Diag(
            status=Status.WARN,
            code="geom.clearance",
            message="no clearance rule for ('C','H')",
            where="sheet0",
        ),
        Diag(
            status=Status.NOT_CHECKED, code="drc.clearance", message="no board loaded"
        ),
    ]
    assert render(diags) == (
        "      error cad.run (main.py:30, frame ab12cd34ef56) "
        "manifest.json: run failed — ValueError: rows must be >= 1\n"
        "            next: fix the script and re-run\n"
        "            refs: block:tube\n"
        "       warn geom.clearance sheet0 no clearance rule for ('C','H')\n"
        "not_checked drc.clearance no board loaded\n"
        "       info geom.summary 24 atoms"
    )
    assert render([]) == "ok"


def test_finding_round_trip() -> None:
    """``finding_from_diag(diag_from_finding(f)) == f`` for every
    Finding field — Diag is the superset."""
    from hexfold.report import Finding, Severity

    f = Finding(
        code="geom.clash",
        severity=Severity.WARN,
        message="atoms closer than the bar",
        where="sheet0",
        span=(12, 4),
        data=(("measured", 1.79), ("expected", 1.8)),
        fix="widen the junction",
    )
    d = precis_diag.diag_from_finding(f)
    assert d.status is Status.WARN
    assert d.next == "widen the junction"
    assert d.anchor == Anchor()  # findings carry no frame anchor
    assert finding_from_diag(d) == f
    for sev in Severity:
        g = Finding(code="geom.check", severity=sev, message="m")
        assert finding_from_diag(precis_diag.diag_from_finding(g)) == g


def test_finding_from_diag_refuses_status_without_severity() -> None:
    for status in (Status.OK, Status.NOT_CHECKED):
        with pytest.raises(ValueError, match="no hexfold Severity"):
            finding_from_diag(Diag(status=status, code="geom.check", message="x"))


def test_status_mapping_table() -> None:
    """The docstring table: only info/warn/error have Finding or
    string-severity counterparts; ok and not_checked do not."""
    assert precis_diag._STATUS_FROM_SEVERITY_NAME == {
        "INFO": Status.INFO,
        "WARN": Status.WARN,
        "ERROR": Status.ERROR,
    }
    for status in (Status.INFO, Status.WARN, Status.ERROR):
        diag = Diag(status=status, code="geom.check", message="m")
        assert finding_from_diag(diag).severity.name == status.name


def test_dict_round_trip() -> None:
    d = Diag(
        status=Status.ERROR,
        message="run failed",
        code="cad.run",
        anchor=Anchor(frame_key="k", file="main.py", line=3),
        refs=("a", "b"),
        next="re-run",
        data=(("measured", 1.0),),
        where="w",
        span=(1, 2),
    )
    assert precis_diag.diag_from_dict(d.to_dict()) == d


def test_anchor_chain_round_trip_and_legacy_default() -> None:
    d = Diag(
        status=Status.ERROR,
        message="nested failure",
        code="cad.run-failed",
        anchor=Anchor(
            frame_key="outer",
            file="main.py",
            line=5,
            chain=(
                FrameSite("middle", "ops.py", 10),
                FrameSite("inner", "ops.py", 20),
            ),
        ),
    )
    raw = d.to_dict()
    assert raw["schema_version"] == 1
    assert raw["anchor"]["chain"] == [
        {"frame_key": "middle", "file": "ops.py", "line": 10},
        {"frame_key": "inner", "file": "ops.py", "line": 20},
    ]
    assert precis_diag.diag_from_dict(raw) == d
    del raw["anchor"]["chain"]
    assert precis_diag.diag_from_dict(raw).anchor.chain == ()


@pytest.mark.parametrize("chain", [{}, "inner", [None]])
def test_reader_refuses_malformed_chain(chain: object) -> None:
    with pytest.raises(ValueError, match="anchor.chain"):
        precis_diag.diag_from_dict({"status": "error", "anchor": {"chain": chain}})


def test_reader_accepts_older_refuses_newer() -> None:
    d = Diag(status=Status.OK, code="geom.check", message="clean").to_dict()
    assert precis_diag.diag_from_dict(d).status is Status.OK
    newer = {**d, "schema_version": precis_diag.SCHEMA_VERSION + 1}
    with pytest.raises(ValueError, match="newer.*upgrade precis-geom"):
        precis_diag.diag_from_dict(newer)
    with pytest.raises(ValueError, match="not in"):
        precis_diag.diag_from_dict({**d, "schema_version": 0})


def test_reader_preserves_original_schema_version(monkeypatch) -> None:
    monkeypatch.setattr(precis_diag, "SCHEMA_VERSIONS", frozenset({0, 1}))
    raw = {
        "schema_version": 0,
        "status": "info",
        "code": "geom.summary",
        "message": "older record",
    }
    diag = precis_diag.diag_from_dict(raw)
    assert diag.schema_version == 0
    assert diag.to_dict() == raw


@pytest.mark.parametrize(
    "code", ["", "run-failed", "CAD.run-failed", ".run", "cad.", "cad.run failed"]
)
def test_diag_refuses_unnamespaced_code(code: str) -> None:
    with pytest.raises(ValueError, match="lowercase domain"):
        Diag(status=Status.ERROR, code=code, message="bad code")
    with pytest.raises(ValueError, match="lowercase domain"):
        precis_diag.diag_from_dict({"status": "error", "code": code})


@pytest.mark.parametrize(
    "code", ["cad.run-failed", "geom.clash", "place.mirror_refused"]
)
def test_diag_accepts_namespaced_code(code: str) -> None:
    diag = Diag(status=Status.ERROR, code=code, message="valid code")
    assert precis_diag.diag_from_dict(diag.to_dict()) == diag


def test_run_result_round_trip_and_ok() -> None:
    rr = RunResult(
        diags=(Diag(status=Status.ERROR, code="cad.run-failed", message="boom"),),
        run="scratch/out/x/trace.json",
    )
    assert not rr.ok
    assert precis_diag.run_result_from_dict(rr.to_dict()) == rr
    assert RunResult().ok


def test_precis_diag_is_stdlib_only() -> None:
    """Every imported top-level module is stdlib or precis_diag itself,
    including imports nested in functions or conditional blocks."""
    import ast
    import sys
    from pathlib import Path

    root = Path(precis_diag.__file__).parent
    for path in root.rglob("*.py"):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                if isinstance(node, ast.ImportFrom) and node.level:
                    continue  # relative imports stay within precis_diag
                mods = (
                    [a.name for a in node.names]
                    if isinstance(node, ast.Import)
                    else [node.module or ""]
                )
                for mod in mods:
                    assert mod.split(".")[0] in sys.stdlib_module_names | {
                        "precis_diag"
                    }, f"{path.name}: import of {mod}"
