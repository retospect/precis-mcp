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

What gets imported (slice A1): per selected reaction, **every member
system** — the clean slab (``star``), the gas-phase references
(``NOgas``) and the product adsorbate system (``NOstar``) alike — each as
its own ``structure`` ref whose external run carries *its* DFT total
energy. Only the adsorbate row additionally carries ``reference``
payloads (the stated ``reaction_energy`` — an adsorption energy for
``star + Xgas -> Xstar`` — and any ``activation_energy``), whose
``members`` name every member system's config_id so they resolve inside
the store. Systems are emitted once per batch even when several selected
reactions share them; a shared adsorbate's record merges the references
of every reaction that produces it (``producing``), while a shared slab
or gas row carries no reaction-specific terms at all.

Unit enclave (`precis.structure` package docstring): eV/Å-native, matching
the ASE ``systems`` it reads; energies are never converted to SI here.
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
            "reading a cathub .db needs ase (core dep — broken venv?): "
            "pip install --force-reinstall 'precis-mcp'"
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

    One record per **member system** of each selected reaction — clean
    slab, gas references and adsorbate product alike — so a reference's
    ``members`` resolve inside the store. A system shared by several
    selected reactions emits once: slab/gas rows then carry no
    ``reactants``/``products`` (no last-writer-wins), while an adsorbate
    row keeps its terms and merges every producing reaction's reference
    fields under ``producing``:

    * ``surface_contains`` — keep a reaction only if its ``surface_composition``
      contains any of these element strings (e.g. ``['Pd','Cu','Ni']``).
    * ``facet`` — keep only reactions whose facet starts with this (``'111'``).
    * ``product_contains`` — keep a reaction only if one of its adsorbate
      product systems' names contains any of these (e.g. ``['NO','NH']``
      for NOx intermediates).
    * ``limit`` — stop after this many *reactions*. A reaction's members
      are never split: all of them emit, or the reaction is not selected.

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
            "reaction_energy, activation_energy, dft_code, dft_functional, "
            "pub_id from reaction"
        ).fetchall()
        systems_by_reaction: dict[int, list[tuple[str, str]]] = {}
        for name, ase_id, rid in conn.execute(
            "select name, ase_id, id from reaction_system"
        ):
            systems_by_reaction.setdefault(rid, []).append((name, ase_id))
    finally:
        conn.close()

    # Phase 2: select the reactions — filters first, ``limit`` counting
    # selected reactions only. A reaction contributes when it has at least
    # one adsorbate product passing the filters; then ALL its systems emit
    # (the clean slab and gas references are what a ``reference.members``
    # resolves to), and its members are never split by the limit.
    picked: list[tuple[tuple, set[str], list[tuple[str, str]]]] = []
    for reaction in reactions:
        rid, surface, rfacet, _r, products, _e, _a, _c, _f, _p = reaction
        if not _matches_surface(surface, surface_contains):
            continue
        if not _matches_facet(rfacet, facet):
            continue
        product_names = set(json.loads(products or "{}"))
        systems = systems_by_reaction.get(rid, [])
        adsorbate_names = {
            name for name, _ in systems if _is_adsorbate_product(name, product_names)
        }
        if not adsorbate_names:
            continue  # the reaction has no adsorbate-on-slab product at all
        if product_contains and not any(
            pc.lower() in name.lower()
            for name in adsorbate_names
            for pc in product_contains
        ):
            continue
        if limit is not None and len(picked) >= limit:
            break
        picked.append((reaction, product_names, systems))

    # Each ase_id emits ONCE per batch: real .db files share systems across
    # reactions, and re-emitting would re-import the same system N times.
    # Counts and producers are over the SELECTED reactions only — a system
    # also used by a filtered-out reaction is not "shared" here.
    #
    # ``producing`` gathers every selected reaction whose adsorbate product
    # a system is. A shared adsorbate's one emitted record still carries
    # every producing reaction's references — its adsorption energy in R1
    # must not vanish because R2 also lists it as a member (NOstar in
    # ``star + NOgas -> NOstar`` AND ``NOstar + star -> Nstar + Ostar``).
    ase_use_count: dict[str, int] = {}
    producing: dict[str, list[dict[str, Any]]] = {}
    for reaction, product_names, systems in picked:
        (
            _id,
            _s,
            _f,
            reactants,
            products,
            reaction_energy,
            activation_energy,
            _c,
            _n,
            _b,
        ) = reaction
        member_ids = [ase_id for _name, ase_id in systems]
        for name, ase_id in systems:
            ase_use_count[ase_id] = ase_use_count.get(ase_id, 0) + 1
            if _is_adsorbate_product(name, product_names):
                producing.setdefault(ase_id, []).append(
                    {
                        "reactants": json.loads(reactants) if reactants else None,
                        "products": json.loads(products) if products else None,
                        "reactionEnergy": reaction_energy,
                        "activationEnergy": activation_energy,
                        "member_ids": member_ids,
                    }
                )

    adb = connect(path)
    out: list[dict[str, Any]] = []
    emitted: set[str] = set()
    for reaction, _pn, systems in picked:
        (
            rid,
            surface,
            rfacet,
            reactants,
            products,
            reaction_energy,
            activation_energy,
            dft_code,
            dft_functional,
            pub_id,
        ) = reaction
        member_ids = [ase_id for _name, ase_id in systems]
        for name, ase_id in systems:
            if ase_id in emitted:
                continue
            emitted.add(ase_id)
            prod = producing.get(ase_id)
            # Role decides whose terms the descriptor carries. A clean slab
            # or gas species shared by selected reactions gets NONE — one
            # reaction's terms would be last-writer-wins noise for a system
            # that equally serves another. An adsorbate keeps its terms —
            # they are what its references key on — taken from a reaction
            # that produces it (a system can be one reaction's product and
            # only a member of the next).
            shared_species = (name == "star" or name.endswith("gas")) and (
                ase_use_count.get(ase_id, 0) > 1
            )
            if shared_species:
                row_reactants = row_products = None
            elif prod:
                row_reactants = prod[0]["reactants"]
                row_products = prod[0]["products"]
            else:
                row_reactants = json.loads(reactants) if reactants else None
                row_products = json.loads(products) if products else None
            row = adb.get(unique_id=ase_id)
            atoms = row.toatoms()
            raw = {
                "reactionEnergy": reaction_energy,
                "activationEnergy": activation_energy,
                "facet": rfacet,
                "surfaceComposition": surface,
                "reactants": row_reactants,
                "products": row_products,
                "dftCode": dft_code,
                "dftFunctional": dft_functional,
                "publication": {"doi": pubs.get(pub_id)},
                "name": name,
                "member_ids": member_ids,
                "system": {
                    "uniqueId": ase_id,
                    "energy": getattr(row, "energy", None),
                    "InputFile": _geom_from_atoms(atoms),
                },
            }
            if prod:
                raw["producing"] = prod
            out.append(raw)
    return out


@dataclass
class ImportSummary:
    """What a :func:`batch_import` run did — for the CLI/log and tests."""

    configs: int = 0  # records the reader yielded (post-filter)
    imported: int = 0  # newly minted structure refs
    reused: int = 0  # re-imports that collapsed onto an existing ref
    skipped: int = 0  # records dropped for carrying no DFT total energy
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

    Each member system is normalised by the shared ``catalysis-hub`` adapter
    and written through :meth:`store.structure_import`, which is idempotent
    on ``(dataset, config_id)`` — so a re-run collapses onto the existing
    refs rather than duplicating them. A record whose system carries no DFT
    total energy is skipped (and counted), never imported with a fabricated
    one.
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
        if run.energy is None:
            summary.skipped += 1
            continue
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
