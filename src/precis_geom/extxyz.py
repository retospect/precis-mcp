"""``precis_geom.extxyz`` — the write side of the importer's extxyz /
``*.bonds.json`` geometry format.

The canonical writers live at this layer because both directions need
them: a host-run scratch script hands :mod:`precis_geom.out` a scene
(dev-loop-scratch D0) and the main package's
:mod:`precis.structure.export` re-exports the same bytes — ``precis_geom``
is a standalone kernel that may never import ``precis`` (the
import-linter contract in ``pyproject.toml``), so the shared code sits
low and the dependency points down.

Everything is duck-typed on the scene protocol — ``.atoms`` (label →
object with ``.element``/``.frac``/``.fixed``/``.maker``),
``.cell.lattice``/``.cell.pbc``/``.cell.frac_to_cart``, ``.bonds``
(``.i``/``.j``/``.order``/``.kind``/``.maker``) — so a ``precis.structure``
``Scene`` and the wheel's own scene objects both serialize. Stdlib only.
"""

from __future__ import annotations

import json
from typing import Any


def to_extxyz(scene: Any) -> str:
    """Extended XYZ, the lossless precis-native round-trip form:
    Cartesian positions + a ``Lattice``/``pbc`` header + our per-atom
    ``label`` column (plus ``maker`` when any atom carries one) — the
    exact bytes :func:`precis.structure.importers.extxyz.extxyz_to_scene`
    reads back. The constraints-preserving ASE variant stays in
    :func:`precis.structure.export.to_extxyz`, which delegates its
    constraint-free tail here.

    ``maker`` is the ``trace.json`` frame id that minted the atom
    (``precis_geom.trace.leaf_id``), so an extxyz row joins back to the
    builder call that made it. When atoms merge — a fused tile's
    coincident sites, an assembly dedup — the surviving atom keeps the
    earliest frame's stamp (``assembly._fuse_copies``: the earlier copy
    wins); a frame's ``made`` count names what it minted."""
    flat = " ".join(f"{x:.8f}" for row in scene.cell.lattice for x in row)
    pbc = " ".join("T" if p else "F" for p in scene.cell.pbc)
    atoms = list(scene.atoms.items())
    has_maker = any(a.maker is not None for _l, a in atoms)
    props = "species:S:1:pos:R:3:label:S:1" + (":maker:I:1" if has_maker else "")
    head = f'Lattice="{flat}" Properties={props} pbc="{pbc}"'
    lines = [str(len(atoms)), head]
    for label, a in atoms:
        x, y, z = scene.cell.frac_to_cart(a.frac)
        row = f"{a.element} {x:.8f} {y:.8f} {z:.8f} {label}"
        if has_maker:
            row += f" {a.maker if a.maker is not None else -1}"
        lines.append(row)
    return "\n".join(lines) + "\n"


def bonds_to_json(bonds: list[Any]) -> str:
    """The ``*.bonds.json`` sidecar — the write side of
    ``bonds_from_json``, same shape (``{"schema": 1, "bonds":
    [{"i", "j", "order"?, "kind"?, "maker"?}]}``, defaults omitted)."""
    out: list[dict[str, Any]] = []
    for b in bonds:
        entry: dict[str, Any] = {"i": b.i, "j": b.j}
        if b.order != 1.0:
            entry["order"] = b.order
        if b.kind != "pairwise":
            entry["kind"] = b.kind
        if b.maker is not None:
            entry["maker"] = b.maker
        out.append(entry)
    return json.dumps({"schema": 1, "bonds": out}, indent=1) + "\n"


__all__ = ["bonds_to_json", "to_extxyz"]
