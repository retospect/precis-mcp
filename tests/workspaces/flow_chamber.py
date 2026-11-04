"""The slice-4a flow workspace fixture — the script half of the
script→import→Flow-tab path (se-flow-kernel 4a, gr563788/td563794).

The chamber void is the cfdtest hex chamber as cad DSL: a flat ``box:``
plus two ``ngon:`` tips (a thin z cell domain) with the six hole
cylinders digging the floor. Its numbers depend on the seed, the
particle count, ``dv`` and the cells, which is what lets a stated value
disagree with the server's. :data:`FLAT_DSL` is the same footprint
undrilled, for tests where the flat floor is the point.

This file IS the workspace ``main.py``: wheel imports only (``precis.cad``
/ ``precis_geom``), no store, no ``precis_se``. Run it as a script with an
out dir and it writes the run the Flow tab plays.
"""

from __future__ import annotations

import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

import numpy as np

from precis.cad.scene import build_design, parse_source
from precis_geom import out as geom_out
from precis_geom.flow import PortPatch
from precis_geom.physics import evaluate

L, W, H = 0.1, 0.03, 1e-3
PORT_W = 0.006
D_V_STAR = 6.0
HOLE_R_MM = 2.5
#: ``(x, y, depth)`` in mm from the chamber centre — the spec's hole set.
HOLES_MM = (
    (-20.0, 0.0, 2.5),
    (-5.0, -7.0, 2.0),
    (-5.0, 7.0, 2.0),
    (10.0, 0.0, 3.0),
    (25.0, -6.0, 1.5),
    (25.0, 6.0, 1.5),
)

FLAT_DSL = (
    "body add box:w70mmd30mmh1mm\n"
    "tip_l add ngon:n4r15mmh1mm @-35mm,0mm,0mm\n"
    "tip_r add ngon:n4r15mmh1mm @35mm,0mm,0mm\n"
)
CHAMBER_DSL = FLAT_DSL + "".join(
    f"hole{i} add cyl:r{HOLE_R_MM}mmh{d + 0.5}mm @{x}mm,{y}mm,{-d}mm\n"
    for i, (x, y, d) in enumerate(HOLES_MM)
)

#: The measures every run declares: all three need no evenness region.
MEASURES = [
    {"name": "bt", "measurand": "breakthrough-fraction"},
    {"name": "rt", "measurand": "residence-time-t0"},
    {"name": "rr", "measurand": "resistance-ratio"},
]


def chamber_solid(dsl: str = CHAMBER_DSL) -> Any:
    """The ``(Design, Expr)`` void the script's evaluate and the
    importer's bound-cad chamber share."""
    design = build_design(parse_source(dsl))
    return design, design.whole()


def tip_patch(x: float, sign: float) -> PortPatch:
    """A port rectangle on the tip's end face (chamber-local frame)."""
    return PortPatch(
        point=np.array([x, 0.0, H / 2]),
        normal=np.array([sign, 0.0, 0.0]),
        u=np.array([0.0, 1.0, 0.0]),
        v=np.array([0.0, 0.0, 1.0]),
        half_u=PORT_W / 2,
        half_v=H / 2,
    )


def floor_patch(half_x: float = 0.03, half_y: float = 0.012) -> PortPatch:
    """A floor rectangle about the chamber centre — an evenness region."""
    return PortPatch(
        point=np.array([0.0, 0.0, 0.0]),
        normal=np.array([0.0, 0.0, -1.0]),
        u=np.array([1.0, 0.0, 0.0]),
        v=np.array([0.0, 1.0, 0.0]),
        half_u=half_x,
        half_v=half_y,
    )


def flow_spec(
    measures: list[dict[str, Any]] | None = None, **overrides: Any
) -> dict[str, Any]:
    """The evaluate spec — ``out.physics`` serialises the same dict."""
    return {
        "inlet": tip_patch(-L / 2, -1.0),
        "outlet": tip_patch(L / 2, 1.0),
        "dv": D_V_STAR,
        "profile": "plug",
        "n": 200,
        "measures": list(MEASURES if measures is None else measures),
        **overrides,
    }


def write_out(
    out_dir: Path,
    *,
    cells: int = 200,
    seed: int = 0,
    measures: list[dict[str, Any]] | None = None,
    dsl: str = CHAMBER_DSL,
    physics: bool = True,
    block_measures: list[dict[str, Any]] | None = None,
    extra: Callable[[Any], None] | None = None,
    **spec: Any,
) -> Any:
    """What ``main.py`` does in the sandbox: build, evaluate, declare —
    ``out.block`` the DSL and ``out.physics`` the evaluate spec plus its
    stated outcome. ``spec`` overrides :func:`flow_spec`'s keys; ``extra``
    declares more of the run on the open ``Run``; ``physics=False`` drops
    the row; ``block_measures`` declares measures on the block itself.
    Returns the ``Outcome`` for assertions."""
    full = flow_spec(measures, **spec)
    oc = evaluate(chamber_solid(dsl), ["flow"], full, seed=seed, cells=cells)
    with geom_out.run(out_dir, check=False) as o:
        o.block("c", dsl, measures=block_measures)
        if physics:
            o.physics("c", ["flow"], full, outcome=oc, seed=seed, cells=cells)
        if extra is not None:
            extra(o)
    return oc


if __name__ == "__main__":
    write_out(Path(sys.argv[1]) if len(sys.argv) > 1 else Path("/work/out"))
