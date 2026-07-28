"""Native structure ingest (slice 1b) — each relaxed intermediate becomes a
first-class precis `structure` ref, linked back to its pathway.

Constructs precis's own `Scene`/`Cell`/`Atom` from an ASE `Atoms` (so no
precis-core change is needed — the bridge just uses the public structure
classes + `store.structure_save`). Slabs are metallic and periodic, so we
ingest **bond-free** (atoms + cell + the fixed-layer mask); coordination is
legible from the geometry without a declared bond graph.

Links use the symmetric `related-to` relation — it validates on every precis
version (a dedicated `pathway-node` relation is a later refinement, once the
live-relations validation is everywhere). The precise `{state → ref_id}` map is
also stored in the pathway meta, so lookup never depends on link semantics.
"""

from __future__ import annotations

import io
import re
from typing import Any

_SLUG_RE = re.compile(r"[^a-z0-9]+")
_FIX_ALL = 7  # FIX_X | FIX_Y | FIX_Z — a frozen slab atom


def _slug(s: Any) -> str:
    return _SLUG_RE.sub("-", str(s).strip().lower()).strip("-") or "x"


def scene_from_ase(atoms: Any) -> Any:
    """ASE Atoms → precis Scene. Order-preserving (atoms keep their ASE order),
    fixed-layer mask carried from any FixAtoms constraint, bond-free."""
    import numpy as np

    from precis.structure.cell import Cell
    from precis.structure.scene import Atom, Scene

    lattice = np.asarray(atoms.cell, dtype=float)
    pbc_bits = [bool(x) for x in atoms.pbc]
    pbc: tuple[bool, bool, bool] = (pbc_bits[0], pbc_bits[1], pbc_bits[2])
    scaled = atoms.get_scaled_positions(wrap=False)

    fixed_idx: set[int] = set()
    for con in getattr(atoms, "constraints", None) or []:
        if type(con).__name__ == "FixAtoms":
            try:
                fixed_idx.update(int(i) for i in con.get_indices())
            except Exception:  # pragma: no cover - ASE version variance
                fixed_idx.update(int(i) for i in getattr(con, "index", []))

    scene_atoms: dict[str, Any] = {}
    label_hi: dict[str, int] = {}
    for i, sym in enumerate(atoms.get_chemical_symbols()):
        label_hi[sym] = label_hi.get(sym, 0) + 1
        label = f"a{sym}{label_hi[sym]}"
        scene_atoms[label] = Atom(
            label=label,
            element=sym,
            frac=np.asarray(scaled[i], dtype=float),
            fixed=_FIX_ALL if i in fixed_idx else 0,
        )
    return Scene(
        cell=Cell(lattice=lattice, pbc=pbc), atoms=scene_atoms, label_hi=label_hi
    )


def _read_extxyz(text: str) -> Any:
    from ase.io import read as ase_read

    return ase_read(io.StringIO(text), format="extxyz")


def ingest_intermediates(
    store: Any,
    pathway_ref_id: int,
    pathway_slug: str,
    key: str,
    structures_extxyz: dict[str, str],
) -> dict[str, int]:
    """Save each relaxed intermediate as a `structure` ref (content-keyed slug,
    so re-runs of the same config are idempotent and a changed config makes
    fresh refs), link it `related-to` the pathway, and return {state → ref_id}.
    Best-effort per intermediate — a bad geometry is skipped, not fatal."""
    out: dict[str, int] = {}
    kp = (key or "x")[:10]
    for state, xyz in (structures_extxyz or {}).items():
        try:
            scene = scene_from_ase(_read_extxyz(xyz))
        except Exception:
            continue
        slug = f"pw-{kp}-{_slug(state)}"
        ref, _created = store.structure_save(
            slug=slug,
            title=f"{pathway_slug}: {state}",
            scene=scene,
            version=1,
            card_text=f"{state} — {len(scene.atoms)} atoms, autocatpath intermediate "
            f"of pathway {pathway_slug}",
            description=f"autocatpath intermediate '{state}' of pathway {pathway_slug}",
        )
        out[state] = ref.id
        try:
            store.add_link(
                src_ref_id=pathway_ref_id, dst_ref_id=ref.id, relation="related-to"
            )
        except Exception:
            pass  # duplicate link on re-run — fine
    return out
