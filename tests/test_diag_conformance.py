"""Adapter conformance (shared-core-layers S1a): every registered
adapter turns a real domain failure into a valid anchored Diag.

``ADAPTERS`` maps adapter name → a fixture callable
``tmp_path -> (RunResult, trace_doc)``: run the real failure path,
adapt its manifest + trace, hand back both so the suite can assert the
Diag's anchor names the user-facing traced op and its script call site,
with inner frames in the chain (gr522025). ``cad-run-failure``
(``precis_geom.run_diag``) is the first registrant; S1b adds the pcb
adapter, S2 the hex/structure one.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from precis_diag import (
    FrameSite,
    RunResult,
    Status,
    diag_from_dict,
    diag_to_dict,
    render,
)
from precis_geom import out as geom_out
from precis_geom import run_diag, trace_schema
from tests.workspaces._script import load_script

pytestmark = pytest.mark.filterwarnings("ignore")

#: ``build(rows)`` delegates to ``sheetnet.strip`` — ``rows=0`` raises
#: inside the traced call, the same fixture ``test_geom_fail`` uses.
_SCRIPT = (
    "from hexfold.sheetnet import strip\n"
    "\n"
    "\n"
    "def build(rows):\n"
    "    strip(rows=rows, periods=3)\n"
)


def _cad_run_failure(tmp_path: Path) -> tuple[RunResult, dict]:
    """A real ``out.run`` failure: script raise → failed manifest +
    error-marked trace → ``failed_run_result``."""
    mod = load_script(tmp_path, _SCRIPT)
    out_dir = tmp_path / "out" / "x"
    with pytest.raises(ValueError), geom_out.run(out_dir, work_root=tmp_path):
        mod.build(0)
    manifest = json.loads((out_dir / "manifest.json").read_text(encoding="utf-8"))
    trace_doc = trace_schema.loads((out_dir / "trace.json").read_text(encoding="utf-8"))
    return (
        run_diag.failed_run_result(
            manifest, trace_doc, run=str(out_dir / "trace.json")
        ),
        trace_doc,
    )


#: name → ``tmp_path -> (RunResult, trace_doc)``.
ADAPTERS = {"cad-run-failure": _cad_run_failure}


@pytest.mark.parametrize("name", sorted(ADAPTERS))
def test_failure_yields_one_anchored_error(name: str, tmp_path: Path) -> None:
    """gr522025 acceptance: one error Diag whose ``frame_key`` and
    ``file:line`` match the error-marked frame in ``trace.json``."""
    rr, doc = ADAPTERS[name](tmp_path)
    assert isinstance(rr, RunResult)
    assert not rr.ok
    assert Path(rr.run).name == "trace.json"
    assert Path(rr.run).is_file()
    [d] = [d for d in rr.diags if d.status is Status.ERROR]
    assert d.code == "cad.run-failed"
    marked = [f for f in doc["frames"] if f.get("error")]
    assert len(marked) == 1
    f = marked[0]
    assert d.anchor.frame_key == f["key"]
    assert d.anchor.file == f["file"]
    assert d.anchor.line == f["line"]
    assert d.anchor.file == "main.py"


@pytest.mark.parametrize("name", sorted(ADAPTERS))
def test_diag_is_wire_valid(name: str, tmp_path: Path) -> None:
    """The Diag survives its own dict round trip and renders — the
    shape an MCP error path ships."""
    rr, _doc = ADAPTERS[name](tmp_path)
    d = rr.diags[0]
    assert diag_from_dict(diag_to_dict(d)) == d
    text = render(rr.diags)
    assert "run failed" in text
    assert f"frame {d.anchor.frame_key}" in text


def test_run_fail_stashes_the_result(tmp_path: Path) -> None:
    """``Run.fail`` builds the same RunResult the adapter does and
    stashes it on the run — ``run.result`` is the programmatic channel."""
    mod = load_script(tmp_path, _SCRIPT)
    out_dir = tmp_path / "out" / "x"
    with pytest.raises(ValueError), geom_out.run(out_dir, work_root=tmp_path) as o:
        mod.build(0)
    assert isinstance(o.result, RunResult)
    assert o.result.run == str(out_dir / "trace.json")
    d = o.result.diags[0]
    assert d.anchor.frame_key
    # The manifest record carries the frame key too (``error.frame``).
    manifest = json.loads((out_dir / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["error"]["frame"] == d.anchor.frame_key
    trace_doc = trace_schema.loads((out_dir / "trace.json").read_text(encoding="utf-8"))
    assert o.result == run_diag.failed_run_result(
        manifest, trace_doc, run=str(out_dir / "trace.json")
    )


def test_no_trace_leaves_an_unanchored_diag(tmp_path: Path) -> None:
    """A failed manifest without a trace still yields the prose Diag —
    the anchor is empty rather than the adapter refusing."""
    manifest = {
        "status": "failed",
        "error": {"type": "ValueError", "message": "rows must be >= 1"},
    }
    rr = run_diag.failed_run_result(manifest, None)
    assert not rr.ok
    d = rr.diags[0]
    assert d.message == "manifest.json: run failed — ValueError: rows must be >= 1"
    assert d.anchor.frame_key == "" and d.anchor.file == ""


def _script_failure(tmp_path: Path, script: str) -> tuple[RunResult, dict, dict]:
    mod = load_script(tmp_path, script)
    out_dir = tmp_path / "out" / "x"
    with pytest.raises(ValueError), geom_out.run(out_dir, work_root=tmp_path) as run:
        mod.build()
    manifest = json.loads((out_dir / "manifest.json").read_text(encoding="utf-8"))
    doc = trace_schema.loads((out_dir / "trace.json").read_text(encoding="utf-8"))
    assert run.result is not None
    return run.result, manifest, doc


def test_nested_failure_anchors_the_user_facing_op(tmp_path: Path) -> None:
    rr, manifest, doc = _script_failure(
        tmp_path,
        "from tests.workspaces.diag_ops import outer\n\n\ndef build():\n    outer(1)\n",
    )
    outer = next(f for f in doc["frames"] if f["qual"].endswith(".outer"))
    inner = next(f for f in doc["frames"] if f["qual"].endswith(".inner"))
    assert inner["error"] == "ValueError"
    [diag] = rr.diags
    assert diag.anchor.frame_key == outer["key"]
    assert (diag.anchor.file, diag.anchor.line) == ("main.py", 5)
    assert diag.anchor.chain == (
        FrameSite(frame_key=inner["key"], file=inner["file"], line=inner["line"]),
    )
    assert manifest["error"]["frame"] == outer["key"]
    assert diag_from_dict(diag_to_dict(diag)) == diag


@pytest.mark.parametrize("traced_first", [False, True])
def test_untraced_raise_uses_manifest_site(tmp_path: Path, traced_first: bool) -> None:
    """Before any trace, and after a successful trace, cite the raise line."""
    script = (
        "from tests.workspaces.diag_ops import inner\n"
        "\n"
        "\n"
        "def helper():\n"
        "    raise ValueError('boom')\n"
        "\n"
        "\n"
        "def build():\n"
    )
    if traced_first:
        script += "    inner(5)\n"
    script += "    helper()\n"
    rr, manifest, doc = _script_failure(tmp_path, script)
    at = manifest["error"]["at"]
    assert (at["file"], at["line"]) == ("main.py", 5)
    [diag] = rr.diags
    assert (diag.anchor.file, diag.anchor.line) == (at["file"], at["line"])
    assert diag.anchor.frame_key == ""
    assert diag.anchor.chain == ()
    if traced_first:
        marked = [f for f in doc["frames"] if f.get("error")]
        assert len(marked) == 1 and marked[0]["kind"] == "user"
        assert marked[0]["line"] != at["line"]
    else:
        assert not any(f.get("error") for f in doc["frames"])


def test_raise_after_traced_call_uses_raise_line(tmp_path: Path) -> None:
    rr, manifest, _doc = _script_failure(
        tmp_path,
        "from tests.workspaces.diag_ops import inner\n"
        "\n"
        "\n"
        "def build():\n"
        "    inner(5)\n"
        "    raise ValueError('boom')\n",
    )
    assert manifest["error"]["at"]["line"] == 6
    assert (rr.diags[0].anchor.file, rr.diags[0].anchor.line) == ("main.py", 6)
    assert rr.diags[0].anchor.chain == ()


def test_missing_trace_uses_manifest_site() -> None:
    manifest = {
        "status": "failed",
        "error": {"type": "ValueError", "at": {"file": "main.py", "line": 9}},
    }
    [diag] = run_diag.failed_run_result(manifest, None).diags
    assert (diag.anchor.file, diag.anchor.line) == ("main.py", 9)
