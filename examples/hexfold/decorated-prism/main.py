"""Decorated prism with a guest — the molecular-workspace-code worked
example, as a runnable script.

A hexagonal prism tube (``mol.prism``: ``prism_tube`` + the stick relax,
a cached step), COOH on every third open outward edge atom
(``mol.decorate``), H on the rest (``mol.cap``), a UFF collapse check
(``mol.relax``, ``rung='auto'`` -> ``uff`` on a finite scene) and a C60
template instanced beside the prism.

Every knob is in ``recipe.json`` beside this file (``steps.recipe()``):
edit ``every`` or ``relax_steps`` and re-run — the prism, the decoration
and the cap are cache hits (``scratch/cache/``), only the first step that
uses the edited number and the ones below it rebuild. The echo prints
``steps: …`` on exit.

Run from the repo root: ``uv run python examples/hexfold/decorated-prism/main.py``
→ ``scratch/out/decorated-prism/``. Import steps:
``examples/hexfold/README.md``. ``relaxed_by uff`` on the block row is the
quick collapse check; the record is the service relax the row's ``relax:``
asks for.
"""

from __future__ import annotations

import precis_geom.mol as mol
from precis_geom import out, steps


def main() -> None:
    R = steps.recipe()  # recipe.json
    with out.run("decorated-prism") as o:
        p = mol.prism(R["periods"], R["rows"], R["strip_rows"], R["seams"])
        p = mol.decorate(
            p,
            sites=f"open & face:out & every:{R['every']}",
            group=R["group"],
        )
        p = mol.cap(p)  # H on the rest
        p, cc = mol.relax(p, steps=R["relax_steps"])  # auto: finite -> uff
        cc.raise_if_collapsed()  # Run.fail records it, naming the frames
        o.block(
            "p",
            p,
            relax={"method": "gfn2"},
            desc="hexagonal prism tube (4, 4, 2, '++++++'), COOH on every "
            "third open outward edge atom, H-capped, UFF collapse-checked",
        )
        o.block("c60", mol.lib("c60"), template=True, desc="C60 guest template")
        o.instance(
            "guest",
            template="c60",
            parent="p",
            pose=[0.0, 0.0, R["guest_nm"]],
            pose_unit="nm",
        )
    print(o.root)


if __name__ == "__main__":
    main()
