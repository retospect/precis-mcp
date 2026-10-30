"""A carbon nanotube with a C60 guest inside — the guest has to fit.

``mol.generate('cnt', ...)`` (the closed-form ``GENERATORS['cnt']``, a
cached step), OH on every second open rim atom (``mol.decorate``), H on the
rest of the rim (``mol.cap``), and a C60 (``mol.lib('c60')``) instanced at
the tube's centre. Before the guest is placed, ``pg.inspect.ray`` profiles
the free radius along the tube axis — the distance from the axis to the
nearest wall van der Waals surface, its minimum the channel bottleneck —
and the run **fails** unless that bottleneck exceeds the C60 cage radius
(the largest atom distance from the cage centre). A tube too narrow for the
guest (try ``"n": 6, "m": 6`` in ``recipe.json``) records a failed run
naming the wall frame that pinches it, not a geometry nobody checked.

The profile is a van der Waals model (free radius to the vdW surface, not to
density); it answers "does the cage's atom centre clear the wall", the
fit the atomic ``section``/``overlap`` probes use.

Run from the repo root: ``uv run python examples/hexfold/cnt-guest/main.py``
→ ``scratch/out/cnt-guest/``. Import steps: ``examples/hexfold/README.md``.
"""

from __future__ import annotations

import numpy as np

import precis_geom.inspect as pgi
import precis_geom.mol as mol
from precis_geom import out, steps

_A = 1e-10  # inspect works in metres


def _cart(geom) -> np.ndarray:
    sc = geom.scene
    return np.array([sc.cell.frac_to_cart(a.frac) for a in sc.atoms.values()])


def main() -> None:
    R = steps.recipe()  # recipe.json
    with out.run("cnt-guest") as o:
        tube = mol.generate("cnt", n=R["n"], m=R["m"], length_A=R["length_A"])
        tube = mol.decorate(
            tube, sites=f"open & every:{R['every']}", group=R["rim_group"]
        )
        tube = mol.cap(tube)  # H on the rest of the rim
        guest = mol.lib("c60")

        xyz = _cart(tube)
        centre = xyz.mean(axis=0)
        r_guest = float(
            np.linalg.norm(_cart(guest) - _cart(guest).mean(axis=0), axis=1).max()
        )
        z0, z1 = float(xyz[:, 2].min()) - 3.0, float(xyz[:, 2].max()) + 3.0
        prof = pgi.ray(
            tube,
            origin=(centre[0] * _A, centre[1] * _A, z0 * _A),
            axis=(0.0, 0.0, 1.0),
            t_max=(z1 - z0) * _A,
            profile_samples=int(R["profile_samples"]),
            events=(),
        )
        assert prof.pinch_m is not None
        free_A = prof.pinch_m / _A
        pinch = [f.label for f in prof.pinch_features]
        if free_A <= r_guest:
            raise ValueError(
                f"the C60 guest does not fit: the tube's free radius bottoms at "
                f"{free_A:.2f} Å (wall {pinch}), the cage radius is {r_guest:.2f} Å"
            )
        o.block(
            "tube",
            tube,
            ports=[],  # the generator's rim ports are capped or decorated now
            desc=f"({R['n']}, {R['m']}) SWCNT, {R['rim_group']} on every "
            f"{R['every']}-th open rim atom, rest H-capped; free radius "
            f"{free_A:.2f} Å > C60 cage radius {r_guest:.2f} Å",
        )
        o.block("c60", guest, template=True, desc="C60 guest template")
        o.instance(
            "guest",
            template="c60",
            parent="tube",
            pose=[float(centre[0]), float(centre[1]), float(centre[2])],
            pose_unit="Å",
        )
    print(o.root)


if __name__ == "__main__":
    main()
