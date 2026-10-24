"""In-process autocatpath run → a self-contained, JSON-serialisable artifact.

This is the *pure* half of the precis bridge: it imports **only autocatpath**
(no ``precis``), so it is importable and testable without precis-mcp
installed. The precis-facing handler (``precis_pathway.handler``) calls
``run_pathway`` and persists what it returns.

It mirrors :func:`autocatpath.pipeline.write_outputs` — same graph, same
``results.json`` shape, same ``methods.md`` — but assembles everything
*in memory* and skips the matplotlib PNG rendering (deferred to a later
slice), so it stays cheap and has no render-backend dependency.

Slice 0 runs the whole pipeline inline on the EMT backend. §B-1 fans
``run()``'s ``(model, seed)`` loop body out across the precis compute
lane: :func:`run_seed_partial` runs ONE unit
(``autocatpath.pipeline.run_one_seed``), :func:`aggregate_seed_partials`
combines N of them (``autocatpath.pipeline.aggregate_partials``, pure
numpy) back into :func:`run_pathway`'s artifact shape.
``quest.compute.dispatch_autocatpath`` mints the per-seed jobs; the
``autocatpath_seed``/``autocatpath_aggregate`` job_types
(``precis_pathway.seed_job``/``.aggregate_job``) call these two
functions. Heavy backends move to the compute lane the same way
(``docs/backlog/autocatpath-integration.md`` in precis-mcp).
"""

from __future__ import annotations

import copy
import hashlib
import io
import json
import logging
import os
import re
import shutil
import signal
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, cast

from autocatpath import __version__, provenance
from autocatpath.config import Config
from autocatpath.pipeline import Results, run

from .coverage_sites import representable_coverages, slab_top_sites
from .scratch import (
    _cleanup_detached,
    _tail_logs,
)
from .scratch import (
    finalize_seed_partial_detached as finalize_seed_partial_detached,
)
from .scratch import (
    kill_seed_partial_detached as kill_seed_partial_detached,
)
from .step_retry import narrow_partial, narrow_structures
from .types import (
    CoverageScanResult,
    DetachedHandle,
    NetworkTopology,
    PathwayArtifact,
    PollResult,
    SeedPartialResult,
    SeedStructureEntry,
)

log = logging.getLogger(__name__)


class ChildKilledError(RuntimeError):
    """The compute child died by signal (SIGKILL/OOM, a negative
    ``returncode``) or exited without producing its expected
    ``result.json`` — an INFRA-class failure (the environment killed the
    process; the compute never actually ran), never a content-class one.

    Raised only by :func:`run_seed_partial_subprocess` (the blocking
    legacy-dispatch path); the detached submit/poll path
    (:func:`poll_seed_partial_detached`) can't capture a returncode at all
    (nothing keeps the ``Popen`` handle to wait on) so it signals the same
    condition via the returned state dict's ``"infra"`` key instead — see
    its docstring. Both feed
    ``precis_pathway.seed_job``'s ``ctx.record_failure(...,
    open_tag="infra:child-killed")`` call
    (docs/backlog/parked-leaf-recovery.md), the generic layer that stamps
    the tag :data:`precis.utils.job_bubble.INFRA_FAILURE_TAGS` reads to
    bound-retry instead of latching the parent immediately. A plain
    positive nonzero ``returncode`` WITH the result file present (an
    unhandled-but-non-fatal child exception, distinct from an environment
    kill) still raises a bare ``RuntimeError`` — content-class, unchanged.
    """


#: A ``subprocess`` ``returncode`` is negative-N when the child was
#: terminated by signal N (Python's own convention, mirroring the raw
#: POSIX wait status) — never true of a child that exited normally, even
#: with a nonzero exit code.
def _died_by_signal(returncode: int) -> bool:
    return returncode < 0


def network_topology(config: dict[str, Any]) -> NetworkTopology:
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


@dataclass(frozen=True)
class _PreparedConfig:
    """A ``Config`` paired with whether a prebuilt slab was stamped onto it.

    autocatpath itself defines the injection point: ``pipeline.run``/
    ``run_one_seed``/``aggregate_partials`` (via ``_build_net``) read the
    slab back off the ``Config`` instance they're handed via
    ``getattr(cfg, "_prebuilt_slab", None)`` — a runtime attr, not a
    dataclass field, so it stays out of ``to_dict``/``content_key``. That's
    autocatpath's own side-channel, not something precis can avoid by
    calling it differently; the attribute has to land on the actual object
    those functions see. What precis controls is not repeating the
    "``cfg._prebuilt_slab = ...`` write, then a separate bare ``getattr``
    read back" pattern at each of the three call sites — :func:`_stamp_slab`
    does the write exactly once per prepared ``Config`` and returns this
    pairing, so ``slab_injected`` is a typed field callers read instead of
    re-deriving it.
    """

    cfg: Config
    slab_injected: bool


def _stamp_slab(cfg: Config, slab_extxyz: str | None) -> _PreparedConfig:
    """Stamp ``cfg`` with the prebuilt-slab side-channel (if given) and
    return it paired with whether the stamp happened. The injected geometry
    addresses via ``config.slab.structure_ref`` (set by the caller) rather
    than the Atoms bytes, so the stamp itself never leaks into
    ``effective``/``content_key`` — only ``Config.from_dict`` output does."""
    injected = slab_extxyz is not None
    if slab_extxyz is not None:
        cfg._prebuilt_slab = _hydrate_slab(slab_extxyz)
    return _PreparedConfig(cfg=cfg, slab_injected=injected)


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


def _analysis_payloads(
    cfg: Config, results: Results
) -> tuple[dict[str, Any], dict[str, Any]]:
    """``(results_json, graph_json)`` — catpath's own analysis, verbatim.

    Delegates to ``autocatpath.pipeline.analyze`` (>= 0.5.2, the pure
    extraction of ``write_outputs``'s analysis half) so the precis artifact
    carries the SAME summary catpath writes standalone — including the
    ``traps`` / ``poisons`` / ``selectivity`` sections and the CHE scalars —
    and the same graph (supply edges with stamped delta_e, gas-ledger
    labels, CHE n_H stamps). This replaces a hand-mirrored summary dict
    that had already drifted (it silently lacked the CHE keys
    ``_dispatch_common`` reads — a dead pass-through this delegation
    retires) and a local graph rebuild.

    Augments the summary with the precis-only adsorption-barrier trust
    signal (the tether's reseat climb — not part of catpath's own summary).
    """
    import networkx as nx
    from autocatpath.pipeline import analyze

    an = analyze(cfg, results, log=lambda *a, **k: None)
    summary = dict(an.summary)
    # adsorption barrier the dissolving tether had to overcome to reseat a
    # desorbing fragment (per state, plus the pathway max as a single trust /
    # annotation signal precis can harvest). 0 => barrierless (spurious
    # desorption legitimately rescued); large => genuine activated adsorption.
    summary.setdefault("adsorption_barriers", dict(results.ads_barriers))
    summary.setdefault(
        "adsorption_barrier",
        max(results.ads_barriers.values()) if results.ads_barriers else 0.0,
    )
    return summary, nx.node_link_data(an.graph, edges="links")


def _stamp_endpoint_tol(results: Results) -> None:
    """Fold the endpoint-agreement tolerance the engine applied into each
    ``endpoint_agreement`` trust record's evidence (``tol_eV``, eV).

    ``autocatpath.trust.endpoint_agreement`` fails a run whose endpoint
    energy sits more than ``ENDPOINT_TOL_EV`` above the (model, state)
    group minimum — today an engine-side constant the artifact never
    recorded, so a ``fail``'s ``delta_meV`` arrived uninterpretable
    (gr508948: a near-universal endpoint-mismatch pattern is only
    diagnosable as chemistry-vs-tolerance-artifact when the reader can see
    the line the deltas were judged against). ``setdefault`` keeps an
    engine that starts minting its own per-call tolerance authoritative; a
    silent no-op where the constant does not exist — the records still
    carry verdict + delta.
    """
    from autocatpath import trust as _trust_mod

    tol = getattr(_trust_mod, "ENDPOINT_TOL_EV", None)
    if isinstance(tol, bool) or not isinstance(tol, (int, float)):
        return
    for rec in getattr(results, "trust", None) or ():
        if (
            isinstance(rec, dict)
            and rec.get("check") == "endpoint_agreement"
            and isinstance(rec.get("evidence"), dict)
        ):
            rec["evidence"].setdefault("tol_eV", float(tol))


def _atoms_to_extxyz(atoms: Any) -> str:
    """One relaxed ASE ``Atoms`` → an extxyz string (lossless: cell, pbc,
    per-atom info). The wire form the precis ``structure`` seam ingests."""
    from ase.io import write as ase_write

    buf = io.StringIO()
    ase_write(buf, atoms, format="extxyz")
    return buf.getvalue()


def _structures_extxyz(results: Results) -> dict[str, str]:
    """Serialise the lowest-energy relaxed Atoms per state to extxyz strings.

    Harvested so the slice-1b ``Scene.from_ase`` ingest (structure refs →
    pathway nodes) has the geometries ready.
    """
    return {name: _atoms_to_extxyz(atoms) for name, atoms in results.structures.items()}


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
) -> PathwayArtifact:
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
    prepared = _stamp_slab(cfg, slab_extxyz)
    results = run(prepared.cfg, log=log)
    _stamp_endpoint_tol(results)

    results_json, graph_json = _analysis_payloads(prepared.cfg, results)
    return {
        "content_key": content_key(effective),
        "autocatpath_version": __version__,
        "config": effective,
        "config_snapshot_yaml": _snapshot_yaml(prepared.cfg),
        "results_json": results_json,
        "graph_json": graph_json,
        "methods_md": provenance.methods_text(prepared.cfg, results),
        "structures_extxyz": _structures_extxyz(results),
        "warnings": list(results.warnings),
    }


def run_pathway_from_yaml(
    text: str,
    *,
    force_backend: str | None = None,
    log: Any = lambda *a, **k: None,
) -> PathwayArtifact:
    """Parse a pathway config YAML and run it. Uses autocatpath's chem-safe
    loader, so ``substrate: NO`` stays the string ``"NO"`` (YAML 1.1 would
    coerce it to ``False``)."""
    from autocatpath.config import _load_yaml

    return run_pathway(_load_yaml(text), force_backend=force_backend, log=log)


# ── §B-1 seed fan-out: run ONE (model, seed) unit, aggregate N of them ─────


def model_specs(
    config: dict[str, Any], *, force_backend: str | None = None
) -> list[tuple[str, str | None]]:
    """The ``(backend, model)`` pairs this config runs — ``cfg.mlip.specs()``,
    exposed so a caller (``quest.compute.dispatch_autocatpath``) can size its
    fan-out without duplicating ``MLIPConfig.specs()``'s own logic."""
    return _prep(config, force_backend).mlip.specs()


def seed_content_key(
    config: dict[str, Any],
    seed: int,
    model_index: int,
    *,
    force_backend: str | None = None,
) -> str:
    """Content address for ONE ``(model, seed)`` unit: the effective config +
    the seed + which spec in ``mlip.specs()`` + the autocatpath version.

    Mirrors :func:`content_key` (same effective-config + version fold) with
    ``seed``/``model_index`` appended — the version fold is load-bearing the
    same way it is there (a redeployed autocatpath re-keys every seed rather
    than dedup-pinning stale partials, the qu164903 fix)."""
    effective = effective_config(config, force_backend=force_backend)
    canonical = json.dumps(effective, sort_keys=True, separators=(",", ":"))
    payload = (
        f"{canonical}\x00autocatpath=={__version__}"
        f"\x00seed={seed}\x00model_index={model_index}"
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def root_state(config: dict[str, Any]) -> str:
    """The network's root (``Network.order()[0]`` — the clean slab, the
    energy zero every partial is referenced to in ``aggregate_partials``).
    Built the way the engine's own ``_build_net`` builds it (template /
    desorb / leavers included), rule-based, no ML."""
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
        template=cfg.template,
        desorb=cfg.auto.desorb,
        leavers=cfg.auto.leavers,
    )
    return str(net.order()[0])


def run_seed_partial(
    config: dict[str, Any],
    seed: int,
    model_index: int,
    *,
    force_backend: str | None = None,
    slab_extxyz: str | None = None,
    only_steps: Sequence[str] | None = None,
    log: Any = lambda *a, **k: None,
) -> SeedPartialResult:
    """Run ONE ``(model, seed)`` unit of a pathway exploration.

    ``only_steps`` (the step-level retry, :mod:`precis_pathway.step_retry`)
    narrows what this unit RECORDS to the named step(s): the pinned engine
    has no single-step entry point, so the network still runs whole, and
    the partial is reduced afterwards (:func:`~precis_pathway.step_retry.
    narrow_partial` — step entry, its trust records, endpoint + root
    energies) so the retry's evidence is exactly the one measurement the
    aggregate replaces. One step per retry in practice; a sequence is
    accepted so a future engine step filter can take the same argument.

    The precis-side wrapper around ``autocatpath.pipeline.run_one_seed`` —
    the function autocatpath built for exactly this ("deliberately
    standalone and JSON-serialisable so an orchestrator can fan out seeds
    across jobs", per its docstring). Mirrors the per-model prep
    ``pipeline.run()`` does inline for the spec at ``model_index`` (backend
    resolve + bulk-lattice relax to that potential) so a fanned-out seed job
    reproduces exactly what the monolith would have done for that unit —
    then calls ``run_one_seed``.

    Returns a JSON-serialisable dict (no ASE ``Atoms`` leak): ``partial``
    (the raw ``run_one_seed`` result, ``model`` tag folded in — same shape
    ``pipeline.run()`` accumulates), ``model`` (the resolved tag string),
    ``lattice`` (``{tag: relaxed_a}`` if this unit relaxed the lattice, else
    ``{}`` — ``aggregate_seed_partials`` merges these back in), and
    ``structures`` (``{state: {"energy": float, "extxyz": str}}`` — the
    lowest-energy relaxed geometry this unit saw per state, serialised to
    extxyz so it survives the JSON job boundary; ``aggregate_seed_partials``
    keeps the min-energy geometry across seeds/models and hands it to the
    native structure ingest, slice-1b).
    """
    from autocatpath.calculators import make_calculator, resolve_backend
    from autocatpath.pipeline import run_one_seed
    from autocatpath.structures import default_lattice, equilibrium_lattice

    cfg = _prep(config, force_backend)
    specs = cfg.mlip.specs()
    if not (0 <= model_index < len(specs)):
        raise ValueError(
            f"model_index={model_index} out of range for {len(specs)} spec(s)"
        )
    backend, model = specs[model_index]
    resolved = resolve_backend(backend)  # `auto` -> best installed ML backend
    if resolved != backend:
        log(f"backend: auto -> {resolved} (best installed ML potential)")
    backend = resolved
    tag = f"{backend}:{model}" if model else backend

    c = copy.deepcopy(cfg)
    c.mlip.backend, c.mlip.model, c.mlip.models = backend, model, []
    prepared = _stamp_slab(c, slab_extxyz)
    c = prepared.cfg
    injected = prepared.slab_injected

    lattice: dict[str, float] = {}
    if c.slab.relax_lattice and c.slab.a is None and not injected:
        a0 = equilibrium_lattice(c.slab.element, lambda: make_calculator(c.mlip))
        a_ref = default_lattice(c.slab.element)
        log(
            f"[{tag}] relaxed lattice a={a0:.4f} A "
            f"(default {a_ref:.4f} A, strain {(a_ref / a0 - 1) * 100:+.2f}%)"
        )
        c.slab.a = a0
        lattice[tag] = a0

    # Capture geometry ONLY from the first model (``model_index == 0``),
    # exactly as ``pipeline.run()`` does (``collect=structures if si == 0``):
    # raw relaxed energies from different MLIP potentials sit on different
    # absolute references, so a cross-model "lowest energy" pick would be an
    # artifact of each potential's zero point, not relaxation quality. Within
    # one model, across seeds, min-energy is meaningful — that's what the
    # aggregate merges. ``collect`` receives ``{state: (energy, Atoms)}`` plus
    # ``poison:<p>`` keys; serialise the non-poison states to extxyz so the
    # geometry crosses the JSON job boundary.
    collect: dict[str, Any] | None = {} if model_index == 0 else None
    partial = run_one_seed(c, seed, log=log, collect=collect)
    partial["model"] = tag
    structures: dict[str, SeedStructureEntry] = {
        name: {"energy": float(energy), "extxyz": _atoms_to_extxyz(atoms)}
        for name, (energy, atoms) in (collect or {}).items()
        if not name.startswith("poison:")
    }
    if only_steps:
        log(f"[{tag}] seed={seed}: step retry — recording only {list(only_steps)}")
        partial = narrow_partial(partial, only_steps, root=root_state(config))
        structures = cast(
            "dict[str, SeedStructureEntry]", narrow_structures(structures, partial)
        )
    return {
        "seed": seed,
        "model": tag,
        "model_index": model_index,
        "partial": partial,
        "lattice": lattice,
        "structures": structures,
    }


#: Default subprocess wall-clock bound (s) when a job declares no
#: ``resources.wall_seconds`` — mirrors ``quest.compute._autocatpath_wall_seconds``'s
#: own 90-min default so a hand-minted seed still gets the same bound the
#: dispatcher would have stamped.
_DEFAULT_SEED_TIMEOUT_S = 5400


def _child_cmd(req_path: str, out_path: str) -> list[str]:
    """The argv both the blocking (:func:`run_seed_partial_subprocess`) and
    detached (:func:`submit_seed_partial_detached`) protocols spawn — the
    same ``python -m precis_pathway.runner req out`` child entrypoint
    (:func:`_subprocess_main`). Factored out so a test can stub the whole
    launch by monkeypatching this one function.

    ``-u`` is load-bearing, not tidiness. The child reports progress with
    ``print()`` (:func:`_subprocess_main` passes
    ``log=lambda *a, **k: print(*a, **k)`` into :func:`run_seed_partial`),
    and both launch protocols redirect that stdout into a *file* — a pipe
    for the blocking sibling, ``stdout.log`` for the detached one. Python
    block-buffers stdout at ~8 KB when it isn't a tty, so without ``-u``
    the progress sits in the child's buffer and is **discarded unflushed**
    when the process is signalled. That inverts the diagnostic exactly
    where it matters: a child that exits cleanly flushes at exit and its
    log is complete, while a child killed by the wall-clock deadline, a
    worker restart, or a node reboot leaves an EMPTY ``stdout.log`` — so
    the failures we most need to explain are the only ones that explain
    nothing. Observed in prod: 85 failed ``autocatpath_seed`` jobs, some
    after 2-3 hours of compute, every one of them carrying an identical
    748-char tail containing nothing but the two import-time ``torch``
    warnings (stderr is line-buffered, so only *it* survived)."""
    import sys

    return [sys.executable, "-u", "-m", "precis_pathway.runner", req_path, out_path]


_MEM_LIMIT_ENV = "PRECIS_AUTOCATPATH_MEM_LIMIT_GB"
#: 32 GB = ~26% of the 121 GB compute node, so three concurrent children
#: (the usual fan-out) still fit under physical RAM. Job 449981 grew ~10 GB/h
#: to ~117 GB and swapped the node; 0 disables the cap.
_DEFAULT_MEM_LIMIT_GB = 32.0


def _mem_limit_bytes() -> int:
    """Per-child memory cap in bytes from ``PRECIS_AUTOCATPATH_MEM_LIMIT_GB``
    (``0`` = uncapped; unparseable -> the default)."""
    raw = os.environ.get(_MEM_LIMIT_ENV, "").strip()
    try:
        gb = float(raw) if raw else _DEFAULT_MEM_LIMIT_GB
    except ValueError:
        gb = _DEFAULT_MEM_LIMIT_GB
    return max(0, int(gb * 1024**3))


def _apply_memory_cap() -> int:
    """Cap this (child) process's heap; returns the bytes applied, 0 if none.

    Runs at the top of :func:`_subprocess_main`, so every launch path (blocking
    seed/coverage, detached seed, kinetics) is covered without a ``preexec_fn``.
    Uses ``RLIMIT_DATA`` (Linux >= 4.7: private writable mappings + brk), not
    ``RLIMIT_AS``: CUDA/torch reserve hundreds of GB of *virtual* address space
    that is never resident, so an AS cap would kill every GPU seed. Over the
    cap, allocations fail and Python raises ``MemoryError``, which the child
    reports as a classified "memory limit exceeded" failure. A no-op (with a
    log line) off Linux, where the limit is not enforced."""
    limit = _mem_limit_bytes()
    if limit <= 0:
        return 0
    if not sys.platform.startswith("linux"):
        log.info(
            "%s: RLIMIT_DATA not enforced on %s; no memory cap",
            _MEM_LIMIT_ENV,
            sys.platform,
        )
        return 0
    import resource

    try:
        _soft, hard = resource.getrlimit(resource.RLIMIT_DATA)
        if hard != resource.RLIM_INFINITY:
            limit = min(limit, hard)
        resource.setrlimit(resource.RLIMIT_DATA, (limit, hard))
    except (ValueError, OSError) as exc:
        log.warning("could not set memory cap (%s); running uncapped", exc)
        return 0
    return limit


def _mem_error_text(limit: int) -> str:
    return (
        f"memory limit exceeded (cap {limit / 1024**3:.0f} GB per child; "
        f"{_MEM_LIMIT_ENV} raises it, 0 disables)"
    )


_CPUSET_RE = re.compile(r"^\d+(-\d+)?(,\d+(-\d+)?)*$")


def _parse_cpuset(spec: str) -> set[int]:
    """Expand taskset list syntax (``"0-4,10-14"``) into CPU ids. ``spec`` must
    already match :data:`_CPUSET_RE`."""
    cpus: set[int] = set()
    for part in spec.split(","):
        lo, _, hi = part.partition("-")
        cpus.update(range(int(lo), int(hi or lo) + 1))
    return cpus


def _pinned_cmd(cmd: list[str], cpuset: str | None) -> list[str]:
    """Prefix ``cmd`` with ``taskset -c <cpuset>`` to pin the host-process
    child to a CPU set (``params.resources.cpuset``, taskset list syntax such
    as ``"0-4,10-14"``). Best-effort: a malformed set, a missing ``taskset``
    binary, a platform without ``os.sched_getaffinity`` (macOS), or a set
    naming CPUs this node lacks (``taskset`` would die with "Invalid
    argument") returns ``cmd`` unpinned rather than failing the job. The
    dispatcher stamps one cpuset on every seed, so the node that runs the
    child checks its own affinity here."""
    if not cpuset:
        return cmd
    if not _CPUSET_RE.match(cpuset):
        log.warning("ignoring malformed resources.cpuset %r; running unpinned", cpuset)
        return cmd
    if not hasattr(os, "sched_getaffinity"):
        log.debug(
            "no os.sched_getaffinity; ignoring cpuset %r, running unpinned", cpuset
        )
        return cmd
    requested = _parse_cpuset(cpuset)
    missing = requested - set(os.sched_getaffinity(0))
    if missing:
        log.warning(
            "resources.cpuset %r names CPUs this node lacks (%s); running unpinned",
            cpuset,
            ",".join(str(c) for c in sorted(missing)),
        )
        return cmd
    if shutil.which("taskset") is None:
        log.warning("taskset not on PATH; ignoring cpuset %r, running unpinned", cpuset)
        return cmd
    return ["taskset", "-c", cpuset, *cmd]


def run_seed_partial_subprocess(
    config: dict[str, Any],
    seed: int,
    model_index: int,
    *,
    force_backend: str | None = None,
    slab_extxyz: str | None = None,
    timeout: int = _DEFAULT_SEED_TIMEOUT_S,
    cpuset: str | None = None,
    only_steps: Sequence[str] | None = None,
) -> SeedPartialResult:
    """Run :func:`run_seed_partial` in a FRESH child process — killable + isolated.

    Isolation is load-bearing on a GPU node (gr191351): loading MACE/CUDA in the
    long-lived precis-worker process deadlocks — the main thread spins in
    ``libcuda.so`` for hours while the ssh_node lease (2h floor) protects it from
    reclaim, wedging *every* system pass on that host (``cast_audio`` included)
    into a SIGKILL-restart loop. The identical MACE load in a fresh process
    completes in seconds. Running the compute out-of-process also makes a genuine
    hang *killable*: ``subprocess`` SIGKILLs the child at ``timeout`` and this
    raises, so the blocking ``ssh_node`` dispatch returns (as a failure) within a
    bounded time instead of holding the worker's pass for the whole lease horizon.

    ``timeout`` must stay below the ssh_node lease (``max(7200, wall+3600)``) so
    the child is killed before the lease can expire and the job be double-claimed;
    the caller (:mod:`precis_pathway.seed_job`) passes the job's declared
    ``resources.wall_seconds``, which is always < that lease by construction.

    Inputs and the returned dict are exactly :func:`run_seed_partial`'s (all
    JSON-serialisable). The JSON result is exchanged via a temp file, never
    stdout, so MACE's chatty logging can't corrupt it; child stdout/stderr are
    logged for diagnosis and, on failure, folded into the raised error.
    """
    request = {
        "config": config,
        "seed": seed,
        "model_index": model_index,
        "force_backend": force_backend,
        "slab_extxyz": slab_extxyz,
        "only_steps": list(only_steps) if only_steps else None,
    }
    result = _run_request_subprocess(
        request,
        timeout=timeout,
        cpuset=cpuset,
        job_name="autocatpath_seed",
        unit=f"seed={seed} model_index={model_index}",
        prefix="autocatpath-seed-",
    )
    return cast("SeedPartialResult", result)


def _run_request_subprocess(
    request: dict[str, Any],
    *,
    timeout: int,
    cpuset: str | None,
    job_name: str,
    unit: str,
    prefix: str,
) -> dict[str, Any]:
    """The blocking child launch both :func:`run_seed_partial_subprocess` and
    :func:`run_coverage_scan_subprocess` share: write ``request`` to a temp
    dir, run :func:`_child_cmd` under ``timeout`` (SIGKILL on expiry), read
    the ``{"ok", "result"|"error"}`` envelope back. ``job_name``/``unit``
    only shape the error text; ``prefix`` names the scratch dir."""
    import os
    import subprocess
    import tempfile

    with tempfile.TemporaryDirectory(prefix=prefix) as td:
        req_path = os.path.join(td, "request.json")
        out_path = os.path.join(td, "result.json")
        with open(req_path, "w", encoding="utf-8") as fh:
            json.dump(request, fh)

        cmd = _pinned_cmd(_child_cmd(req_path, out_path), cpuset)
        try:
            proc = subprocess.run(
                cmd,
                timeout=timeout,
                capture_output=True,
                text=True,
                encoding="utf-8",
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            # subprocess.run has already SIGKILLed the child by here — the
            # hung MACE/CUDA process is gone, the worker pass is free again.
            tail = (exc.stderr or exc.stdout or "")[-2000:]
            raise RuntimeError(
                f"{job_name} compute exceeded its {timeout}s wall budget "
                f"and was killed ({unit}); last output: {tail!r}"
            ) from exc

        if proc.stdout:
            log.debug("%s child stdout:\n%s", job_name, proc.stdout[-4000:])
        if proc.stderr:
            log.debug("%s child stderr:\n%s", job_name, proc.stderr[-4000:])

        if proc.returncode != 0 or not os.path.exists(out_path):
            tail = (proc.stderr or proc.stdout or "<no output>")[-2000:]
            if os.path.exists(out_path):
                # The child's own envelope names the cause (e.g. the
                # classified "memory limit exceeded"); prefer it over a
                # possibly-empty stderr tail.
                try:
                    with open(out_path, encoding="utf-8") as fh:
                        tail = str(json.load(fh).get("error") or tail)
                except (OSError, ValueError):
                    pass
            msg = (
                f"{job_name} subprocess failed (rc={proc.returncode}, "
                f"{unit}); last output: {tail!r}"
            )
            # INFRA-class: the child was signal-killed (SIGKILL/OOM, a
            # negative returncode) or exited without writing its result
            # envelope at all — the compute never ran. A positive nonzero
            # returncode WITH the envelope present (an unhandled-but-non-
            # fatal child exception that still exited normally) is the one
            # case that reaches this branch (via the OR's first arm) without
            # being infra-classifiable — stays a bare RuntimeError.
            if _died_by_signal(proc.returncode) or not os.path.exists(out_path):
                raise ChildKilledError(msg)
            raise RuntimeError(msg)

        with open(out_path, encoding="utf-8") as fh:
            payload = json.load(fh)

    if not payload.get("ok"):
        raise RuntimeError(f"{job_name} compute error ({unit}): {payload.get('error')}")
    return cast("dict[str, Any]", payload["result"])


# ── coverage anchors (surface-Pourbaix slice 1) ───────────────────────────


def coverage_anchor_key(config: dict[str, Any], *, slab_key: str | None = None) -> str:
    """Content address of an anchor set: everything that fixes *which slab,
    which adsorbates at which coverages, on which thermodynamic footing* —
    the config minus its ``mlip`` block (models pool under one key; the
    model tag is recorded per scan) — plus the engine version, like
    :func:`content_key`. The correction set rides in the config (an engine
    that records one does so under ``corrections``), so a correction flip
    changes the key.

    ``coverage.coverages`` is normalised to the theta values the engine
    will actually run on this slab (:func:`representable_coverages`) before
    hashing: the engine snaps each request to an integer count on the
    cell's ``n_top`` sites and reports the achieved ``n / n_top``, so two
    requests that differ only pre-snap — ``[0.25, 0.5, 0.75, 1.0]`` versus
    ``[2/9, 4/9, 7/9, 1]`` on a 9-site cell — are ONE anchor set and must
    pool under one key (gr482107).

    ``slab_key`` discriminates an *injected* slab (engine >= 0.25.0 honours
    ``cfg._prebuilt_slab``): the config's ``slab`` label no longer fixes the
    geometry — two doped candidates sharing it would otherwise pool anchors
    measured on different slabs. Pass a digest of the extxyz (the quest
    dispatch uses ``sha256`` of the exported bytes); same crystal → same
    key → twins still pool. ``None`` = label-built slab, the pre-0.25.0
    behaviour, and keys against it unchanged."""
    cfg = _prep(config, None).to_dict()
    cfg.pop("mlip", None)
    cov = cfg.get("coverage")
    if isinstance(cov, dict):
        cov["coverages"] = representable_coverages(
            cov.get("coverages"), slab_top_sites(cfg)
        )
    canonical = json.dumps(cfg, sort_keys=True, separators=(",", ":"))
    payload = f"{canonical}\x00autocatpath=={__version__}"
    if slab_key:
        payload += f"\x00slab={slab_key}"
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def run_coverage_scan(
    config: dict[str, Any],
    model_index: int = 0,
    *,
    force_backend: str | None = None,
    slab_extxyz: str | None = None,
    seed: int | None = None,
    log: Any = print,
) -> CoverageScanResult:
    """Run catpath's ab-initio-thermodynamics coverage scan
    (``autocatpath.coverage.scan``) for the MLIP spec at ``model_index`` —
    the anchor generator of ``docs/backlog/surface-pourbaix-staircase-
    optimizer.md`` slice 1.

    Without ``slab_extxyz`` the engine's scan builds its own clean slab
    from ``config.slab`` (element + facet). With it, the exported candidate
    slab is stamped through the same ``cfg._prebuilt_slab`` side channel
    the pathway pipeline uses (:func:`_stamp_slab`) — honoured by
    autocatpath >= 0.25.0, which relaxes each ``(adsorbate, coverage)`` on
    the injected geometry and records ``slab_source: injected`` plus a
    ``lattice_source`` in ``coverage.json``. Per-model selection mirrors
    :func:`run_seed_partial`: the scan itself only ever runs
    ``specs()[0]``, so the chosen spec is pinned onto the config before the
    call. JSON-safe return (:class:`CoverageScanResult`).

    ``corrections`` is what the SCAN records (``coverage.json``'s own
    ``corrections`` block), never the config's: on 0.24.0 the scan prices
    ``mu`` from the raw gas energies plus the thermo table only and applies
    neither the gas set nor the H* shift (those run in
    ``pipeline._corrections_plan`` at aggregation; catpath session,
    2026-10-07), so a 0.24.0 anchor is uncorrected whatever the config says
    and must be keyed as such — ``None`` until the engine applies and
    records them (the catpath brief's ask 2).

    ``seed`` goes to ``scan(seed=...)`` as a keyword, never into the
    config: one ``default_rng(seed)`` drives the engine's site order and
    adsorbate rattle (engine >= 0.30.0, which records it as
    ``coverage.json['seed']``), and keeping it out of the config keeps it
    out of :func:`coverage_anchor_key`, so the retries of one anchor set
    pool under one key (gr492112). ``None`` sends no keyword: the unseeded
    scan, and the only call an older engine accepts — the caller gates on
    :func:`coverage_seed_supported`.
    """
    from autocatpath.calculators import resolve_backend
    from autocatpath.coverage import scan

    cfg = _prep(config, force_backend)
    specs = cfg.mlip.specs()
    if not (0 <= model_index < len(specs)):
        raise ValueError(
            f"model_index={model_index} out of range for {len(specs)} spec(s)"
        )
    backend, model = specs[model_index]
    resolved = resolve_backend(backend)
    if resolved != backend:
        log(f"backend: auto -> {resolved} (best installed ML potential)")
    prepared = _stamp_slab(cfg, slab_extxyz)
    c = copy.deepcopy(prepared.cfg)
    c.mlip.backend, c.mlip.model, c.mlip.models = resolved, model, []
    data = scan(c, log=log) if seed is None else scan(c, log=log, seed=seed)
    corrections = data.get("corrections")
    return {
        "model": str(
            data.get("model") or (f"{resolved}:{model}" if model else resolved)
        ),
        "model_index": model_index,
        "coverage": data,
        "engine_version": __version__,
        "corrections": corrections if isinstance(corrections, dict) else None,
    }


#: Default wall-clock bound (s) for a coverage scan child: a handful of
#: adsorbates × four coverages of single relaxes on one slab — minutes on
#: MACE, well inside the seed default.
_DEFAULT_COVERAGE_TIMEOUT_S = 3600


def run_coverage_scan_subprocess(
    config: dict[str, Any],
    model_index: int = 0,
    *,
    force_backend: str | None = None,
    slab_extxyz: str | None = None,
    seed: int | None = None,
    timeout: int = _DEFAULT_COVERAGE_TIMEOUT_S,
    cpuset: str | None = None,
) -> CoverageScanResult:
    """:func:`run_coverage_scan` in a fresh, killable child — the same
    ``python -m precis_pathway.runner`` entrypoint and isolation argument as
    :func:`run_seed_partial_subprocess` (gr191351: never load MACE/CUDA in
    the long-lived worker). ``slab_extxyz`` and ``seed`` ride the request
    envelope the same way the seed child's do."""
    request = {
        "mode": "coverage",
        "config": config,
        "model_index": model_index,
        "force_backend": force_backend,
        "slab_extxyz": slab_extxyz,
        "seed": seed,
    }
    result = _run_request_subprocess(
        request,
        timeout=timeout,
        cpuset=cpuset,
        job_name="surface_coverage_scan",
        unit=f"model_index={model_index}",
        prefix="autocatpath-coverage-",
    )
    return cast("CoverageScanResult", result)


# How long a terminal-branch reap will spin WNOHANG for a proven-done child to
# become reapable before giving up (see _reap_zombie). Short: it only has to
# cover normal exit/teardown, never a pathological libcuda hang.
_TERMINAL_REAP_WAIT_S = 1.0


#: Raw ``waitpid`` status of children THIS process reaped, by pid — lets the
#: no-envelope failure branch say how the child died (the detached ``Popen``
#: is discarded at submit, so nothing else keeps the returncode). Absent for
#: a child reaped by a prior worker generation or by init.
_REAPED_STATUS: dict[int, int] = {}
#: Insertion-ordered cap on :data:`_REAPED_STATUS` (oldest evicted); branches
#: that don't need the status also pop it.
_REAPED_STATUS_CAP = 256


def _exit_info(pid: int) -> dict[str, Any]:
    """``exit_code`` (negative = signal) and ``exit_signal`` (name, e.g.
    ``SIGKILL``) for a child this process reaped; ``{}`` when the status is
    unknown. Pops the entry."""
    status = _REAPED_STATUS.pop(pid, None)
    if status is None:
        return {}
    if os.WIFSIGNALED(status):
        signum = os.WTERMSIG(status)
        try:
            name = signal.Signals(signum).name
        except ValueError:
            name = f"SIG{signum}"
        return {"exit_code": -signum, "exit_signal": name}
    if os.WIFEXITED(status):
        return {"exit_code": os.WEXITSTATUS(status)}
    return {}


_WARNING_LINE_RE = re.compile(r"\b(Deprecation|User|Future)Warning\b")


def _stderr_tail(scratch_dir: str, max_lines: int = 40) -> str:
    """Last ``max_lines`` of ``stderr.log`` with warning noise removed.

    Drops lines naming a Deprecation/User/FutureWarning plus the indented
    source line python prints under each (``warnings.warn(`` and friends),
    so a crash or kill is not buried: the 2026-10 seed failures' raw 2000-char
    tail was warnings only. ``""`` when stderr is absent or all noise."""
    try:
        with open(
            os.path.join(scratch_dir, "stderr.log"), encoding="utf-8", errors="replace"
        ) as fh:
            lines = fh.read().splitlines()
    except OSError:
        return ""
    kept: list[str] = []
    skip_continuation = False
    for line in lines:
        if _WARNING_LINE_RE.search(line):
            skip_continuation = True
            continue
        if skip_continuation and line.startswith((" ", "\t")):
            continue
        skip_continuation = False
        if line.strip():
            kept.append(line)
    return "\n".join(kept[-max_lines:])


def _reap_zombie(pid: int, *, wait_s: float = 0.0) -> bool:
    """``os.waitpid(pid, WNOHANG)`` reap, optionally spun up to ``wait_s``.

    :func:`submit_seed_partial_detached` discards its ``Popen`` right after
    spawning — nothing else ever calls ``.wait()``/``.poll()`` on that PID.
    Without an explicit reap, the child sits ``<defunct>`` in the
    submitting process's table forever — the common succeeding-job case,
    not a rare failure — so this runs on EVERY terminal branch of
    :func:`poll_seed_partial_detached`, not just the crash one.

    ``wait_s`` bounds a short retry spin, never an unbounded block:

    * **liveness** probe (:func:`_process_alive`) uses default ``0.0``: a
      single WNOHANG probe that must never stall on a still-running
      child; not-yet-a-zombie reads as "not reaped", falls through to
      ``kill(pid, 0)``.
    * **terminal** branch of :func:`poll_seed_partial_detached` passes a
      small ``wait_s``: a parsed ``result.json`` envelope (written via
      ``os.replace`` as the child's LAST act, :func:`_subprocess_main`)
      proves it's exiting, but is visible before the child is actually a
      reapable zombie — a plain WNOHANG would lose that race and leak the
      ``<defunct>``. Spinning WNOHANG for bounded ``wait_s`` reaps it
      deterministically.

    ``wait_s`` is a SHORT cap, not full ``os.waitpid(pid, 0)``: a
    MACE/CUDA child can pathologically hang in driver/interpreter
    teardown AFTER writing its envelope (gr191351's "spins in ``libcuda``
    for hours" class, cf. :func:`run_seed_partial_subprocess`), and this
    runs inside the single-threaded ``run_ssh_node_pass`` poll loop whose
    own deadline check fires BEFORE ``poll()`` — an unbounded wait here
    would freeze that worker generation un-killably. On timeout: leave
    the zombie (reaped when the worker recycles) — one leaked
    ``<defunct>`` beats a frozen loop.

    Returns ``True`` if THIS call reaped the child, ``False`` if still
    running when the spin gave up, or ``ChildProcessError``/ECHILD (not
    our child — a restarted worker generation, or already reaped; either
    way an orphaned zombie reparents to init/a subreaper and gets reaped
    there)."""
    import os
    import time

    deadline = time.monotonic() + wait_s
    while True:
        try:
            reaped_pid, _status = os.waitpid(pid, os.WNOHANG)
        except ChildProcessError:
            return False
        if reaped_pid == pid:
            _REAPED_STATUS.pop(pid, None)  # pid reuse: never report a stale one
            _REAPED_STATUS[pid] = _status
            while len(_REAPED_STATUS) > _REAPED_STATUS_CAP:
                del _REAPED_STATUS[next(iter(_REAPED_STATUS))]
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.01)


def _process_alive(pid: int) -> bool:
    """Liveness probe that reaps a same-process zombie (:func:`_reap_zombie`)
    before falling back to the cross-process ``kill(pid, 0)`` signal-0
    probe — see :func:`_reap_zombie` for why the reap matters: a bare
    ``os.kill(pid, 0)`` on an un-reaped zombie still succeeds (the kernel
    keeps the process-table entry until it's reaped), so without the reap
    attempt first this would misreport "running" forever for a same-process
    child that already exited."""
    import os

    if _reap_zombie(pid):
        return False
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        # Signal permitted-but-blocked (pid owned by another user's
        # process) reads as "alive" — same reused-pid caveat the callers'
        # docstrings note; the wall-clock deadline bounds it regardless.
        return True


def submit_seed_partial_detached(
    config: dict[str, Any],
    seed: int,
    model_index: int,
    *,
    force_backend: str | None = None,
    slab_extxyz: str | None = None,
    work_dir: str | None = None,
    cpuset: str | None = None,
    only_steps: Sequence[str] | None = None,
) -> DetachedHandle:
    """Launch :func:`run_seed_partial` in a DETACHED child — the ssh_node
    ``submit`` half of the detached submit/poll protocol (gr187627). Where
    :func:`run_seed_partial_subprocess` blocks the caller for the run's
    whole duration, this returns almost immediately: it writes the request
    file, spawns the same child entrypoint (:func:`_child_cmd`) via
    ``Popen`` in its own session (``start_new_session=True`` — the child
    becomes its own process-group leader, so
    :func:`kill_seed_partial_detached` can reach everything it spawns, not
    just its immediate PID — the same isolation
    :func:`run_seed_partial_subprocess`'s docstring explains for gr191351),
    and returns a handle WITHOUT waiting on it.

    The scratch dir is a PERSISTENT ``tempfile.mkdtemp`` (not the blocking
    sibling's context-managed ``TemporaryDirectory`` — it must outlive this
    call so a LATER pass's :func:`poll_seed_partial_detached` can still find
    ``result.json``, potentially after a worker restart; the compute
    survives independent of the worker, per ``ssh_node``'s re-adopt note).
    Cleanup happens inside :func:`poll_seed_partial_detached` once the job
    reaches a terminal state, or inside :func:`kill_seed_partial_detached`
    on a wall-clock kill — never here.

    Returns the JSON-serialisable handle
    ``{"pid", "pgid", "dir", "started_at"}`` persisted onto
    ``meta.compute_handle`` by the executor.
    """
    import json as _json
    import os
    import subprocess
    import tempfile
    import time

    request = {
        "config": config,
        "seed": seed,
        "model_index": model_index,
        "force_backend": force_backend,
        "slab_extxyz": slab_extxyz,
        "only_steps": list(only_steps) if only_steps else None,
    }
    scratch = work_dir or tempfile.mkdtemp(prefix="autocatpath-seed-")
    req_path = os.path.join(scratch, "request.json")
    out_path = os.path.join(scratch, "result.json")
    with open(req_path, "w", encoding="utf-8") as fh:
        _json.dump(request, fh)

    cmd = _pinned_cmd(_child_cmd(req_path, out_path), cpuset)
    with (
        open(os.path.join(scratch, "stdout.log"), "wb") as out_fh,
        open(os.path.join(scratch, "stderr.log"), "wb") as err_fh,
    ):
        proc = subprocess.Popen(
            cmd,
            stdout=out_fh,
            stderr=err_fh,
            start_new_session=True,
        )
    # start_new_session=True makes the child its own session/process-group
    # leader, so its pgid is its pid — no separate os.getpgid round-trip.
    return {
        "pid": proc.pid,
        "pgid": proc.pid,
        "dir": scratch,
        "started_at": time.time(),
    }


def poll_seed_partial_detached(
    handle: DetachedHandle, *, defer_cleanup: bool = False
) -> PollResult:
    """Poll one detached submit — the ssh_node ``poll`` half.

    Checks ``result.json`` FIRST: a child that wrote its envelope and exited
    in the same instant must resolve to done/failed, not a stale "running"
    read of a pid that's already gone. ``_subprocess_main`` writes it
    atomically (``result.json.tmp`` + ``os.replace``), so an EXISTING
    ``result.json`` should always be a complete, parseable envelope — an
    unreadable file is therefore treated as a race/defense-in-depth case,
    not the normal failure path: fall back to PID liveness
    (:func:`_process_alive`) instead of terminalizing outright, so a poll
    that somehow still landed mid-write (or hit any other transient read
    glitch) doesn't misclassify a genuinely still-running job as failed.
    Only when the file is absent AND the process is dead does "no envelope"
    itself mean an infra failure (the child crashed before it could write
    one). A reused pid racing back to life in either gap would misread as
    "running" — accepted per the design brief: ``ssh_node``'s wall-clock
    deadline (:func:`kill_seed_partial_detached`) bounds it regardless.

    Every terminal branch reaps the child (:func:`_reap_zombie`) before
    returning — not just the crash path: the succeeding-job path is the
    COMMON one, so skipping the reap there would leave a ``<defunct>``
    zombie per job the fan-out runs, exhausting the worker's process table
    over enough jobs.

    Returns one of ``{"state": "running"}``,
    ``{"state": "done", "result": <run_seed_partial output>, "tail": ...}``,
    or ``{"state": "failed", "error": ..., "tail": ..., "infra": <bool, only
    on the no-envelope branch>}``. FAILED states remove the scratch dir
    (best-effort) after extracting what they need — unless
    ``defer_cleanup=True``, where the caller removes it via
    :func:`finalize_seed_partial_detached` once the job's completion stamp
    has landed (gr496403); the DONE state leaves it
    in place until the caller has persisted the result and calls
    :func:`finalize_seed_partial_detached` — a persist failure must be
    retryable, not a permanent loss of a finished run.
    """
    import json as _json
    import os

    scratch = handle["dir"]
    pid = handle["pid"]
    out_path = os.path.join(scratch, "result.json")

    if os.path.exists(out_path):
        try:
            with open(out_path, encoding="utf-8") as fh:
                payload = _json.load(fh)
        except (OSError, ValueError) as exc:
            # Defense-in-depth (see docstring): still alive -> keep polling,
            # no reap/cleanup (nothing has exited yet); dead with an
            # unreadable file -> a genuine infra failure.
            if _process_alive(pid):
                return {"state": "running"}
            # Annotated (not just the terminal no-envelope branch below):
            # every other ``result =`` rebinding in this function is a bare
            # reassignment of the SAME name, and mypy only allows one
            # explicit annotation per name per function — so this one decl
            # widens the inferred type for all of them, letting each
            # branch's own subset of PollResult's (``total=False``) optional
            # keys coexist without a per-branch cast.
            result: PollResult = {
                "state": "failed",
                "error": f"result.json unreadable: {exc}",
                "tail": _tail_logs(scratch),
            }
            _REAPED_STATUS.pop(pid, None)
            if not defer_cleanup:
                _cleanup_detached(scratch, failed=True)
            return result
        # Envelope parsed cleanly -> the child is done one way or another;
        # reap now, on the SUCCEEDING branch too, or it never happens. A bare
        # WNOHANG would usually observe the envelope (the child's last act)
        # pre-zombie, no-op, and leak the <defunct>; a short bounded spin
        # reaps it deterministically without risking an unbounded block on a
        # slow-teardown child in the poll loop (see _reap_zombie).
        _reap_zombie(pid, wait_s=_TERMINAL_REAP_WAIT_S)
        _REAPED_STATUS.pop(pid, None)  # envelope branches don't use the status
        if payload.get("ok"):
            # Grab the tail BEFORE cleanup wipes the scratch dir — the
            # success path otherwise discards the child's stdout/stderr
            # entirely. The 4000-char budget (the run_log chunk budget
            # seed_job caps at) is now the shared default: the failure
            # branches used to take a terser one, which had it backwards —
            # a successful run's log is the one nobody reads.
            # NO cleanup here: the caller hasn't persisted the result yet.
            # Deleting the scratch dir before the DB write once turned a
            # failed persist into a permanent loss — the next poll found a
            # dead pid with no envelope and misclassified 48 finished seeds
            # as ``infra:child-killed``. The envelope stays on disk until
            # the caller confirms persistence via
            # :func:`finalize_seed_partial_detached`; re-polling this branch
            # is idempotent (:func:`_reap_zombie` tolerates ECHILD).
            return {
                "state": "done",
                "result": payload["result"],
                "tail": _tail_logs(scratch),
            }
        result = {
            "state": "failed",
            "error": str(payload.get("error")),
            "tail": _tail_logs(scratch),
        }
        if not defer_cleanup:
            _cleanup_detached(scratch, failed=True)
        return result

    if _process_alive(pid):
        return {"state": "running"}

    # _process_alive already reaped it (if it was ours to reap) as part of
    # determining "dead" — no separate _reap_zombie call needed here.
    # INFRA-class (parked-leaf-recovery, docs/backlog/
    # parked-leaf-recovery.md): the child exited without writing its result
    # envelope at all — the ``ChildKilledError`` condition, just observed
    # from the detached (no captured returncode) side rather than raised —
    # ``"infra": True`` tells ``seed_job._poll`` to pass
    # ``open_tag="infra:child-killed"`` through to ``ctx.record_failure``.
    result = {
        "state": "failed",
        "error": "child process exited without writing result.json",
        "tail": _tail_logs(scratch),
        "stderr_tail": _stderr_tail(scratch),
        "infra": True,
    }
    info = _exit_info(pid)
    if "exit_code" in info:
        result["exit_code"] = info["exit_code"]
    if "exit_signal" in info:
        result["exit_signal"] = info["exit_signal"]
    if not defer_cleanup:
        _cleanup_detached(scratch, failed=True)
    return result


def aggregate_seed_partials(
    config: dict[str, Any],
    # Covariant (Sequence + Mapping, not list[dict]) so a caller can pass
    # either a fresh list[SeedPartialResult] (in-process fan-out, e.g. tests)
    # or the loosely-reconstructed list[dict[str, Any]] the job glue rebuilds
    # from persisted job meta (aggregate_job._collect_seed_results) — both
    # are read-only here (only ``r["partial"]``/``r.get(...)`` access).
    seed_results: Sequence[Mapping[str, Any]],
    *,
    force_backend: str | None = None,
    slab_extxyz: str | None = None,
) -> PathwayArtifact:
    """Combine N :func:`run_seed_partial` outputs into the same
    self-contained artifact shape :func:`run_pathway` returns, including the
    per-state relaxed geometries the fan-out now carries (slice-1b): the
    lowest-energy geometry seen for each state across the first-model seeds
    (only ``model_index == 0`` units collect geometry — see
    :func:`run_seed_partial` — so these energies are on one potential's scale
    and min-energy-wins is meaningful, mirroring the monolith).

    Pure numpy (``autocatpath.pipeline.aggregate_partials`` — no ML deps),
    so this runs in-process on any node, not just wherever the seeds ran.
    """
    from autocatpath.pipeline import aggregate_partials

    cfg = _prep(config, force_backend)
    effective = cfg.to_dict()
    prepared = _stamp_slab(cfg, slab_extxyz)

    partials = [r["partial"] for r in seed_results]
    results = aggregate_partials(prepared.cfg, partials)
    _stamp_endpoint_tol(results)
    lattice: dict[str, float] = {}
    for r in seed_results:
        lattice.update(r.get("lattice") or {})
    results.lattice = lattice

    # Min-energy geometry per state across the first-model seeds (only those
    # units carry ``structures``). A partial from before this field existed
    # simply contributes nothing (``.get`` → {}), so a mixed-vintage aggregate
    # degrades to "whatever was collected".
    best: dict[str, tuple[float, str]] = {}
    for r in seed_results:
        for name, geo in (r.get("structures") or {}).items():
            energy = geo["energy"]
            if name not in best or energy < best[name][0]:
                best[name] = (energy, geo["extxyz"])
    structures_extxyz = {name: xyz for name, (_e, xyz) in best.items()}

    results_json, graph_json = _analysis_payloads(prepared.cfg, results)
    return {
        "content_key": content_key(effective),
        "autocatpath_version": __version__,
        "config": effective,
        "config_snapshot_yaml": _snapshot_yaml(prepared.cfg),
        "results_json": results_json,
        "graph_json": graph_json,
        "methods_md": provenance.methods_text(prepared.cfg, results),
        "structures_extxyz": structures_extxyz,
        "warnings": list(results.warnings),
    }


def aggregate_seed_partials_subprocess(
    config: dict[str, Any],
    seed_results: Sequence[Mapping[str, Any]],
    *,
    force_backend: str | None = None,
    timeout: int = _DEFAULT_SEED_TIMEOUT_S,
) -> PathwayArtifact:
    """:func:`aggregate_seed_partials` in a fresh child, so the per-child
    memory cap (:func:`_apply_memory_cap`) and a wall-clock bound cover the
    combine too (job 449981 grew a worker to ~117 GB and nobody proved it was
    kinetics). Same inputs and returned artifact as the in-process function;
    a cap hit raises ``RuntimeError("... memory limit exceeded ...")``."""
    request = {
        "mode": "aggregate",
        "config": config,
        "seed_results": list(seed_results),
        "force_backend": force_backend,
    }
    result = _run_request_subprocess(
        request,
        timeout=timeout,
        cpuset=None,
        job_name="autocatpath_aggregate",
        unit=f"{len(seed_results)} seed partial(s)",
        prefix="autocatpath-aggregate-",
    )
    return cast("PathwayArtifact", result)


def coverage_mixtures_supported() -> bool:
    """Whether the installed engine's scan takes ``coverage.mixtures`` —
    co-adsorbed A/B configurations (gr489475). Feature-detected on the
    config dataclass rather than a version floor: an older engine builds
    ``CoverageConfig(**coverage)`` and raises ``TypeError`` on the key,
    so the request side must not send it there."""
    import dataclasses

    from autocatpath.config import CoverageConfig

    return "mixtures" in {f.name for f in dataclasses.fields(CoverageConfig)}


def spectators_supported() -> bool:
    """Whether the installed engine's ``Config`` takes ``spectators`` — the
    background adsorbate layer (autocatpath 0.31.0, catpath PR #8; gr496903).
    Feature-detected on the dataclass fields like
    :func:`coverage_mixtures_supported`: an older engine's
    ``Config.from_dict`` refuses the unknown key."""
    import dataclasses

    return "spectators" in {f.name for f in dataclasses.fields(Config)}


def drop_unsupported_spectators(config: dict[str, Any]) -> dict[str, Any]:
    """``config`` as the installed engine can take it: unchanged when it
    supports ``spectators`` (or none is set), else a copy without the key
    and a logged warning so the dilute run is never mistaken for a dense
    one. The dispatchers call this before the config reaches the engine."""
    if "spectators" not in config or spectators_supported():
        return config
    log.warning(
        "autocatpath engine %s has no Config.spectators; running %r without "
        "the spectator layer",
        __version__,
        config.get("name", "?"),
    )
    return {k: v for k, v in config.items() if k != "spectators"}


def coverage_seed_supported() -> bool:
    """Whether the installed engine's scan takes ``seed`` (gr492112) —
    feature-detected on ``scan``'s signature, the keyword
    :func:`run_coverage_scan` passes; an older scan raises ``TypeError``
    on it, so the request side must not send it there."""
    import inspect

    from autocatpath.coverage import scan

    return "seed" in inspect.signature(scan).parameters


def _import_kinetics() -> Any:
    """The ``autocatpath.kinetics`` module, or ``None`` when the deployed
    engine predates it (the module shipped in engine 0.15.0; the fleet's
    pinned floor, 0.13.0, predates it — a plain ``ImportError``, not a bug).
    A thin, mockable seam so :func:`run_kinetics` never needs a >=0.15
    engine installed to exercise its ImportError / exception handling in
    tests — a caller can monkeypatch this function directly instead of
    faking ``sys.modules``.
    """
    try:
        from autocatpath import kinetics
    except ImportError:
        return None
    return kinetics


def _est_mean(x: Any) -> float:
    """``Estimate`` | ``{"mean": ...}`` | float -> float; a null mean reads as
    NaN so finiteness guards treat both alike (mirrors
    ``autocatpath.kinetics._mean``)."""
    v = x.get("mean") if isinstance(x, dict) else getattr(x, "mean", x)
    return float("nan") if v is None else float(v)


#: Fraction of the kinetics wall-clock budget the calibration may project the
#: solve into; the rest stays headroom for the engine's own bookkeeping and a
#: contended worker.  (witty 2026-10-21, gr502251)
_MC_BUDGET_FRAC = 0.6
#: Below this many Monte-Carlo draws the sensitivity band is not worth
#: reporting — refuse the solve instead of emitting noise on a hopeless
#: budget.
_MC_SAMPLE_FLOOR = 100


def _steady_state_seconds(
    steady: Any, transitions: Any, root: str, t_end: float
) -> float:
    """Wall time of one ``steady_state`` solve — the probe the MC budget
    extrapolates from.  Cheap (ms), so run a few for a stable mean; the small
    iteration cap bounds the probe itself on a pathological input."""
    import inspect
    import time

    params = set(inspect.signature(steady).parameters)
    kw = {"t_end": t_end} if "t_end" in params else {}
    t0 = time.monotonic()
    n = 0
    while n < 5:
        steady(transitions, root, **kw)
        n += 1
        if time.monotonic() - t0 >= 0.05:
            break
    return (time.monotonic() - t0) / n


def _mc_calibrated_samples(
    kinetics: Any,
    cfg: Config,
    net: Any,
    ledgers: dict[str, Any],
    refs: dict[str, Any],
    nodes: dict[str, Any],
    edges: list[dict[str, Any]],
    *,
    budget_s: int,
) -> tuple[int | None, str | None]:
    """The Monte-Carlo draw count that fits the kinetics wall-clock budget on
    THIS worker — ``(n_samples, None)`` to run with, or ``(None, reason)`` to
    refuse before solving.

    WHY THIS EXISTS (gr502251, witty 2026-10-21): ~3% of aggregate jobs burn
    the full 900 s kinetics ceiling.  The cause is not ill-posedness — every
    recent job has >1 absorbing component (stored nullity 4–26, including
    trusted solves) — it is sheer size: the 17 timed-out runs are the largest
    networks (66–76 transitions vs a 54 median), and the cost is dominated by
    ``monte_carlo``'s ``n_samples × ≤(2·n_transitions + 1)`` steady-state
    resolves.  So the solve self-calibrates: build the real transition list
    (the engine's own ``build_transitions``), time one ``steady_state`` solve
    on it, extrapolate, and shrink ``n_samples`` to fit — a reduced band beats
    a killed child that records nothing.  Below the 100-draw floor the band
    isn't worth reporting and the solve is refused instead.

    ``budget_s=0`` (the unbounded in-process path) or an engine missing
    ``build_transitions``/``steady_state`` skips calibration entirely and
    keeps the configured count.
    """
    import inspect

    default = cfg.kinetics.n_samples
    build = getattr(kinetics, "build_transitions", None)
    steady = getattr(kinetics, "steady_state", None)
    area_of = getattr(kinetics, "site_area_A2", None)
    if default <= 0 or budget_s <= 0 or not (build and steady and area_of):
        return default, None
    order = net.order()
    root = order[0] if order else "*"
    g = {name: _est_mean(est) for name, est in nodes.items()}
    want = {
        "sticking": getattr(cfg.kinetics, "sticking", None),
        "target": cfg.kinetics.product or cfg.target,
        "root": root,
        "dg_tol": getattr(cfg.search, "energy_thresh", 0.05),
        "environment": cfg.env() if getattr(cfg, "environment", None) else None,
    }
    have = set(inspect.signature(build).parameters)
    transitions, _skipped = build(
        g,
        edges,
        list(net.links),
        ledgers,
        refs,
        cfg.conditions.temperature,
        dict(cfg.conditions.pressures),
        area_of(net.slab()),
        **{k: v for k, v in want.items() if k in have},
    )
    if not transitions:
        return default, None  # solve raises its own "no usable transitions"
    t_s = _steady_state_seconds(steady, transitions, root, cfg.kinetics.t_end)
    n_trans = len(transitions)
    n_states = len(
        {getattr(t, "a", None) for t in transitions}
        | {getattr(t, "b", None) for t in transitions}
    )
    # one draw costs the v1 MC resolve pass (2T+1); engine >= 0.34 runs the
    # D4 correlated pass on top — a second full pass (2T+1, checks + solve
    # + DRC) plus one TOF-only resolve per extra rho in the sweep
    sweep_extra = len(
        [
            r
            for r in getattr(cfg.kinetics, "rho_sweep", ()) or ()
            if r != getattr(cfg.kinetics, "rho", 0.5)
        ]
    )
    per_draw = (2 * (2 * n_trans + 1) + sweep_extra) * t_s
    fixed = (1 + 2 * n_trans + 2 * n_states) * t_s  # steady + DRC + TRC
    avail = _MC_BUDGET_FRAC * budget_s - fixed
    cap = int(avail / per_draw) if per_draw > 0 else default
    capped = min(default, cap)
    if capped < _MC_SAMPLE_FLOOR:
        proj = fixed + _MC_SAMPLE_FLOOR * per_draw
        return None, (
            f"kinetics: projected solve needs ~{proj:.0f}s even at the "
            f"{_MC_SAMPLE_FLOOR}-sample floor — over "
            f"{_MC_BUDGET_FRAC * budget_s:.0f}s of the {budget_s}s "
            f"wall-clock budget; refused before solving "
            f"({_KINETICS_TIMEOUT_ENV} raises the ceiling)"
        )
    return capped, None


def run_kinetics(
    config: dict[str, Any],
    artifact: PathwayArtifact,
    *,
    budget_s: int | None = None,
    tpd: dict[str, Any] | None = None,
) -> None:
    """Run catpath's post-hoc microkinetics over a just-aggregated pathway,
    in-process, folding the result onto ``artifact["results_json"]`` (mutated
    in place — no return value).

    Mirrors autocatpath's own CLI ``kinetics`` post-processing branch
    (``cli.py``, ``args.cmd == "kinetics"``: rebuild the net + ledger refs +
    thermo-corrected refs from the config, then ``kinetics.solve``), but
    reads ``nodes``/``edges``/``score.activity.span_eV`` straight off the
    in-memory ``results_json`` instead of a ``results.json`` on disk — this
    flow (the aggregate job) never writes one. ``mari`` is always ``None``:
    no coverage scan runs here.

    ``config`` must be the EFFECTIVE (post-backend-override) config —
    typically ``artifact["config"]`` fresh off :func:`aggregate_seed_partials`
    — not the raw dispatch params, whose ``force_backend`` override may
    already be baked into ``artifact["config"]`` and would otherwise silently
    diverge from what actually ran.

    Kinetics is a diagnostic bonus riding on a successful aggregate, never
    load-bearing for it: this function never raises. A deployed engine
    predating the ``kinetics`` module, or any exception the solve raises,
    lands as ``results_json["kinetics_error"]`` instead — the aggregate
    itself still succeeds and persists either way.

    ``tpd`` (kinetics v2 slice 9, engine >= 0.39) is the ``kinetics_tpd``
    payload of :func:`run_kinetics_tpd_subprocess`: its applied offsets put
    the solve on the ``tpd_corrected`` states (provider ``vacuum+tpd``) and
    its comparisons land on the v2 record.
    """
    results_json = artifact["results_json"]
    kinetics = _import_kinetics()
    if kinetics is None:
        results_json["kinetics_error"] = f"engine {__version__} lacks kinetics"
        return
    try:
        from autocatpath.ledger import compute_ledgers
        from autocatpath.pipeline import _build_net, ledger_refs
        from autocatpath.thermo import build_table

        cfg = _prep(config, None)
        nodes = results_json["nodes"]
        edges = results_json["edges"]
        span = ((results_json.get("score") or {}).get("activity") or {}).get("span_eV")
        net = _build_net(cfg)
        ledgers, _warns = compute_ledgers(net)
        refs = ledger_refs(cfg, net, ledgers, log=lambda *a, **k: None)
        table = build_table(cfg)
        if table and refs:
            refs = table.corrected_refs(refs)
        n_samples, refuse = _mc_calibrated_samples(
            kinetics,
            cfg,
            net,
            ledgers,
            refs,
            nodes,
            edges,
            budget_s=(_kinetics_timeout_s() if budget_s is None else max(0, budget_s)),
        )
        if refuse is not None:
            results_json["kinetics_error"] = refuse
            return
        configured = cfg.kinetics.n_samples
        cfg.kinetics.n_samples = n_samples
        extra = {"tpd": tpd} if tpd is not None else {}
        out = kinetics.solve(
            cfg,
            nodes,
            edges,
            net,
            refs,
            span=span,
            mari=None,
            log=lambda *a, **k: None,
            **extra,
        )
        # engine >= 0.33 returns (v1, kinetics_v2); older engines return the
        # v1 dict alone. The v2 record sits BESIDE the v1 block (D8).
        if isinstance(out, tuple):
            result, v2 = out
            results_json["kinetics"] = result
            if isinstance(v2, dict):
                results_json["kinetics_v2"] = v2
        else:
            result = out
            results_json["kinetics"] = result
        if n_samples < configured:
            band = (result.get("sensitivity") or {}).get("tof") or {}
            result["mc_budget"] = {
                "n_samples": n_samples,
                "configured": configured,
                "tof_ci": {"p5": band.get("p5"), "p95": band.get("p95")},
            }
            warns = result.get("warnings")
            if isinstance(warns, list):
                warns.append(
                    f"kinetics: reduced samples ({n_samples} of {configured}, budget)"
                )
    except Exception as exc:
        log.warning("autocatpath kinetics: solve failed", exc_info=True)
        results_json["kinetics_error"] = (
            _mem_error_text(_mem_limit_bytes())
            if isinstance(exc, MemoryError) and _mem_limit_bytes()
            else str(exc)
        )


#: Wall-clock ceiling for the kinetics solve, in seconds. Measured cost on a
#: real production aggregate (job 449981's own inputs, engine 0.22.0): **29
#: seconds**. 900 is thirty times that — generous enough that a slow-but-
#: healthy solve is never cut off, small enough that a wedged one cannot eat
#: a worker. Set to 0 to run in-process with no ceiling (what every caller
#: did before this existed).
_KINETICS_TIMEOUT_ENV = "PRECIS_AUTOCATPATH_KINETICS_SECONDS"
_DEFAULT_KINETICS_TIMEOUT_S = 900


def _kinetics_timeout_s() -> int:
    try:
        return max(0, int(os.environ.get(_KINETICS_TIMEOUT_ENV, "")))
    except ValueError:
        return _DEFAULT_KINETICS_TIMEOUT_S


def run_kinetics_subprocess(
    config: dict[str, Any],
    artifact: PathwayArtifact,
    *,
    timeout: int | None = None,
    tpd: dict[str, Any] | None = None,
) -> None:
    """:func:`run_kinetics` under a wall-clock ceiling, in a killable child.

    Same contract as :func:`run_kinetics` — mutates ``artifact["results_json"]``
    in place, returns nothing, and **never raises**. A timeout is just another
    ``kinetics_error``, so the aggregate this rides on still succeeds and
    persists, exactly as it does for an engine that lacks the module or a
    solve that throws.

    WHY THIS EXISTS. ``run_kinetics``'s own docstring calls kinetics "a
    diagnostic bonus riding on a successful aggregate, never load-bearing",
    but it had no ceiling of any kind and its ``try/except`` cannot interrupt
    a hang. ``autocatpath_aggregate`` job 449981 was claimed four times
    between 2026-09-25 18:00Z and 2026-09-26 07:25Z — once per deploy bounce,
    via the epoch reclaim arm — and each time wrote its "combining N seed
    partial(s)" chunk and then nothing, while its worker grew to ~117 GB. The
    same job's work, replayed against its real inputs on the same engine
    version, takes **33 seconds end to end**. Whatever made those runs
    diverge, a 29-second diagnostic must not be able to hold a worker for
    eleven hours.

    The cause is not established (see
    ``docs/backlog/autocatpath-aggregate-ran-11h-on-a-33s-job.md``), which is
    the second reason for the subprocess: a timeout lands a *legible*
    ``kinetics_error`` where today there is only silence, so the next
    occurrence identifies itself instead of needing another archaeology pass.

    Only a separate process can enforce this. A thread cannot be killed out
    of a numpy call, and the suspect path — ``kinetics._evolve``'s BDF
    ``solve_ivp`` fallback — is exactly that. Same reasoning, same machinery
    as :func:`run_seed_partial_subprocess` (gr191351).

    ``timeout=0`` (or ``PRECIS_AUTOCATPATH_KINETICS_SECONDS=0``) restores the
    old unbounded in-process behaviour.
    """
    results_json = artifact["results_json"]
    budget = _kinetics_timeout_s() if timeout is None else max(0, timeout)
    if budget == 0:
        run_kinetics(
            config, artifact, budget_s=0, **({"tpd": tpd} if tpd is not None else {})
        )
        return

    import subprocess
    import tempfile

    request = {
        "mode": "kinetics",
        "config": config,
        # The child's MC-sample calibration needs the ceiling the parent is
        # enforcing — it cannot recompute it once a caller passed an explicit
        # ``timeout``.
        "budget_s": budget,
        # Only what run_kinetics actually reads off results_json. Sending the
        # whole artifact would drag structures_extxyz through two JSON hops
        # for nothing.
        "nodes": results_json.get("nodes"),
        "edges": results_json.get("edges"),
        "score": results_json.get("score"),
        "tpd": tpd,
    }
    with tempfile.TemporaryDirectory(prefix="autocatpath-kinetics-") as td:
        req_path = os.path.join(td, "request.json")
        out_path = os.path.join(td, "result.json")
        with open(req_path, "w", encoding="utf-8") as fh:
            json.dump(request, fh)

        try:
            proc = subprocess.run(
                _child_cmd(req_path, out_path),
                timeout=budget,
                capture_output=True,
                text=True,
                encoding="utf-8",
                check=False,
            )
        except subprocess.TimeoutExpired:
            # subprocess.run has SIGKILLed the child by here, so the worker
            # pass is free again — the whole point.
            log.warning("autocatpath kinetics: timed out after %ss", budget)
            results_json["kinetics_error"] = (
                f"kinetics timed out after {budget}s and was killed "
                f"({_KINETICS_TIMEOUT_ENV} raises the ceiling; 0 disables it). "
                "The aggregate itself is unaffected."
            )
            return

        try:
            with open(out_path, encoding="utf-8") as fh:
                payload = json.load(fh)
        except (OSError, ValueError):
            # No envelope at all: the child died before it could write one
            # (OOM-killed, import failure, segfault in the engine). Its
            # stderr is the only diagnosis available, so carry the tail.
            tail = (proc.stderr or proc.stdout or "").strip()[-500:]
            results_json["kinetics_error"] = (
                f"kinetics child exited {proc.returncode} without a result"
                + (f": {tail}" if tail else "")
            )
            return

    if "kinetics" in payload:
        results_json["kinetics"] = payload["kinetics"]
        # the v2 record rides beside v1 (D8); a pre-0.33 child never
        # emits it and none is fabricated
        if isinstance(payload.get("kinetics_v2"), dict):
            results_json["kinetics_v2"] = payload["kinetics_v2"]
    else:
        results_json["kinetics_error"] = payload.get(
            "kinetics_error", "kinetics child returned no result"
        )


def run_kinetics_sweep_subprocess(
    config: dict[str, Any],
    results_json: dict[str, Any],
    *,
    timeout: int | None = None,
) -> None:
    """The (T, p_total) MF sweep under a wall-clock ceiling, killable.

    Mutates ``results_json`` in place: ``kinetics_sweep`` on success,
    ``kinetics_sweep_error`` on any failure — never raises (the
    ``autocatpath_kinetics`` job turns the error into a job note, never a
    pathway-fatal).  Same child machinery as
    :func:`run_kinetics_subprocess`.
    """
    budget = _kinetics_timeout_s() if timeout is None else max(0, timeout)
    if budget == 0:
        _run_kinetics_sweep(config, results_json)
        return

    import subprocess
    import tempfile

    request = {
        "mode": "kinetics_sweep",
        "config": config,
        "budget_s": budget,
        "nodes": results_json.get("nodes"),
        "edges": results_json.get("edges"),
        "score": results_json.get("score"),
        "thermo": results_json.get("thermo"),
        "kinetics_tpd": results_json.get("kinetics_tpd"),
    }
    with tempfile.TemporaryDirectory(prefix="autocatpath-kinsweep-") as td:
        req_path = os.path.join(td, "request.json")
        out_path = os.path.join(td, "result.json")
        with open(req_path, "w", encoding="utf-8") as fh:
            json.dump(request, fh)
        try:
            proc = subprocess.run(
                _child_cmd(req_path, out_path),
                timeout=budget,
                capture_output=True,
                text=True,
                encoding="utf-8",
                check=False,
            )
        except subprocess.TimeoutExpired:
            results_json["kinetics_sweep_error"] = (
                f"kinetics sweep timed out after {budget}s and was killed"
            )
            return
        try:
            with open(out_path, encoding="utf-8") as fh:
                payload = json.load(fh)
        except (OSError, ValueError):
            tail = (proc.stderr or proc.stdout or "").strip()[-500:]
            results_json["kinetics_sweep_error"] = (
                f"kinetics sweep child exited {proc.returncode} without a result"
                + (f": {tail}" if tail else "")
            )
            return
    if "kinetics_sweep" in payload:
        results_json["kinetics_sweep"] = payload["kinetics_sweep"]
        if isinstance(payload.get("kinetics_v2"), dict):
            results_json["kinetics_v2"] = payload["kinetics_v2"]
    else:
        results_json["kinetics_sweep_error"] = payload.get(
            "kinetics_sweep_error", "kinetics sweep child returned no result"
        )


def _run_kinetics_sweep(config: dict[str, Any], results_json: dict[str, Any]) -> None:
    """In-process half of :func:`run_kinetics_sweep_subprocess` — also the
    ``kinetics_sweep`` child's workhorse.  Builds cfg/net/ledgers/refs like
    :func:`run_kinetics`, then calls ``kinetics.sweep`` (the MF (T,
    p_total) grid + optimum + competing map).  Never raises."""
    try:
        from autocatpath import kinetics as _kin
        from autocatpath.ledger import compute_ledgers
        from autocatpath.pipeline import _build_net, ledger_refs
        from autocatpath.thermo import build_table

        cfg = _prep(config, None)
        nodes = results_json["nodes"]
        edges = results_json["edges"]
        net = _build_net(cfg)
        ledgers, _warns = compute_ledgers(net)
        refs = ledger_refs(cfg, net, ledgers, log=lambda *a, **k: None)
        table = build_table(cfg)
        if table and refs:
            refs = table.corrected_refs(refs)
        g = {
            str(k): float(v.get("mean", v) if isinstance(v, dict) else v)
            for k, v in nodes.items()
        }
        target = str(
            (results_json.get("score") or {}).get("target")
            or config.get("target")
            or "NH3"
        )
        tpd = results_json.get("kinetics_tpd")
        payload = _kin.sweep(
            cfg,
            nodes,
            edges,
            net,
            ledgers,
            refs,
            g=g,
            target=target,
            thermo_record=results_json.get("thermo"),
            log=lambda *a, **k: None,
            # slice 9: the sweep prices the tpd_corrected states too
            **({"tpd": tpd} if isinstance(tpd, dict) and tpd.get("offsets") else {}),
        )
        results_json["kinetics_sweep"] = payload
        if isinstance(payload.get("kinetics_v2"), dict):
            results_json["kinetics_v2"] = payload["kinetics_v2"]
    except Exception as exc:
        log.warning("autocatpath kinetics sweep failed", exc_info=True)
        results_json["kinetics_sweep_error"] = (
            _mem_error_text(_mem_limit_bytes())
            if isinstance(exc, MemoryError) and _mem_limit_bytes()
            else str(exc)
        )


def _kinetics_sweep_child(req: dict[str, Any], out_path: str, mem_cap: int = 0) -> int:
    """Child half of :func:`run_kinetics_sweep_subprocess`."""
    results_json: dict[str, Any] = {
        "nodes": req.get("nodes"),
        "edges": req.get("edges"),
        "score": req.get("score"),
        "thermo": req.get("thermo"),
        "kinetics_tpd": req.get("kinetics_tpd"),
    }
    try:
        _run_kinetics_sweep(req["config"], results_json)
        payload = (
            {
                k: results_json[k]
                for k in ("kinetics_sweep", "kinetics_v2")
                if k in results_json
            }
            if "kinetics_sweep" in results_json
            else {
                "kinetics_sweep_error": results_json.get(
                    "kinetics_sweep_error", "no result"
                )
            }
        )
    except Exception as exc:  # pragma: no cover - _run never raises
        payload = {"kinetics_sweep_error": f"{type(exc).__name__}: {exc}"}
    tmp_path = f"{out_path}.tmp"
    with open(tmp_path, "w", encoding="utf-8") as fh:
        json.dump(payload, fh)
    os.replace(tmp_path, out_path)
    return 0


def has_tpd() -> bool:
    """Whether the installed engine carries the slice-9 TPD check (>= 0.39)."""
    kinetics = _import_kinetics()
    return kinetics is not None and hasattr(kinetics, "tpd_check")


def run_kinetics_tpd_subprocess(
    config: dict[str, Any],
    results_json: dict[str, Any],
    *,
    timeout: int | None = None,
) -> None:
    """The D11 TPD check under a wall-clock ceiling, killable (kinetics v2
    slice 9).

    Mutates ``results_json``: ``kinetics_tpd`` (the engine's
    ``kinetics.tpd_check`` payload — per-adsorbate comparisons, applied
    offsets, the corrections stamp) on success, ``kinetics_tpd_error``
    otherwise; never raises.  Same child machinery as
    :func:`run_kinetics_sweep_subprocess`.
    """
    budget = _kinetics_timeout_s() if timeout is None else max(0, timeout)
    if budget == 0:
        _run_kinetics_tpd(config, results_json)
        return

    import subprocess
    import tempfile

    request = {
        "mode": "kinetics_tpd",
        "config": config,
        "nodes": results_json.get("nodes"),
        "edges": results_json.get("edges"),
        "score": results_json.get("score"),
        "thermo": results_json.get("thermo"),
    }
    with tempfile.TemporaryDirectory(prefix="autocatpath-kintpd-") as td:
        req_path = os.path.join(td, "request.json")
        out_path = os.path.join(td, "result.json")
        with open(req_path, "w", encoding="utf-8") as fh:
            json.dump(request, fh)
        try:
            proc = subprocess.run(
                _child_cmd(req_path, out_path),
                timeout=budget,
                capture_output=True,
                text=True,
                encoding="utf-8",
                check=False,
            )
        except subprocess.TimeoutExpired:
            results_json["kinetics_tpd_error"] = (
                f"TPD check timed out after {budget}s and was killed"
            )
            return
        try:
            with open(out_path, encoding="utf-8") as fh:
                payload = json.load(fh)
        except (OSError, ValueError):
            tail = (proc.stderr or proc.stdout or "").strip()[-500:]
            results_json["kinetics_tpd_error"] = (
                f"TPD child exited {proc.returncode} without a result"
                + (f": {tail}" if tail else "")
            )
            return
    if isinstance(payload.get("kinetics_tpd"), dict):
        results_json["kinetics_tpd"] = payload["kinetics_tpd"]
    else:
        results_json["kinetics_tpd_error"] = payload.get(
            "kinetics_tpd_error", "TPD child returned no result"
        )


def _run_kinetics_tpd(config: dict[str, Any], results_json: dict[str, Any]) -> None:
    """In-process half of :func:`run_kinetics_tpd_subprocess`; never raises."""
    try:
        from autocatpath import kinetics as _kin

        if not hasattr(_kin, "tpd_check"):
            results_json["kinetics_tpd_error"] = (
                f"engine {__version__} lacks the TPD check (< 0.39)"
            )
            return
        from autocatpath.ledger import compute_ledgers
        from autocatpath.pipeline import _build_net, ledger_refs
        from autocatpath.thermo import build_table

        cfg = _prep(config, None)
        nodes = results_json["nodes"]
        net = _build_net(cfg)
        ledgers, _warns = compute_ledgers(net)
        refs = ledger_refs(cfg, net, ledgers, log=lambda *a, **k: None)
        table = build_table(cfg)
        if table and refs:
            refs = table.corrected_refs(refs)
        g = {
            str(k): float(v.get("mean", v) if isinstance(v, dict) else v)
            for k, v in nodes.items()
        }
        target = str(
            (results_json.get("score") or {}).get("target")
            or config.get("target")
            or "NH3"
        )
        results_json["kinetics_tpd"] = _kin.tpd_check(
            cfg,
            nodes,
            results_json["edges"],
            net,
            ledgers,
            refs,
            g=g,
            target=target,
            thermo_record=results_json.get("thermo"),
            log=lambda *a, **k: None,
        )
    except Exception as exc:
        log.warning("autocatpath TPD check failed", exc_info=True)
        results_json["kinetics_tpd_error"] = (
            _mem_error_text(_mem_limit_bytes())
            if isinstance(exc, MemoryError) and _mem_limit_bytes()
            else str(exc)
        )


def _kinetics_tpd_child(req: dict[str, Any], out_path: str, mem_cap: int = 0) -> int:
    """Child half of :func:`run_kinetics_tpd_subprocess`."""
    results_json: dict[str, Any] = {
        k: req.get(k) for k in ("nodes", "edges", "score", "thermo")
    }
    _run_kinetics_tpd(req["config"], results_json)
    payload = (
        {"kinetics_tpd": results_json["kinetics_tpd"]}
        if "kinetics_tpd" in results_json
        else {"kinetics_tpd_error": results_json.get("kinetics_tpd_error", "no result")}
    )
    tmp_path = f"{out_path}.tmp"
    with open(tmp_path, "w", encoding="utf-8") as fh:
        json.dump(payload, fh)
    os.replace(tmp_path, out_path)
    return 0


def fold_tpd(results_json: dict[str, Any]) -> None:
    """Fold an UNCORRECTED TPD check (no applied offset) onto the stored v2
    record: its comparisons become ``kinetics_v2.tpd``; every other field
    stays the aggregate's (the provider did not change).  A check that
    applied offsets is not folded — the kinetics solve re-runs on the
    corrected states instead (:func:`run_kinetics_subprocess` with
    ``tpd=``)."""
    tpd = results_json.get("kinetics_tpd")
    v2 = results_json.get("kinetics_v2")
    if isinstance(tpd, dict) and not tpd.get("offsets") and isinstance(v2, dict):
        v2["tpd"] = tpd.get("tpd") or None


def _kinetics_child(req: dict[str, Any], out_path: str, mem_cap: int = 0) -> int:
    """Child half of :func:`run_kinetics_subprocess`.

    Rebuilds the minimal artifact shape :func:`run_kinetics` reads, runs it,
    and writes back whichever of ``kinetics``/``kinetics_error`` it produced.
    Written atomically for the same reason the seed child's envelope is
    (``os.replace``; see :func:`_subprocess_main`).
    """
    artifact: dict[str, Any] = {
        "results_json": {
            "nodes": req.get("nodes"),
            "edges": req.get("edges"),
            "score": req.get("score"),
        }
    }
    try:
        run_kinetics(
            req["config"],
            cast("PathwayArtifact", artifact),
            budget_s=req.get("budget_s"),
            **({"tpd": req["tpd"]} if req.get("tpd") is not None else {}),
        )
        rj = artifact["results_json"]
        payload = (
            {k: rj[k] for k in ("kinetics", "kinetics_v2") if k in rj}
            if "kinetics" in rj
            else {"kinetics_error": rj.get("kinetics_error", "no result")}
        )
    except Exception as exc:  # pragma: no cover - run_kinetics never raises
        payload = {
            "kinetics_error": (
                _mem_error_text(mem_cap)
                if isinstance(exc, MemoryError) and mem_cap
                else f"{type(exc).__name__}: {exc}"
            )
        }

    tmp_path = f"{out_path}.tmp"
    with open(tmp_path, "w", encoding="utf-8") as fh:
        json.dump(payload, fh)
    os.replace(tmp_path, out_path)
    return 0


def _snapshot_yaml(cfg: Config) -> str:
    import yaml

    return yaml.safe_dump(cfg.to_dict(), sort_keys=False)


def _subprocess_main(argv: list[str]) -> int:
    """Child entrypoint for :func:`run_seed_partial_subprocess`.

    Reads a ``run_seed_partial`` request (JSON) from ``argv[1]`` and writes
    ``{"ok": True, "result": <run_seed_partial output>}`` — or ``{"ok": False,
    "error": ...}`` on any exception — to ``argv[2]``. The result goes to a file,
    not stdout, so MACE's stdout logging can't corrupt the JSON envelope the
    parent parses. Never raises: a failure is reported in the envelope so the
    parent can surface a clean job failure rather than an opaque non-zero exit.

    The write is ATOMIC (a same-dir ``.tmp`` file + ``os.replace``), not a
    direct ``open(out_path, "w")`` — load-bearing for
    :func:`poll_seed_partial_detached`, which polls ``out_path`` from a
    separate process with no synchronization: a direct write leaves a
    window where the file exists but is only partially flushed, and a poll
    landing in that window would JSONDecodeError a job that's actually
    SUCCEEDING. ``os.replace`` is atomic on POSIX (same filesystem — both
    paths share this scratch dir), so a poll only ever observes the file
    fully absent or fully written, never in between.
    """
    import os

    req_path, out_path = argv[1], argv[2]
    mem_cap = _apply_memory_cap()
    with open(req_path, encoding="utf-8") as fh:
        req = json.load(fh)
    # `mode` defaults to "seed" so a request file written by an older build
    # — the detached protocol leaves them on disk to be polled later — still
    # parses after this key was introduced.
    if req.get("mode") == "kinetics":
        return _kinetics_child(req, out_path, mem_cap)
    if req.get("mode") == "kinetics_sweep":
        return _kinetics_sweep_child(req, out_path, mem_cap)
    if req.get("mode") == "kinetics_tpd":
        return _kinetics_tpd_child(req, out_path, mem_cap)
    try:
        result: dict[str, Any]
        if req.get("mode") == "aggregate":
            result = dict(
                aggregate_seed_partials(
                    req["config"],
                    req["seed_results"],
                    force_backend=req.get("force_backend"),
                )
            )
        elif req.get("mode") == "coverage":
            result = dict(
                run_coverage_scan(
                    req["config"],
                    int(req.get("model_index", 0)),
                    force_backend=req.get("force_backend"),
                    slab_extxyz=req.get("slab_extxyz"),
                    seed=req.get("seed"),
                    log=lambda *a, **k: print(*a, **k),
                )
            )
        else:
            result = dict(
                run_seed_partial(
                    req["config"],
                    int(req["seed"]),
                    int(req["model_index"]),
                    force_backend=req.get("force_backend"),
                    slab_extxyz=req.get("slab_extxyz"),
                    # Absent on a request file an older build wrote (the
                    # detached protocol leaves them on disk) -> whole network.
                    only_steps=req.get("only_steps") or None,
                    # Child logs go to stdout (parent captures + logs them);
                    # the JSON result travels by file, so this can't corrupt
                    # the envelope.
                    log=lambda *a, **k: print(*a, **k),
                )
            )
        payload: dict[str, Any] = {"ok": True, "result": result}
    except Exception as exc:
        import traceback

        payload = {
            "ok": False,
            "error": (
                _mem_error_text(mem_cap)
                if isinstance(exc, MemoryError) and mem_cap
                else f"{type(exc).__name__}: {exc}"
            ),
            "traceback": traceback.format_exc(),
        }
    tmp_path = f"{out_path}.tmp"
    with open(tmp_path, "w", encoding="utf-8") as fh:
        json.dump(payload, fh)
    os.replace(tmp_path, out_path)
    return 0 if payload["ok"] else 1


if __name__ == "__main__":
    import sys

    raise SystemExit(_subprocess_main(sys.argv))
