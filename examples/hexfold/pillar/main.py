"""Sphere-topped pillar on a graphene sheet — a ``sheetnet.strip`` base,
a short SWCNT standing on it and a C60 cap, placed through
``pga.assembly`` and written as one block.

Unrelaxed placement: the pillar stands on the sheet's centre and the cap
sits on the pillar's top rim — the SE side relaxes the seed.

Run from the repo root: ``uv run python examples/hexfold/pillar/main.py``
→ ``scratch/out/pillar/``. Import steps: ``examples/hexfold/README.md``.
"""

from __future__ import annotations

import numpy as np

import precis_geom.assembly as pga
from hexfold.sheetnet import strip
from precis_geom import out
from precis_se.atomic.generators.sp2 import build_cnt, build_fullerene


def main() -> None:
    sheet = strip(8, 8)
    pillar_gen = build_cnt({"n": 8, "m": 8, "length_A": 40.0})
    cap_gen = build_fullerene({"atoms": 60})

    base = np.asarray(sheet.block.coords)
    cx, cy = (float(v) for v in base[:, :2].mean(axis=0))
    pillar_top = float(np.asarray(pillar_gen.coords)[:, 2].max())
    r_cap = float(np.linalg.norm(np.asarray(cap_gen.coords), axis=1).max())

    sheet_g = pga.geom(sheet)
    pillar = pga.translate(
        pga.geom(
            pillar_gen,
            meta={"chiral_index": list(pillar_gen.topology["chiral_index"])},
        ),
        [cx, cy, 0.0],
    )
    cap = pga.translate(pga.geom(cap_gen), [cx, cy, pillar_top + r_cap - 0.4])
    with out.run("pillar") as o:
        o.block(
            "pillar",
            pga.assembly("pillar", children=[sheet_g, pillar, cap]),
            desc="C60-capped (8,8) SWCNT pillar on a graphene sheet "
            "(sheetnet strip + sp2 generators, unrelaxed placement)",
        )
    print(o.root)


if __name__ == "__main__":
    main()
