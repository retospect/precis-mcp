"""The adapter registry — external DFT library import's ETL seam for external DFT catalyst DBs.

Format heterogeneity (AQCat25 JSON, Catalysis-Hub REST, OC20 LMDB, NCCR/Zenodo
tarballs, ...) is absorbed by one **pure adapter per source**, each a plain
function::

    adapter(raw_record) -> (Scene, ExternalRun, ExternalId)

An adapter does no I/O and no DB writes — it just normalises one source's raw
record into the shared IR. The store write-path (``store.structure_import``)
is the only thing that turns that triple into rows, and the ``[import]``-extra
source clients (ASE/``datasets``/``h5py``/``lmdb``) are the only things that
fetch a ``raw_record`` in the first place. This module stays dependency-free
so it can be imported from anywhere (handler, worker, tests) with zero
optional deps installed.

Unit enclave (`precis.structure` package docstring): Å/eV-native, matching
the source DFT DBs' own convention (ASE-native); ``ExternalRun.energy``/
``max_force`` are eV / eV/Å, never converted to SI.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from ..scene import Scene


@dataclass(frozen=True)
class ExternalId:
    """The idempotent collapse key for one imported config.

    Mirrors the ``ref_identifiers`` discipline (AGENTS.md): a re-import of
    the same ``(dataset, config_id)`` updates the existing rows, never
    duplicates them.
    """

    dataset: str
    config_id: str


@dataclass(frozen=True)
class Reference:
    """One typed reference quantity an imported record reports
    (catalyst-library tie-in, slice A1).

    ``energy`` on :class:`ExternalRun` is always the DFT *total* energy of
    that system; a :class:`Reference` is a *derived* quantity the source
    stated against its own member systems — e.g. the adsorption energy of
    ``star + NOgas -> NOstar``. Persisted as a ``measures`` row on the
    imported structure ref by ``store.structure_import``.

    * ``quantity`` — ``adsorption_energy`` | ``reaction_energy`` |
      ``activation_energy``.
    * ``value_eV`` — the source's stated value.
    * ``equation`` — ``{"reactants": [...], "products": [...]}``, each term
      ``{"name", "state" ("gas"|"star"), "stoich"}`` as the source wrote it.
    * ``members`` — config_ids of the systems the value was computed from
      (clean slab, adsorbed state, gas molecules, transition state); each
      resolves to an imported ref via ``(dataset, config_id)``.
    """

    quantity: str
    value_eV: float
    equation: dict
    members: tuple[str, ...] = ()


@dataclass
class ExternalRun:
    """The run-cube payload an import pre-fills.

    Lands as one ``struct_runs`` row alongside the imported ``Scene`` —
    energy + forces + relaxed geometry the source already computed, plus the
    §4 ``method`` fingerprint that keeps it from being naively compared
    against a different functional/cutoff/k-mesh. ``provenance`` defaults to
    ``"external"`` (vs ``"computed"``) so an imported row is never mistaken
    for — or silently overwritten by — our own compute.

    ``energy`` is ``None`` for a geometry-only source (CoRE MOF holds
    experimental, solvent-removed cells and no DFT energy); the row still
    lands, with its ``method`` fingerprint naming the source, so the design
    stays read-only and reports where its atoms came from.

    ``method`` keeps only method fields (``functional``/``code``/
    ``cutoff_eV``/``kmesh``/``spin``/``dataset_doi``) so its fingerprint is a
    pure comparability key; record-level *descriptors* (``facet``,
    ``surface_composition``, ``reactants``, ``products`` — what the record
    describes, not how it was computed) ride in ``descriptor``, stored in the
    ``struct_runs.descriptor`` column (0252). ``references`` carries the
    source's stated derived quantities (a reaction/adsorption/activation
    energy) — never folded into ``energy``.
    """

    energy: float | None
    max_force: float | None
    final_geometry: dict | None  # relaxed geometry payload, JSONB-serializable
    method: dict = field(default_factory=dict)  # functional/cutoff_eV/kmesh/spin/...
    descriptor: dict = field(
        default_factory=dict
    )  # facet/surface_composition/reactants/...
    references: tuple[Reference, ...] = ()
    provenance: str = "external"


class Adapter(Protocol):
    """One pure normaliser per source: ``raw_record -> (Scene, ExternalRun, ExternalId)``."""

    def __call__(self, raw: object) -> tuple[Scene, ExternalRun, ExternalId]: ...


_ADAPTERS: dict[str, Adapter] = {}


def register_adapter(name: str, fn: Adapter) -> None:
    """Register ``fn`` as the adapter for source ``name`` (e.g. ``'catalysis-hub'``)."""
    _ADAPTERS[name] = fn


def get_adapter(name: str) -> Adapter:
    """Look up the adapter registered for ``name``.

    Raises ``ValueError`` (not a bare ``KeyError``) naming the known sources,
    so a bad/unregistered source name fails legibly at the on-demand hydrate
    seam or the batch-import CLI.
    """
    try:
        return _ADAPTERS[name]
    except KeyError:
        known = ", ".join(sorted(_ADAPTERS)) or "(none registered)"
        raise ValueError(
            f"no adapter registered for {name!r}; known: {known}"
        ) from None


__all__ = [
    "Adapter",
    "ExternalId",
    "ExternalRun",
    "Reference",
    "get_adapter",
    "register_adapter",
]
