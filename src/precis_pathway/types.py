"""Shared payload shapes for the ``precis_pathway`` package.

``runner.py`` is the pure autocatpath↔precis bridge (0 dataclasses until
now) — every function on its public surface built and consumed plain
``dict[str, Any]`` payloads, so a key rename on one side of a call
(``runner`` -> ``handler``/``persist``/the job glue) could only be caught
by a runtime ``KeyError``, never by mypy. These TypedDicts name the
envelopes ``runner.py`` itself assembles (:class:`NetworkTopology`,
:class:`PathwayArtifact`, :class:`SeedPartialResult`,
:class:`DetachedHandle`, :class:`PollResult`) so a drift trips a type
error at the call site instead.

Nested blobs that originate from autocatpath's own untyped ``dict``
returns (``results_json``/``graph_json`` via ``autocatpath.pipeline.
analyze``, the raw ``run_one_seed`` partial) stay ``dict[str, Any]`` —
autocatpath has no type surface of its own to mirror, so pretending
otherwise here would just be a second, driftable copy of its shape. This
is a shape-documentation pass, not a schema for autocatpath's internals.
"""

from __future__ import annotations

from typing import Any, TypedDict


class TopologyState(TypedDict):
    """One intermediate in a :func:`runner.network_topology` preview."""

    name: str
    label: str
    composition: dict[str, int]


class TopologyStep(TypedDict):
    """One elementary reaction step in a :func:`runner.network_topology`
    preview."""

    name: str
    reactant: str
    product: str


class TopologyLink(TypedDict):
    """One stoichiometry supply edge in a :func:`runner.network_topology`
    preview."""

    reactant: str
    product: str


class NetworkTopology(TypedDict):
    """:func:`runner.network_topology`'s return — the cheap, rule-based,
    no-ML network preview ``PathwayHandler._preview``/``_mermaid``/
    ``_intermediates`` render (and stash under ``meta.topology``)."""

    strategy: str
    substrate: str
    target: str
    element: str
    order: list[str]
    states: list[TopologyState]
    steps: list[TopologyStep]
    links: list[TopologyLink]


class PathwayArtifact(TypedDict):
    """A self-contained, JSON-serialisable autocatpath run —
    :func:`runner.run_pathway`/:func:`runner.run_pathway_from_yaml`/
    :func:`runner.aggregate_seed_partials`'s return, and everything
    ``persist.persist_result`` writes onto a ``pathway`` ref."""

    content_key: str
    autocatpath_version: str
    config: dict[str, Any]
    config_snapshot_yaml: str
    results_json: dict[str, Any]
    graph_json: dict[str, Any]
    methods_md: str
    structures_extxyz: dict[str, str]
    warnings: list[str]


class SeedStructureEntry(TypedDict):
    """One state's lowest-energy relaxed geometry harvested by a
    ``model_index == 0`` seed unit (:func:`runner.run_seed_partial`)."""

    energy: float
    extxyz: str


class SeedPartialResult(TypedDict):
    """:func:`runner.run_seed_partial`'s return — one ``(model, seed)``
    unit's JSON-serialisable partial. Consumed by ``seed_job`` (stashed
    onto the job's own meta) and merged by
    :func:`runner.aggregate_seed_partials`."""

    seed: int
    model: str
    model_index: int
    partial: dict[str, Any]
    lattice: dict[str, float]
    structures: dict[str, SeedStructureEntry]


class DetachedHandle(TypedDict):
    """:func:`runner.submit_seed_partial_detached`'s return — persisted
    onto ``meta.compute_handle``; the handle
    :func:`runner.poll_seed_partial_detached`/
    :func:`runner.kill_seed_partial_detached` operate on."""

    pid: int
    pgid: int
    dir: str
    started_at: float


class PollResult(TypedDict, total=False):
    """:func:`runner.poll_seed_partial_detached`'s return — a
    ``state``-discriminated union collapsed into one dict (mirrors the
    runtime ``dict[str, Any]`` literals it actually builds, so `total=False`
    rather than three separate TypedDicts). ``state`` is ALWAYS present
    (``"running"`` | ``"done"`` | ``"failed"``); the rest is state-specific:
    ``"done"`` carries ``result``/``tail``; ``"failed"`` carries
    ``error``/``tail``/(only on the no-envelope branch) ``infra``.
    """

    state: str
    result: SeedPartialResult
    error: str
    tail: str
    infra: bool


__all__ = [
    "DetachedHandle",
    "NetworkTopology",
    "PathwayArtifact",
    "PollResult",
    "SeedPartialResult",
    "SeedStructureEntry",
    "TopologyLink",
    "TopologyState",
    "TopologyStep",
]
