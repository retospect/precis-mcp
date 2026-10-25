"""C60 nanobud on an armchair SWCNT — the sp2 generators' closed-form
geometry placed through ``pga.assembly`` and written as one block.

The bud is tangent, not fused: ``build_fullerene`` emits a closed cage
with no rim valence to join on, so this is the seed placement the SE
side relaxes — not a bonded structure.

Run from the repo root: ``uv run python examples/hexfold/nanobud/main.py``
→ ``scratch/out/nanobud/``. Import steps: ``examples/hexfold/README.md``.
"""

from __future__ import annotations

import numpy as np

import precis_geom.assembly as pga
from precis_geom import out
from precis_se.atomic.generators.sp2 import build_cnt, build_fullerene


def main() -> None:
    tube_gen = build_cnt({"n": 10, "m": 10, "length_A": 40.0})
    bud_gen = build_fullerene({"atoms": 60})

    r_tube = float(tube_gen.topology["radius_A"])
    r_bud = float(np.linalg.norm(np.asarray(bud_gen.coords), axis=1).max())
    z_mid = float(np.asarray(tube_gen.coords)[:, 2].mean())

    tube = pga.geom(
        tube_gen,
        meta={"chiral_index": list(tube_gen.topology["chiral_index"])},
    )
    bud = pga.translate(pga.geom(bud_gen), [r_tube + r_bud - 0.4, 0.0, z_mid])
    with out.run("nanobud") as o:
        o.block(
            "nanobud",
            pga.assembly("nanobud", children=[tube, bud]),
            desc="(10,10) armchair SWCNT, C60 bud tangent to the wall "
            "(build_cnt + build_fullerene, unrelaxed placement)",
        )
    print(o.root)


if __name__ == "__main__":
    main()
