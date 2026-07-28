"""In-process autocatpath run → a self-contained, JSON-serialisable artifact.

This is the *pure* half of the precis bridge: it imports **only autocatpath**
(no ``precis``), so it is importable and testable without precis-mcp
installed. The precis-facing handler (``precis_pathway.handler``) calls
``run_pathway`` and persists what it returns.

It mirrors :func:`autocatpath.pipeline.write_outputs` — same graph, same
``results.json`` shape, same ``methods.md`` — but assembles everything
*in memory* and skips the matplotlib PNG rendering (deferred to a later
slice), so it stays cheap and has no render-backend dependency.

Slice 0 runs the whole pipeline inline on the EMT backend. Fan-out across
``(model, seed)`` and heavy backends move to the precis compute lane in
slice 1 (see ``docs/design/autocatpath-integration.md`` in precis-mcp).
"""

from __future__ import annotations

import hashlib
import io
import json
from typing import Any

from autocatpath import __version__, provenance
from autocatpath.config import Config
from autocatpath.graph import build_graph
from autocatpath.pipeline import Results, g_has_edge, run


def network_topology(config: dict[str, Any]) -> dict[str, Any]:
    """Build the reaction network **cheaply** (rule-based, NO ML) and return its
    structure as plain data: intermediates (with composition), atom-conserving
    elementary steps, and stoichiometry supply links.

    This is the "argue before you compute" surface — the LLM can inspect and
    contest the network (is this intermediate real? is this step right?) before
    any relax/NEB is spent. No slab is built and no calculator is loaded, so it
    is fast and dependency-light (ASE/RDKit only, no potential)."""
    from autocatpath.network import build_network

    cfg = Config.from_dict(config)
    net = build_network(
        cfg.slab,
        cfg.network,
        cfg.reagents,
        cfg.substrate,
        cfg.target,
        max_extra=cfg.auto.max_extra,
        max_states=cfg.auto.max_states,
    )
    states = net.states()
    order = net.order()
    return {
        "strategy": cfg.network,
        "substrate": cfg.substrate,
        "target": cfg.target,
        "element": cfg.slab.element,
        "order": order,
        "states": [
            {
                "name": n,
                "label": states[n].label,
                "composition": dict(states[n].adsorbate_counts()),
            }
            for n in order
            if n in states
        ],
        "steps": [
            {"name": s.name, "reactant": s.reactant.name, "product": s.product.name}
            for s in net.steps
        ],
        "links": [{"reactant": a, "product": b} for a, b in net.links],
    }


def _prep(config: dict[str, Any], force_backend: str | None) -> Config:
    """Build the Config, applying a backend override (used by the run and by
    the cache-key computation so they never diverge)."""
    cfg = Config.from_dict(config)
    if force_backend:
        cfg.mlip.backend = force_backend
        cfg.mlip.models = []
    return cfg


def effective_config(
    config: dict[str, Any], *, force_backend: str | None = None
) -> dict[str, Any]:
    """The normalised config that WILL run (post-backend-override). The handler
    keys the regen cache on this and mints the compute job with it, so the
    in-process and routed paths address the same content."""
    return _prep(config, force_backend).to_dict()


def content_key(config: dict[str, Any]) -> str:
    """Content address for a pathway run: the config + the autocatpath version.

    Regen is keyed on this — an unchanged config against an unchanged
    autocatpath produces the same key, so the handler can skip re-running.
    Deterministic: keys sorted, floats left as-is (config values are
    small and exact). The autocatpath version is folded in so a code bump
    invalidates stale artifacts (autocatpath itself does no hashing —
    provenance is deterministic text only, so precis owns the key).
    """
    canonical = json.dumps(config, sort_keys=True, separators=(",", ":"))
    payload = f"{canonical}\x00autocatpath=={__version__}"
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _summary(cfg: Config, results: Results) -> dict[str, Any]:
    """The ``results.json`` payload, byte-for-concept identical to
    :func:`autocatpath.pipeline.write_outputs`."""
    from autocatpath.calculators import resolve_backend

    return {
        "name": cfg.name,
        "substrate": cfg.substrate,
        "target": cfg.target,
        "backend": resolve_backend(cfg.mlip.backend),
        "models": results.models,
        "seeds": cfg.search.seeds,
        "n_samples": max(1, len(results.models)) * len(cfg.search.seeds),
        "relaxed_lattice_A": results.lattice,
        "energy_reference": f"relative to substrate state '{results.pathway[0]}'",
        "pathway": results.pathway,
        "nodes": {k: v.as_dict() for k, v in results.node_energies.items()},
        "edges": [
            {
                "name": e["name"],
                "reactant": e["reactant"],
                "product": e["product"],
                "barrier": e["barrier"].as_dict(),
                "delta_e": e["delta_e"].as_dict(),
            }
            for e in results.edges
        ],
        # adsorption barrier the dissolving tether had to overcome to reseat a
        # desorbing fragment (per state, plus the pathway max as a single trust /
        # annotation signal precis can harvest). 0 => barrierless (spurious
        # desorption legitimately rescued); large => genuine activated adsorption.
        "adsorption_barriers": dict(results.ads_barriers),
        "adsorption_barrier": (
            max(results.ads_barriers.values()) if results.ads_barriers else 0.0
        ),
        "warnings": results.warnings,
    }


def _graph_json(results: Results) -> dict[str, Any]:
    """The reaction DAG as node-link JSON (mirrors ``graph.to_json``),
    including the dashed 'supply' edges ``write_outputs`` adds."""
    import networkx as nx

    ref = results.node_energies[results.pathway[0]].mean
    edges = list(results.edges)
    for a, b in results.links:
        if not g_has_edge(results.edges, a, b):
            edges.append(
                {"name": f"{a}->{b}", "reactant": a, "product": b, "kind": "supply"}
            )
    g = build_graph(results.node_energies, edges, energy_ref=ref)
    return nx.node_link_data(g, edges="links")


def _structures_extxyz(results: Results) -> dict[str, str]:
    """Serialise the lowest-energy relaxed Atoms per state to extxyz strings.

    Not ingested in slice 0, but harvested here so slice 1's
    ``Scene.from_ase`` ingest (structure refs → pathway nodes) has the
    geometries ready. extxyz is lossless (cell, pbc, per-atom info).
    """
    from ase.io import write as ase_write

    out: dict[str, str] = {}
    for name, atoms in results.structures.items():
        buf = io.StringIO()
        ase_write(buf, atoms, format="extxyz")
        out[name] = buf.getvalue()
    return out


def _hydrate_slab(slab_extxyz: str) -> Any:
    """Parse an extxyz string (one frame) into an ASE ``Atoms`` slab.

    extxyz is the wire form the precis ``structure`` seam hands us — lossless
    for cell / pbc / positions / constraints, and JSON-embeddable as a string,
    so ``run_pathway`` keeps its "plain data in, plain data out" contract.
    """
    from ase.io import read as ase_read

    atoms = ase_read(io.StringIO(slab_extxyz), format="extxyz", index=0)
    return atoms


def run_pathway(
    config: dict[str, Any],
    *,
    force_backend: str | None = None,
    slab_extxyz: str | None = None,
    log: Any = lambda *a, **k: None,
) -> dict[str, Any]:
    """Run autocatpath in-process and return a self-contained artifact.

    ``config`` is the parsed pathway YAML (a plain dict). ``force_backend``
    overrides ``mlip.backend`` (slice 0 pins ``emt`` so an unconfigured or
    heavy-backend request still runs the cheap in-process path).
    ``slab_extxyz`` (optional) is an externally-prepared slab — the precis
    ``structure`` seam: when given, autocatpath scores *that* slab instead of
    building an fcc(111) one from the config label, and the reaction's
    adsorbates are placed on it (clean-fcc(111) first cut). ``log`` is a
    autocatpath-style logging callable (default: silent).

    The returned dict is JSON-serialisable end to end (no ASE ``Atoms``
    leak into it) and carries everything the handler persists:

    * ``content_key`` — regen/cache address
    * ``config`` / ``config_snapshot_yaml`` — the authoritative IR + provenance
    * ``results_json`` — energies, barriers, pooled uncertainty, warnings
    * ``graph_json`` — the reaction DAG (node-link)
    * ``methods_md`` — the citable methods paragraph
    * ``structures_extxyz`` — relaxed geometries per state (for slice-1 ingest)
    """
    cfg = _prep(config, force_backend)
    # Key on the EFFECTIVE config (post-override) so the cache address
    # matches what actually ran — not the raw request.
    effective = cfg.to_dict()
    if slab_extxyz is not None:
        # Side-channel: a runtime attr `_build_net` stamps onto the Network.
        # Not a dataclass field, so it stays out of `effective`/content_key —
        # the injected geometry addresses via `config.slab.structure_ref`
        # (set by the caller) rather than the Atoms bytes.
        cfg._prebuilt_slab = _hydrate_slab(slab_extxyz)  # type: ignore[attr-defined]
    results = run(cfg, log=log)

    return {
        "content_key": content_key(effective),
        "autocatpath_version": __version__,
        "config": effective,
        "config_snapshot_yaml": _snapshot_yaml(cfg),
        "results_json": _summary(cfg, results),
        "graph_json": _graph_json(results),
        "methods_md": provenance.methods_text(cfg, results),
        "structures_extxyz": _structures_extxyz(results),
        "warnings": list(results.warnings),
    }


def run_pathway_from_yaml(
    text: str,
    *,
    force_backend: str | None = None,
    log: Any = lambda *a, **k: None,
) -> dict[str, Any]:
    """Parse a pathway config YAML and run it. Uses autocatpath's chem-safe
    loader, so ``substrate: NO`` stays the string ``"NO"`` (YAML 1.1 would
    coerce it to ``False``)."""
    from autocatpath.config import _load_yaml

    return run_pathway(_load_yaml(text), force_backend=force_backend, log=log)


def _snapshot_yaml(cfg: Config) -> str:
    import yaml

    return yaml.safe_dump(cfg.to_dict(), sort_keys=False)
