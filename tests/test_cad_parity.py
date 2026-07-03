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
        "bx  add box:w40d20h10\n"
        "cy  add cyl:r6h12       @30,0,0\n"
        "co  add cone:r5h9       @-30,0,0\n"
        "tc  add tcone:rb6rt3h8  @0,30,0\n"
        "sp  add sphere:r7       @0,-30,0\n"
        "to  add torus:R12r3     @0,0,20\n"
    ),
    "polygons": (
        "component p\n"
        "hx  add hex:r8h6\n"
        "ng  add ngon:n5r7h6      @25,0,0\n"
        "fr  add frustum:n6rb8rt4h10 @-25,0,0\n"
        "py  add pyramid:n4r6h9   @0,25,0\n"
    ),
    "patterns_and_pose": (
        "component q\n"
        "plate add cyl:r25h6\n"
        "bolts add cyl:r2h8       @18,0,-1 polar:n6r18\n"
        "slots cut box:w4d4h8     @0,0,0   linear:n3dx6dy0dz0\n"
        "tilt  add box:w6d6h6     @0,0,10  rot:0,0,30\n"
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
def test_browser_tessellator_matches_server(tmp_path: Path) -> None:
    cases_file = tmp_path / "cases.json"
    cases_file.write_text(json.dumps(_build_cases()))
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
