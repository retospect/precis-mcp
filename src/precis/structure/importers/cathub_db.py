"""Catalysis-Hub **local `.db`** reader — external DFT library import's batch-mirror ingress.

Catalysis-Hub's *live* channels are all credential-gated as of late-2025 (the
GraphQL API wants an ``X-API-Key``; the "public" ``apiuser`` Postgres password
in ``cathub/config.py`` was rotated server-side). But a Catalysis-Hub dataset
is *also* a self-contained, **keyless** file: a cathub ``.db`` is plain SQLite
carrying a relational ``reaction`` table + the ASE ``systems`` structures it
references + a ``publication`` row. That file — downloaded once, shipped with a
paper, or handed over directly — is the batch-mirror unit this module consumes,
no network and no ``cathub`` dependency required (we read the ASE half with
``ase.db``, already in the ``[import]`` extra, and the reaction half with stdlib
``sqlite3``).

Normalisation is **not** duplicated here: a cathub record is reshaped into the
exact dict :func:`precis.structure.importers.catalysis_hub.adapter` already
consumes (same source, ``dataset='catalysis-hub'``), so the one tested adapter
handles both the (dead) live path and this local-file path.

What gets imported: per reaction, the **product adsorbate system** — the
``reaction_system`` row whose ``name`` is one of the reaction's ``products``
keys (e.g. ``NOstar``: NO adsorbed on the slab). That is the substrate the
catalyst quest explores; the clean-slab (``star``) and gas-phase reference
(``NOgas``) systems of the same reaction are skipped in v1. The imported run's
energy is the reaction's ``reaction_energy`` (the adsorption energy — the
quantity the quest grounds against); the per-system DFT total energy rides along
as the adapter's fallback.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from ...store import Store


class CathubDbUnsupported(RuntimeError):
    """The local-``.db`` reader's dep (``ase``, the ``[import]`` extra) is missing."""


def _require_ase() -> Any:
    try:
        from ase.db import connect
    except ImportError as exc:  # pragma: no cover - exercised via monkeypatch
        raise CathubDbUnsupported(
            "reading a cathub .db needs ase — pip install 'precis-mcp[import]'"
        ) from exc
    return connect


def _geom_from_atoms(atoms: Any) -> dict[str, Any]:
    """ASE ``Atoms`` -> the ``InputFile``-shaped geometry dict that
    :func:`catalysis_hub._scene_from_input_file` decodes (cell / pbc /
    numbers / positions / ``FixAtoms`` constraints)."""
    from ase.constraints import FixAtoms

    fixed = sorted(
        {int(i) for c in atoms.constraints if isinstance(c, FixAtoms) for i in c.index}
    )
    return {
        "cell": [[float(x) for x in row] for row in atoms.cell[:]],
        "pbc": [bool(p) for p in atoms.pbc],
        "numbers": [int(n) for n in atoms.numbers],
        "positions": [[float(x) for x in pos] for pos in atoms.positions],
        "constraints": (
            [{"name": "FixAtoms", "kwargs": {"indices": fixed}}] if fixed else []
        ),
    }


def _matches_surface(surface: str | None, surface_contains: list[str] | None) -> bool:
    if not surface_contains:
        return True
    s = (surface or "").lower()
    return any(e.lower() in s for e in surface_contains)


def _matches_facet(facet: str | None, want: str | None) -> bool:
    if want is None:
        return True
    return str(facet or "").startswith(want)


def _is_adsorbate_product(name: str, product_names: set[str]) -> bool:
    """True for the adsorbate-on-slab product system, robust to reaction
    direction. A cathub reaction *usually* reads ``star + Xgas -> Xstar`` so the
    only product key is the adsorbate — but a desorption-direction reaction
    could list ``star`` or a bare gas species (``NOgas``) as a product. Those
    are never the substrate the quest wants, so exclude them explicitly rather
    than trusting the `products` keys blindly (matches the documented
    "clean-slab / gas refs are skipped" guarantee)."""
    return name in product_names and name != "star" and not name.endswith("gas")


def read_cathub_db(
    path: str,
    *,
    surface_contains: list[str] | None = None,
    facet: str | None = None,
    product_contains: list[str] | None = None,
    limit: int | None = None,
) -> list[dict[str, Any]]:
    """Read a local cathub ``.db`` into raw records for
    :func:`catalysis_hub.adapter`.

    One record per **product adsorbate system** (``reaction_system.name`` in the
    reaction's ``products``) that passes the filters:

    * ``surface_contains`` — keep a reaction only if its ``surface_composition``
      contains any of these element strings (e.g. ``['Pd','Cu','Ni']``).
    * ``facet`` — keep only reactions whose facet starts with this (``'111'``).
    * ``product_contains`` — keep a product system only if its name contains any
      of these (e.g. ``['NO','NH']`` for NOx intermediates).
    * ``limit`` — stop after this many records.

    Raises:
        CathubDbUnsupported: ``ase`` (the ``[import]`` extra) isn't installed.
    """
    connect = _require_ase()

    # Phase 1: read all reaction + reaction_system + publication metadata via a
    # read-only raw connection, then close it — so ASE's own connection never
    # contends for a lock with ours. Build the file: URI via as_uri() so a path
    # containing URI-reserved chars (``?``/``#``) is percent-encoded, not
    # misparsed as a query/fragment.
    conn = sqlite3.connect(f"{Path(path).resolve().as_uri()}?mode=ro", uri=True)
    try:
        pubs: dict[str, str | None] = {}
        for pub_id, doi in conn.execute("select pub_id, doi from publication"):
            pubs[pub_id] = doi
        reactions = conn.execute(
            "select id, surface_composition, facet, reactants, products, "
            "reaction_energy, dft_code, dft_functional, pub_id from reaction"
        ).fetchall()
        systems_by_reaction: dict[int, list[tuple[str, str]]] = {}
        for name, ase_id, rid in conn.execute(
            "select name, ase_id, id from reaction_system"
        ):
            systems_by_reaction.setdefault(rid, []).append((name, ase_id))
    finally:
        conn.close()

    # Phase 2: for each selected product system, pull its ASE atoms + energy.
    adb = connect(path)
    out: list[dict[str, Any]] = []
    for (
        rid,
        surface,
        rfacet,
        reactants,
        products,
        reaction_energy,
        dft_code,
        dft_functional,
        pub_id,
    ) in reactions:
        if not _matches_surface(surface, surface_contains):
            continue
        if not _matches_facet(rfacet, facet):
            continue
        product_names = set(json.loads(products or "{}"))
        for name, ase_id in systems_by_reaction.get(rid, []):
            if not _is_adsorbate_product(name, product_names):
                continue  # only adsorbate-on-slab product systems (not slab/gas)
            if product_contains and not any(
                pc.lower() in name.lower() for pc in product_contains
            ):
                continue
            row = adb.get(unique_id=ase_id)
            atoms = row.toatoms()
            raw = {
                "reactionEnergy": reaction_energy,
                "facet": rfacet,
                "surfaceComposition": surface,
                "reactants": json.loads(reactants) if reactants else None,
                "products": json.loads(products) if products else None,
                "dftCode": dft_code,
                "dftFunctional": dft_functional,
                "publication": {"doi": pubs.get(pub_id)},
                "system": {
                    "uniqueId": ase_id,
                    "energy": getattr(row, "energy", None),
                    "InputFile": _geom_from_atoms(atoms),
                },
            }
            out.append(raw)
            if limit is not None and len(out) >= limit:
                return out
    return out


@dataclass
class ImportSummary:
    """What a :func:`batch_import` run did — for the CLI/log and tests."""

    configs: int = 0  # records the reader yielded (post-filter)
    imported: int = 0  # newly minted structure refs
    reused: int = 0  # re-imports that collapsed onto an existing ref
    ref_ids: list[int] = field(default_factory=list)


def batch_import(
    store: Store,
    path: str,
    *,
    surface_contains: list[str] | None = None,
    facet: str | None = None,
    product_contains: list[str] | None = None,
    limit: int | None = None,
) -> ImportSummary:
    """Mine a local cathub ``.db`` into ``structure`` refs (batch mirror).

    Each product config is normalised by the shared ``catalysis-hub`` adapter and
    written through :meth:`store.structure_import`, which is idempotent on
    ``(dataset, config_id)`` — so a re-run collapses onto the existing refs
    rather than duplicating them.
    """
    from .catalysis_hub import adapter

    records = read_cathub_db(
        path,
        surface_contains=surface_contains,
        facet=facet,
        product_contains=product_contains,
        limit=limit,
    )
    summary = ImportSummary(configs=len(records))
    for raw in records:
        scene, run, eid = adapter(raw)
        existed = (
            store.find_ref_by_identifier(
                eid.dataset.strip().lower(), eid.config_id.strip(), kind="structure"
            )
            is not None
        )
        ref_id = store.structure_import(scene, run, eid)
        summary.ref_ids.append(ref_id)
        if existed:
            summary.reused += 1
        else:
            summary.imported += 1
    return summary


__all__ = [
    "CathubDbUnsupported",
    "ImportSummary",
    "batch_import",
    "read_cathub_db",
]
