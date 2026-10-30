"""gr522026 — the CAD probe adapter (shared-core-layers S1c):
``out.run(...) as o: o.probe(selector, label=...)`` resolves a selector
or pg object at script time, prints one ``probe <label>: resolves to N
<kind>`` line, and records ``{label, count, kind, digest}`` on the
calling trace frame so ``view='trace'``'s frame card lists it there.
An unresolvable selector raises ``precis_diag.ProbeError`` carrying the
refusal Diag, which the failed run's record picks up by code.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import precis_diag
from precis_geom import out as geom_out
from precis_geom import trace_schema
from tests.workspaces._script import load_script

pytestmark = pytest.mark.filterwarnings("ignore")

#: ``build(o)`` makes a three-atom scene, blocks it (so the traced call
#: mints the ``main.build`` user frame) and probes the scene — the trace
#: must carry the probe record on that calling frame.
_PG_SCRIPT = (
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
    "    return o.probe(scene, label='triad')\n"
)

#: A script with a traced call and a ``frame:<key>`` selector — the key
#: is computed from the public key recipe (root key constant +
#: ``frame_key``), standing in for a key copied from the previous run's
#: trace.json (keys are stable across reruns of an unchanged script).
_FRAME_SCRIPT = (
    "from hexfold.sheetnet import strip\n"
    "from precis_geom import trace_schema as ts\n"
    "\n"
    "\n"
    "def build(o):\n"
    "    strip(rows=2, periods=2)\n"
    "    root = ts.sha8('precis-trace-root')\n"
    "    build_key = ts.frame_key(root, 'main.build', 0)\n"
    "    strip_key = ts.frame_key(build_key, 'sheetnet.strip', 0)\n"
    "    o.probe(f'frame:{strip_key}', label='strip-frame')\n"
)

#: A selector nobody owns — the run fails and the record names the code.
_BAD_SCRIPT = (
    "from precis_geom import out\n"
    "\n"
    "\n"
    "def build(o):\n"
    "    o.probe('frames:typo', label='typo')\n"
)


def _doc(out_dir: Path) -> dict:
    return trace_schema.loads((out_dir / "trace.json").read_text(encoding="utf-8"))


def test_probe_pg_object_prints_and_records_under_the_calling_frame(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    mod = load_script(tmp_path, _PG_SCRIPT)
    out_dir = tmp_path / "out" / "x"
    with geom_out.run(out_dir, work_root=tmp_path) as o:
        result = mod.build(o)
    printed = capsys.readouterr().out
    build_frame = next(f for f in _doc(out_dir)["frames"] if f["qual"] == "main.build")
    (record,) = build_frame["probes"]
    assert record["label"] == "triad"
    assert record["kind"] == "atoms"
    assert record["count"] == 3
    # positions resolve through the scene's cell — the digest is a 3D
    # bbox and the sample renders it (never "")
    assert record["digest"]["bbox"] == [0.0, 0.0, 0.0, 10.0, 10.0, 5.0]
    assert result["sample"] == "[0, 0, 0, 10, 10, 5]"
    # the printed line agrees with the recorded count
    assert "probe triad: resolves to 3 atoms" in printed


def test_probe_frame_key_selector_resolves_made_counts(tmp_path: Path) -> None:
    mod = load_script(tmp_path, _FRAME_SCRIPT)
    out_dir = tmp_path / "out" / "x"
    with geom_out.run(out_dir, work_root=tmp_path) as o:
        mod.build(o)
    doc = _doc(out_dir)
    strip_frame = next(f for f in doc["frames"] if f["qual"] == "sheetnet.strip")
    build_frame = next(f for f in doc["frames"] if f["qual"] == "main.build")
    (record,) = build_frame["probes"]
    assert record["label"] == "strip-frame"
    assert record["kind"] == "entities"
    assert record["count"] == sum(strip_frame["made"].values())
    assert record["digest"]["ids"] == [
        f"{k}:{v}" for k, v in sorted(strip_frame["made"].items())
    ]


def test_probe_unresolvable_selector_fails_the_run_with_the_diag(
    tmp_path: Path,
) -> None:
    from precis_diag import ProbeError

    mod = load_script(tmp_path, _BAD_SCRIPT)
    out_dir = tmp_path / "out" / "x"
    with (
        pytest.raises(ProbeError) as exc_info,
        geom_out.run(out_dir, work_root=tmp_path) as o,
    ):
        mod.build(o)
    assert exc_info.value.diag.code == "core.probe-unresolved"
    manifest = json.loads((out_dir / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["status"] == "failed"
    assert manifest["error"]["code"] == "core.probe-unresolved"


def test_cad_resolver_registers_for_global_dispatch() -> None:
    """Importing precis_geom.probe registers the cad resolver — the
    conformance suite's dispatch path."""
    import precis_geom.probe  # noqa: F401 — registration is the point
    from hexfold.build import build

    net = build("hexfold 0.2\norigin s\ns: sheet(40,40) - hex(2)@(20,20,A):0\n")
    result = precis_diag.probe(net)
    assert result["digest"]["kind"] == "atoms"
    assert result["count"] == len(net.atoms)
    # far past the ids cap: the digest carries a bounded head plus the
    # overflow count, and the count stays authoritative
    assert len(result["digest"]["ids"]) == precis_diag.IDS_SAMPLE_CAP
    assert result["digest"]["ids_capped"] == len(net.atoms) - precis_diag.IDS_SAMPLE_CAP
