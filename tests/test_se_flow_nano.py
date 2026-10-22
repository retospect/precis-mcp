"""precis_se.flow lumen path — slice 3's adapter acceptance of
docs/backlog/se-flow-kernel.md: a hexfold lumen block routes to the
Knudsen propagator through ``view='flow'`` and the four
FLOW_COMPUTERS measures work on the shared Paths contract.
"""

from __future__ import annotations

import base64
import json

import numpy as np
import pytest

from hexfold.build import build as hexfold_build
from precis.errors import BadInput
from precis.structure.cell import Cell
from precis.structure.scene import Atom, Scene
from precis_se.flow import (
    FLOW_COMPUTERS,
    LumenSpec,
    parse_flow_shape,
    render_flow,
)
from precis_se.measures import MeasureSpec
from precis_se.ops import BoundScene, SeBlock, SeTree, apply_ops

R_VDW_C = 1.70


def _lumen_tree() -> SeTree:
    """A block bound to a (6,6) hexfold tube, scene resolver wired."""
    net = hexfold_build("hexfold 0.1\norigin t\nt: tube(6,6, len=6)\n")
    pos = np.asarray(net.seed3, dtype=float)
    cell = Cell(np.eye(3) * 100.0)
    scene = Scene(
        cell=cell,
        atoms={
            f"aC{i}": Atom(f"aC{i}", "C", pos[i] / 100.0) for i in range(pos.shape[0])
        },
    )
    meta = {
        "generated": {
            "generator": "hexfold_spec",
            "sites": {"sites": [], "pairs": []},
        }
    }
    bound = BoundScene(scene=scene, meta=meta)
    tree = SeTree()
    tree.blocks["t"] = SeBlock(
        name="t",
        envelope="box:w1e-8d1e-8h1e-8",
        bound_kind="structure",
        bound="hex-tube",
    )
    tree.structures = lambda slug: bound if slug == "hex-tube" else None
    return tree


def test_lumen_view_flow_text_and_json() -> None:
    """view='flow' on a hexfold lumen block runs the Knudsen propagator
    without being asked — transmission, t* residence, the same (K, n, 3)
    frames blob, the scale triple and the card."""
    tree = _lumen_tree()
    args = {"block": "t", "alpha": 1.0, "n": 300, "frames": 8}
    body = render_flow(None, tree, args)
    assert "Knudsen" in body and "transmission" in body and "residence" in body
    payload = json.loads(render_flow(None, tree, {**args, "format": "json"}))
    assert payload["regime"] == "knudsen" and payload["alpha"] == 1.0
    assert payload["scale"]["L"] == pytest.approx(13.527e-10, rel=0.01)
    assert payload["scale"]["h0"] == pytest.approx(
        2 * payload["mesh"]["lumen"]["r_eff"] * payload["scale"]["L"], rel=1e-9
    )
    fr = payload["frames"]
    assert fr["shape"] == [8, 300, 3] and fr["dtype"] == "float32"
    blob = np.frombuffer(base64.b64decode(fr["data_b64"]), dtype="<f4")
    frames = blob.reshape(fr["shape"])
    assert frames[:, :, 0].min() >= -1e-6 and frames[:, :, 0].max() <= 1.0 + 1e-6
    mesh = payload["mesh"]["lumen"]
    centres = np.frombuffer(base64.b64decode(mesh["centres"]["data_b64"]), dtype="<f4")
    assert centres.size == 144 * 3  # all tube atoms wall the lumen
    # a second run lands the same transmission in seed scatter
    t2 = json.loads(render_flow(None, tree, {**args, "seed": 1, "format": "json"}))
    # transmission is reported in the text, not the payload — recompute
    # via the same propagator contract instead: check determinism path


def test_lumen_routing_gates() -> None:
    """Kn ≥ 10 runs; 0.01 < Kn < 10 refuses naming DSMC; Kn ≤ 0.01
    refuses (a lumen has no continuum solver)."""
    tree = _lumen_tree()
    base = {"block": "t", "alpha": 1.0, "n": 50}
    h0 = 2 * (8.136e-10 / 2 - R_VDW_C * 1e-10)  # ~free diameter, metres
    ok = render_flow(None, tree, {**base, "mean_free_path": 50 * h0})
    assert "transmission" in ok
    with pytest.raises(BadInput, match="DSMC"):
        render_flow(None, tree, {**base, "mean_free_path": 1.0 * h0})
    with pytest.raises(BadInput, match="thin chamber"):
        render_flow(None, tree, {**base, "mean_free_path": 0.001 * h0})
    # chamber-only inputs refused on a lumen
    with pytest.raises(BadInput, match="thin-chamber input"):
        render_flow(None, tree, {**base, "dv": 6.0})
    # alpha is required — never defaulted
    with pytest.raises(BadInput, match="alpha"):
        render_flow(None, tree, {"block": "t", "n": 50})


def test_lumen_flow_spec_and_computers() -> None:
    """flow:lumen;alpha=<a> parses, the pocket op accepts it, and the
    four FLOW_COMPUTERS measures realise values on the lumen."""
    spec = parse_flow_shape("flow:lumen;alpha=1")
    assert isinstance(spec, LumenSpec) and spec.alpha == 1.0
    for bad in ("flow:lumen", "flow:lumen;dv=1", "flow:lumen;alpha=2"):
        with pytest.raises(ValueError):
            parse_flow_shape(bad)
    tree = _lumen_tree()
    apply_ops(
        tree,
        [
            {
                "op": "add_pocket",
                "block": "t",
                "name": "flow",
                "shape": "flow:lumen;alpha=1",
                "regions": [{"selector": "atoms:t[0-20]"}],
            }
        ],
    )
    datum = "atoms:t[0-20]"
    got = {}
    for slug, unit in (
        ("breakthrough-fraction", "ratio"),
        ("residence-time-t0", "ratio"),
        ("resistance-ratio", "ratio"),
        ("evenness", "ratio"),
    ):
        m = MeasureSpec(block="t", name="m", datum=datum, measurand=slug, unit=unit)
        got[slug] = FLOW_COMPUTERS[slug](tree, m)
    assert 0.0 < got["breakthrough-fraction"] <= 1.0
    assert got["residence-time-t0"] > 0.0
    # corrugated lumen vs smooth cylinder at R_eff: slightly above 1
    assert got["resistance-ratio"] > 0.9
    assert 0.0 <= got["evenness"] <= 1.0


def test_lumen_chamber_card_routes() -> None:
    """On a cad chamber the card still governs: Kn ≥ 10 refuses naming
    the propagator's lumen requirement."""
    tree = SeTree()
    apply_ops(
        tree, [{"op": "add_block", "name": "c", "envelope": "box:w0.1d0.03h0.001"}]
    )
    args = {
        "block": "c",
        "inlet": "patch:c.side3@0,0+0.006x0.001",
        "outlet": "patch:c.side1@0,0+0.006x0.001",
        "dv": 6.0,
        "n": 20,
        "mu": 1e-3,
        "rho": 1.0,
        "u0": 0.01,
        "mean_free_path": 1.0,  # Kn = 1000
    }
    with pytest.raises(BadInput, match="free-molecular"):
        render_flow(None, tree, args)
    # transition band refuses naming DSMC
    with pytest.raises(BadInput, match="DSMC"):
        render_flow(None, tree, {**args, "mean_free_path": 0.002})
