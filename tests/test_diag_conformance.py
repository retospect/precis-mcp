"""Adapter conformance (shared-core-layers S1a): every registered
adapter turns a real domain failure into a valid anchored Diag.

``ADAPTERS`` maps adapter name → ``(fixture, error_code)``: the fixture
``tmp_path -> (RunResult, trace_doc)`` runs the real failure path,
adapts its manifest + trace, hands back both so the suite can assert the
Diag's anchor names the user-facing traced op and its script call site,
with inner frames in the chain (gr522025); ``error_code`` is the
adapter's own record code. ``cad-run-failure``
(``precis_geom.run_diag``) is the first registrant, ``hex-run-failure``
the S2 one (gr522031) — a report-carrying failure yields one Diag per
finding instead of the run-level code. ``pcb-run-failure`` is the S1b
one (gr522027) — the builder stamps ``diag_code``/``diag_refs`` on its
raises, so the run-failed Diag carries the domain's own code and the
entity refs.
"""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from precis_diag import (
    FrameSite,
    RunResult,
    Status,
    diag_from_dict,
    diag_to_dict,
    probe,
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

#: ``build_net()`` compiles a .hx spec with one deliberate bad feature —
#: the fuse endpoint ``nope.in`` resolves to nothing, so the traced
#: ``hexfold.build`` raises ``BuildError`` whose Report carries exactly
#: one ERROR finding (``port.unknown``, spec line 4 — gr522031).
_HEX_SCRIPT = (
    "from hexfold.build import build\n"
    "\n"
    "\n"
    "def build_net():\n"
    '    build("""hexfold 0.2\n'
    "origin s\n"
    "s: sheet(40,40) - hex(2)@(20,20,A):0\n"
    "s.hole --fuse k=0--> nope.in\n"
    '""", strict=True)\n'
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


def _hex_run_failure(tmp_path: Path) -> tuple[RunResult, dict]:
    """A real hexfold spec failure under ``out.run``: the traced
    ``build`` raises ``BuildError``, its serialized Report rides
    ``manifest.error.report``, and the adapter turns the findings into
    anchored Diags (gr522031)."""
    from hexfold.report import BuildError

    mod = load_script(tmp_path, _HEX_SCRIPT)
    out_dir = tmp_path / "out" / "x"
    with pytest.raises(BuildError), geom_out.run(out_dir, work_root=tmp_path):
        mod.build_net()
    manifest = json.loads((out_dir / "manifest.json").read_text(encoding="utf-8"))
    trace_doc = trace_schema.loads((out_dir / "trace.json").read_text(encoding="utf-8"))
    return (
        run_diag.failed_run_result(
            manifest, trace_doc, run=str(out_dir / "trace.json")
        ),
        trace_doc,
    )


#: ``build()`` reuses refdes ``U1`` — the second traced ``b.part``
#: raises ``PcbBuilderError`` carrying ``diag_refs``/``diag_code``,
#: which ``_error_entry`` serializes and the adapter folds into the
#: run-failed Diag (gr522027).
_PCB_SCRIPT = (
    "import precis_geom.pcb as pcb\n"
    "\n"
    "\n"
    "def build():\n"
    "    b = pcb.Board('x')\n"
    "    b.part('U1')\n"
    "    b.part('U1')\n"
)


def _pcb_run_failure(tmp_path: Path) -> tuple[RunResult, dict]:
    """A real board-script failure under ``out.run``: the stamped
    ``diag_refs``/``diag_code`` ride ``manifest.error`` into the
    run-failed Diag — ``pcb.builder-error`` with a ``part:`` ref."""
    import precis_geom.pcb as pcb
    from precis.pcb import diag as pcb_diag

    mod = load_script(tmp_path, _PCB_SCRIPT)
    out_dir = tmp_path / "out" / "x"
    with pytest.raises(pcb.PcbBuilderError), geom_out.run(out_dir, work_root=tmp_path):
        mod.build()
    manifest = json.loads((out_dir / "manifest.json").read_text(encoding="utf-8"))
    trace_doc = trace_schema.loads((out_dir / "trace.json").read_text(encoding="utf-8"))
    return (
        pcb_diag.failed_run_result(
            manifest, trace_doc, run=str(out_dir / "trace.json")
        ),
        trace_doc,
    )


#: name → ``(tmp_path -> (RunResult, trace_doc), expected error code)``.
ADAPTERS = {
    "cad-run-failure": (_cad_run_failure, "cad.run-failed"),
    "hex-run-failure": (_hex_run_failure, "port.unknown"),
    "pcb-run-failure": (_pcb_run_failure, "pcb.builder-error"),
}


# ── probes (S1c) ─────────────────────────────────────────────────────────
#
# ``PROBES`` maps adapter name → ``(tmp_path -> (callable, kind))``: the
# fixture drives the domain's real probe path and returns a zero-arg
# callable producing the ``{"count", "digest", "sample"}`` mapping plus
# the entity kind the digest must name. The contract every registered
# adapter meets: count + digest + sample, ``count == digest["count"]``,
# and a non-empty kind.


def _drop_resolvers(domain: str) -> None:
    """Drop one domain from the probe registry — test teardown only."""
    import sys

    mod = sys.modules["precis_diag.probe"]
    mod._REGISTRY[:] = [r for r in mod._REGISTRY if r.domain != domain]


class _ToyProbeResolver:
    """The registry-shape stand-in until each domain adapter lands: a
    resolver registered for the duration of the toy fixture only."""

    domain = "toy"

    def can_resolve(self, selector: Any) -> bool:
        return selector == "toy:pins"

    def resolve(self, selector: Any) -> list[str]:
        return ["U3.1", "U3.2", "U3.3"]

    def digest(self, entities: list[str]) -> dict[str, Any]:
        return {"count": len(entities), "kind": "pins", "ids": list(entities)}


def _toy_probe(tmp_path: Path) -> tuple[Any, str]:
    from precis_diag import register_probe

    register_probe(_ToyProbeResolver())
    return (lambda: probe("toy:pins"), "pins")


#: ``build(o)`` blocks a three-atom scene and probes it — the CAD slice's
#: script-time probe path (gr522026).
_CAD_PROBE_SCRIPT = (
    "import numpy as np\n"
    "from precis.structure.cell import Cell\n"
    "from precis.structure.scene import Atom, Scene\n"
    "\n"
    "\n"
    "def build(o):\n"
    "    cell = Cell(np.eye(3) * 50.0, (False, False, False))\n"
    "    atoms = {}\n"
    "    for i, cart in enumerate([(0, 0, 0), (10, 0, 0), (0, 10, 5)]):\n"
    "        la = f'aC{i + 1}'\n"
    "        atoms[la] = Atom(label=la, element='C', frac=np.asarray(cart) / 50.0)\n"
    "    scene = Scene(cell=cell, atoms=atoms)\n"
    "    o.block('triad', scene)\n"
    "    o.probe(scene, label='triad')\n"
)


def _cad_probe(tmp_path: Path) -> tuple[Any, str]:
    """The real CAD path: a script probes its scene inside ``out.run``
    (printing the line + recording on the calling frame), and the same
    registered resolver answers the contract check for a fresh scene."""
    import numpy as np

    from precis.structure.cell import Cell
    from precis.structure.scene import Atom, Scene

    mod = load_script(tmp_path, _CAD_PROBE_SCRIPT)
    out_dir = tmp_path / "out" / "x"
    with geom_out.run(out_dir, work_root=tmp_path) as o:
        mod.build(o)
    doc = trace_schema.loads((out_dir / "trace.json").read_text(encoding="utf-8"))
    frame = next(f for f in doc["frames"] if f.get("probes"))
    (record,) = frame["probes"]

    cell = Cell(np.eye(3) * 50.0, (False, False, False))
    atoms = {
        f"aC{i + 1}": Atom(label=f"aC{i + 1}", element="C", frac=np.asarray(c) / 50.0)
        for i, c in enumerate([(0, 0, 0), (10, 0, 0), (0, 10, 5)])
    }
    scene = Scene(cell=cell, atoms=atoms)

    def run_probe() -> dict[str, Any]:
        result = probe(scene, label="triad")
        # the dispatch and the script-time record agree
        assert result["count"] == record["count"]
        return result

    return run_probe, "atoms"


def _pcb_board() -> Any:
    """A tiny two-part board: U3 declares 3 pins, LED1 ties two nets."""
    import precis_geom.pcb as geom_pcb

    b = geom_pcb.Board("probe-demo")
    b.part("U3").declare(1, 2, 3)
    led = b.part("LED1")
    led.declare(1, 2)
    b.net("LED_A").tie(led[1])
    b.net("LED_K").tie(led[2])
    return b


def _pcb_probe_pads(tmp_path: Path) -> tuple[Any, str]:
    """The real pcb path: ``pcb.local.probe`` resolves pads_of('U3')
    against the batch with no op run (gr522028)."""
    from precis.pcb import local as pcb_local

    board = _pcb_board()

    def run_probe() -> dict[str, Any]:
        return pcb_local.probe(board, "pads_of('U3')")

    return run_probe, "pads"


def _pcb_probe_nets(tmp_path: Path) -> tuple[Any, str]:
    from precis.pcb.probe import resolver_for

    board = _pcb_board()

    def run_probe() -> dict[str, Any]:
        return probe("nets:LED_*", resolver=resolver_for(board))

    return run_probe, "nets"


def _hex_probe(tmp_path: Path) -> tuple[Any, str]:
    """The real hex path: ``hexfold.probe.probe`` resolves an
    ``atoms:<inst>[…]`` selector against a built net — no op runs
    (gr522032, shared-core-layers S2)."""
    from hexfold.build import build
    from hexfold.probe import probe as hex_probe

    net = build("hexfold 0.2\norigin t\nt: tube(5,0, len=3)\n")

    def run_probe() -> dict[str, Any]:
        return hex_probe(net, "atoms:t[0,3,5-9]")

    return run_probe, "atoms"


PROBES = {
    "cad-pg-object": (_cad_probe, "atoms"),
    "hex-atom-set": (_hex_probe, "atoms"),
    "pcb-net-pattern": (_pcb_probe_nets, "nets"),
    "pcb-pads-of": (_pcb_probe_pads, "pads"),
    "toy-adapter": (_toy_probe, "pins"),
}


@pytest.mark.parametrize("name", sorted(PROBES))
def test_probe_returns_count_digest_sample(name: str, tmp_path: Path) -> None:
    """S1c conformance: every registered adapter's probe answers count +
    digest + sample; the digest's own count agrees and names its kind."""
    fixture, kind = PROBES[name]
    run_probe, expected_kind = fixture(tmp_path)
    try:
        result = run_probe()
    finally:
        _drop_resolvers("toy")
    assert set(result) == {"count", "digest", "sample"}
    assert result["count"] == result["digest"]["count"]
    assert result["digest"]["kind"] == expected_kind
    assert result["digest"]["count"] > 0
    assert "ids" in result["digest"] or "bbox" in result["digest"]


@pytest.mark.parametrize("name", sorted(ADAPTERS))
def test_failure_yields_one_anchored_error(name: str, tmp_path: Path) -> None:
    """One error Diag whose ``frame_key`` and ``file:line`` match the
    error-marked frame in ``trace.json`` (gr522025; the record code is
    the adapter's own — gr522031's finding codes carry verbatim)."""
    fixture, code = ADAPTERS[name]
    rr, doc = fixture(tmp_path)
    assert isinstance(rr, RunResult)
    assert not rr.ok
    assert Path(rr.run).name == "trace.json"
    assert Path(rr.run).is_file()
    [d] = [d for d in rr.diags if d.status is Status.ERROR]
    assert d.code == code
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
    fixture, _code = ADAPTERS[name]
    rr, _doc = fixture(tmp_path)
    d = rr.diags[0]
    assert diag_from_dict(diag_to_dict(d)) == d
    text = render(rr.diags)
    assert d.message in text
    assert f"frame {d.anchor.frame_key}" in text


def test_hex_failure_emits_one_record_per_finding(tmp_path: Path) -> None:
    """gr522031 acceptance: a .hx spec with one deliberate bad feature
    yields one error record — the finding's code and spec ``span``
    verbatim, anchored on the traced ``build`` call (its frame key and
    the script's call site) so the agent goes straight to the fix site.
    """
    rr, doc = _hex_run_failure(tmp_path)
    [d] = rr.diags
    assert d.status is Status.ERROR
    assert d.code == "port.unknown"
    assert "fuse endpoints unresolved" in d.message
    assert d.span == (4, 1)
    build_frame = next(f for f in doc["frames"] if f["qual"] == "build.build")
    assert build_frame["error"] == "BuildError"
    assert d.anchor.frame_key == build_frame["key"]
    assert (d.anchor.file, d.anchor.line) == ("main.py", 5)


def test_hex_failure_serializes_the_report(tmp_path: Path) -> None:
    """``Run.fail`` writes the exception's report on
    ``manifest.error.report`` — the adapter's only input — and stashes
    the per-finding RunResult on ``run.result``."""
    from hexfold.report import BuildError

    mod = load_script(tmp_path, _HEX_SCRIPT)
    out_dir = tmp_path / "out" / "x"
    with pytest.raises(BuildError), geom_out.run(out_dir, work_root=tmp_path) as o:
        mod.build_net()
    manifest = json.loads((out_dir / "manifest.json").read_text(encoding="utf-8"))
    [finding] = manifest["error"]["report"]["findings"]
    assert finding["code"] == "port.unknown"
    assert isinstance(o.result, RunResult)
    assert [d.code for d in o.result.diags] == ["port.unknown"]
    assert manifest["error"]["frame"] == o.result.diags[0].anchor.frame_key


def test_report_survives_a_wrapping_exception(tmp_path: Path) -> None:
    """A ``BuildError`` chained under a wrapper (``raise … from`` — the
    se-side ``GeneratorError`` shape) still reports per-finding Diags;
    without a traced op between the raise and user code the anchor is
    the script's own site."""
    script = (
        "from hexfold.build import build\n"
        "from hexfold.report import BuildError\n"
        "\n"
        "\n"
        "def wrap():\n"
        "    try:\n"
        '        build("""hexfold 0.2\norigin s\n'
        "s: sheet(40,40) - hex(2)@(20,20,A):0\n"
        's.hole --fuse k=0--> nope.in\n""", strict=True)\n'
        "    except BuildError as exc:\n"
        '        raise ValueError("generator refused the spec") from exc\n'
    )
    mod = load_script(tmp_path, script)
    out_dir = tmp_path / "out" / "x"
    with pytest.raises(ValueError, match="generator refused"):
        with geom_out.run(out_dir, work_root=tmp_path) as o:
            mod.wrap()
    manifest = json.loads((out_dir / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["error"]["type"] == "ValueError"
    assert manifest["error"]["report"]["findings"][0]["code"] == "port.unknown"
    assert o.result is not None
    [d] = o.result.diags
    assert d.code == "port.unknown"
    # The ``build`` frame outranks the wrapper's raise as the failure
    # anchor (last error-marked frame in doc order).
    assert d.anchor.frame_key
    assert (d.anchor.file, d.anchor.line) == ("main.py", 7)


def test_diags_from_report_matches_the_wire_path(tmp_path: Path) -> None:
    """``hexfold.diag.diags_from_report`` (live-object conversion) and
    the manifest wire path agree — same findings, same fields."""
    from hexfold.diag import diags_from_report
    from hexfold.report import BuildError

    mod = load_script(tmp_path, _HEX_SCRIPT)
    out_dir = tmp_path / "out" / "x"
    with pytest.raises(BuildError) as exc_info:
        with geom_out.run(out_dir, work_root=tmp_path):
            mod.build_net()
    report = exc_info.value.report
    manifest = json.loads((out_dir / "manifest.json").read_text(encoding="utf-8"))
    doc = trace_schema.loads((out_dir / "trace.json").read_text(encoding="utf-8"))
    wire = run_diag.failed_run_result(manifest, doc).diags
    live = diags_from_report(report)
    assert len(live) == len(wire)
    for w, l in zip(wire, live, strict=True):
        assert replace(l, anchor=w.anchor) == w


def test_warn_only_report_still_fails_the_run() -> None:
    """A failed run whose report carries only INFO/WARN findings keeps
    its per-finding Diags but gets ``cad.run-failed`` appended — the
    run failed, so ``RunResult.ok`` is never True."""
    manifest = {
        "status": "failed",
        "error": {
            "type": "BuildError",
            "message": "warn-only spec",
            "report": {
                "findings": [
                    {
                        "code": "port.shaky",
                        "severity": "WARN",
                        "message": "tolerance loose",
                        "where": "spec",
                        "span": [2, 1],
                        "fix": "tighten",
                    },
                    {
                        "code": "mesh.sparse",
                        "severity": "INFO",
                        "message": "coarse",
                    },
                ]
            },
        },
    }
    rr = run_diag.failed_run_result(manifest, None)
    assert not rr.ok
    assert [d.code for d in rr.diags] == [
        "port.shaky",
        "mesh.sparse",
        "cad.run-failed",
    ]
    assert [d.status for d in rr.diags[:2]] == [Status.WARN, Status.INFO]
    assert rr.diags[-1].status is Status.ERROR


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
