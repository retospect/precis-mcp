"""Render-parity harness — the browser tessellator must emit the *same*
triangles as the server's numpy tessellator, so the client-side 3D view can
never drift from the STL / glTF geometry (the "validate they render the same
way" guard for the client-side render).

We build a corpus of designs, tessellate each node with the authoritative
:func:`precis.cad.tessellate.node_meshes` (+ ``gltf._merge``), and hand those
golden meshes plus the scene-recipe nodes to ``scripts/cad_tessellate_parity.mjs``,
which recomputes them in JS (``static/cad-tessellate.js``) and diffs. The test
**skips when ``node`` is absent** (mirrors the repo's optional-dep skips) so the
Python-only gate stays green; CI has node and runs it for real.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from precis.cad.dsl import parse as parse_shape
from precis.cad.gltf import _merge
from precis.cad.scene import parse_source
from precis.cad.tessellate import node_meshes

_REPO = Path(__file__).resolve().parents[1]
_SCRIPT = _REPO / "scripts" / "cad_tessellate_parity.mjs"

# A corpus exercising every primitive, both pattern kinds, and transforms/cuts.
_CORPUS: dict[str, str] = {
    "primitives": (
        "component a\n"
        "bx  add box:w40mmd20mmh10mm\n"
        "cy  add cyl:r6mmh12mm       @30mm,0mm,0mm\n"
        "co  add cone:r5mmh9mm       @-30mm,0mm,0mm\n"
        "tc  add tcone:rb6mmrt3mmh8mm  @0mm,30mm,0mm\n"
        "sp  add sphere:r7mm       @0mm,-30mm,0mm\n"
        "to  add torus:R12mmr3mm     @0mm,0mm,20mm\n"
    ),
    "polygons": (
        "component p\n"
        "hx  add hex:r8mmh6mm\n"
        "ng  add ngon:n5r7mmh6mm      @25mm,0mm,0mm\n"
        "fr  add frustum:n6rb8mmrt4mmh10mm @-25mm,0mm,0mm\n"
        "py  add pyramid:n4r6mmh9mm   @0mm,25mm,0mm\n"
    ),
    "patterns_and_pose": (
        "component q\n"
        "plate add cyl:r25mmh6mm\n"
        "bolts add cyl:r2mmh8mm       @18mm,0mm,-1mm polar:n6r18mm\n"
        "slots cut box:w4mmd4mmh8mm     @0mm,0mm,0mm   linear:n3dx6mmdy0mmdz0mm\n"
        "tilt  add box:w6mmd6mmh6mm     @0mm,0mm,10mm  rot:0deg,0deg,30deg\n"
    ),
}


def _build_cases() -> dict[str, object]:
    cases = []
    for name, src in _CORPUS.items():
        spec = parse_source(src)
        nodes = []
        for n in spec.nodes:
            merged = _merge(node_meshes(n))
            if merged is None:
                continue
            verts, tris = merged
            sh = parse_shape(n.config)
            nodes.append(
                {
                    "node": {
                        "name": n.name,
                        "loc": list(n.loc),
                        "rot": list(n.rot),
                        "pattern": n.pattern,
                        "shape": {"alias": sh.alias, "params": sh.params},
                    },
                    "expected": {
                        "verts": [[float(x) for x in v] for v in verts],
                        "tris": [[int(i) for i in t] for t in tris],
                    },
                }
            )
        cases.append({"design": name, "nodes": nodes})
    return {"cases": cases}


@pytest.mark.skipif(shutil.which("node") is None, reason="node not available")
@pytest.mark.skipif(
    sys.platform == "win32",
    reason="node's ESM loader rejects a Windows drive-letter absolute path"
    " ('d:...') passed as a bare argv path — file:// scheme required there",
)
def test_browser_tessellator_matches_server(tmp_path: Path) -> None:
    cases_file = tmp_path / "cases.json"
    cases_file.write_text(json.dumps(_build_cases()), encoding="utf-8")
    result = subprocess.run(
        ["node", str(_SCRIPT), str(cases_file)],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, (
        f"browser/server geometry drift:\n{result.stdout}\n{result.stderr}"
    )


def test_corpus_tessellates_server_side() -> None:
    # Pure-Python sanity: every corpus node has a finite mesh (no accidental
    # chamfer), so the JS parity comparison above is over real geometry.
    for _name, src in _CORPUS.items():
        spec = parse_source(src)
        assert spec.nodes
        for n in spec.nodes:
            merged = _merge(node_meshes(n))
            assert merged is not None
            verts, _tris = merged
            assert len(verts) > 0
