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


@dataclass
class ExternalRun:
    """The run-cube payload an import pre-fills.

    Lands as one ``struct_runs`` row alongside the imported ``Scene`` —
    energy + forces + relaxed geometry the source already computed, plus the
    §4 ``method`` fingerprint that keeps it from being naively compared
    against a different functional/cutoff/k-mesh. ``provenance`` defaults to
    ``"external"`` (vs ``"computed"``) so an imported row is never mistaken
    for — or silently overwritten by — our own compute.
    """

    energy: float
    max_force: float | None
    final_geometry: dict | None  # relaxed geometry payload, JSONB-serializable
    method: dict = field(default_factory=dict)  # functional/cutoff_eV/kmesh/spin/...
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
    "get_adapter",
    "register_adapter",
]
