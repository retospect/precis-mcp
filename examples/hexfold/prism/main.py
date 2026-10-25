"""Hexagonal prism tube — ``hexfold.prism.prism_tube`` built, stick-
relaxed and written as one block through ``precis_geom.out``.

Run from the repo root: ``uv run python examples/hexfold/prism/main.py``
→ ``scratch/out/prism/``. Import into an se design via the
``import_workspace`` edit op — the steps are in
``examples/hexfold/README.md``.
"""

from __future__ import annotations

import precis_geom.assembly as pga
from hexfold.prism import prism_tube
from hexfold.stick import stick
from precis_geom import out


def main() -> None:
    net, _tube = prism_tube(4, 4, 2, "++++++")
    assert net.report.ok
    with out.run("prism") as o:
        o.block(
            "prism",
            pga.geom(net, coords=stick(net)),
            desc="hexagonal prism tube, 6 fin seams, stick-relaxed "
            "(prism_tube(4, 4, 2, '++++++'))",
        )
    print(o.root)


if __name__ == "__main__":
    main()
