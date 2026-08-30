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
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from autocatpath import __version__, provenance
from autocatpath.config import Config
from autocatpath.pipeline import Results, run

from .types import (
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
    the tag :data:`precis.handlers._job_bubble.INFRA_FAILURE_TAGS` reads to
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


def run_seed_partial(
    config: dict[str, Any],
    seed: int,
    model_index: int,
    *,
    force_backend: str | None = None,
    slab_extxyz: str | None = None,
    log: Any = lambda *a, **k: None,
) -> SeedPartialResult:
    """Run ONE ``(model, seed)`` unit of a pathway exploration.

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


def run_seed_partial_subprocess(
    config: dict[str, Any],
    seed: int,
    model_index: int,
    *,
    force_backend: str | None = None,
    slab_extxyz: str | None = None,
    timeout: int = _DEFAULT_SEED_TIMEOUT_S,
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
    import os
    import subprocess
    import tempfile

    request = {
        "config": config,
        "seed": seed,
        "model_index": model_index,
        "force_backend": force_backend,
        "slab_extxyz": slab_extxyz,
    }
    with tempfile.TemporaryDirectory(prefix="autocatpath-seed-") as td:
        req_path = os.path.join(td, "request.json")
        out_path = os.path.join(td, "result.json")
        with open(req_path, "w", encoding="utf-8") as fh:
            json.dump(request, fh)

        cmd = _child_cmd(req_path, out_path)
        try:
            proc = subprocess.run(
                cmd,
                timeout=timeout,
                capture_output=True,
                text=True,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            # subprocess.run has already SIGKILLed the child by here — the
            # hung MACE/CUDA process is gone, the worker pass is free again.
            tail = (exc.stderr or exc.stdout or "")[-2000:]
            raise RuntimeError(
                f"autocatpath_seed compute exceeded its {timeout}s wall budget "
                f"and was killed (seed={seed} model_index={model_index}); "
                f"last output: {tail!r}"
            ) from exc

        if proc.stdout:
            log.debug("autocatpath_seed child stdout:\n%s", proc.stdout[-4000:])
        if proc.stderr:
            log.debug("autocatpath_seed child stderr:\n%s", proc.stderr[-4000:])

        if proc.returncode != 0 or not os.path.exists(out_path):
            tail = (proc.stderr or proc.stdout or "<no output>")[-2000:]
            msg = (
                f"autocatpath_seed subprocess failed (rc={proc.returncode}, "
                f"seed={seed} model_index={model_index}); last output: {tail!r}"
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
        raise RuntimeError(
            f"autocatpath_seed compute error (seed={seed} model_index={model_index}): "
            f"{payload.get('error')}"
        )
    return payload["result"]


def _tail_logs(scratch_dir: str, limit: int = 4000) -> str:
    """Tail of the detached child's captured streams — the diagnosis a
    blocking run gets for free from ``subprocess.run``'s ``capture_output``
    (:func:`run_seed_partial_subprocess`), reconstructed here from the files
    :func:`submit_seed_partial_detached` redirected the child into (a
    detached ``Popen`` can't be waited on for output).

    The budget is split PER STREAM and each is labelled, rather than tailing
    one concatenation of both. Concatenating and slicing once looks
    equivalent and isn't: stderr carries the traceback and the import
    warnings, stdout carries hours of ``print()`` progress, and a single
    trailing window over ``stderr + stdout`` returns the end of *stdout*
    only — silently dropping the very traceback that says why the run died.
    That was latent while stdout was always empty (``_child_cmd`` ran the
    child buffered, so nothing survived a kill); making the child unbuffered
    is exactly what would have armed it.

    ``limit`` bounds the log CONTENT, not the returned string: each stream's
    label adds a few dozen chars on top. Callers that re-truncate to a hard
    chunk budget may therefore clip a leading label — cosmetic, since every
    stream was already sliced to its own share before the labels went on.
    """
    import os

    streams: list[tuple[str, str]] = []
    for name in ("stderr.log", "stdout.log"):
        path = os.path.join(scratch_dir, name)
        try:
            with open(path, encoding="utf-8", errors="replace") as fh:
                text = fh.read()
        except OSError:
            continue
        if text.strip():
            streams.append((name, text))
    if not streams:
        return ""
    per = max(1, limit // len(streams))
    return "\n".join(
        f"--- {name} (last {per} chars) ---\n{text[-per:]}" for name, text in streams
    )


def _cleanup_detached(scratch_dir: str, *, failed: bool = False) -> None:
    """Best-effort scratch-dir removal — a detached submit's dir is a bare
    ``tempfile.mkdtemp`` (not context-managed, unlike the blocking sibling's
    ``TemporaryDirectory``), so nothing else reclaims it.

    Set ``PRECIS_PATHWAY_KEEP_FAILED_SCRATCH=1`` to RETAIN the dir of a run
    that failed, so its full ``stdout.log`` / ``stderr.log`` outlive the
    terse tail lifted into the job event. Off by default deliberately: these
    dirs live in ``/tmp`` on a compute node, and a job type that can fail
    ~100 times a week would turn unconditional retention into a disk-filling
    failure mode of its own — trading one silent outage for another. Turn it
    on while actively diagnosing, off the rest of the time.
    """
    import os
    import shutil

    if failed and os.environ.get("PRECIS_PATHWAY_KEEP_FAILED_SCRATCH") == "1":
        log.warning(
            "autocatpath_seed: retaining failed scratch dir %s "
            "(PRECIS_PATHWAY_KEEP_FAILED_SCRATCH=1)",
            scratch_dir,
        )
        return
    shutil.rmtree(scratch_dir, ignore_errors=True)


# How long a terminal-branch reap will spin WNOHANG for a proven-done child to
# become reapable before giving up (see _reap_zombie). Short: it only has to
# cover normal exit/teardown, never a pathological libcuda hang.
_TERMINAL_REAP_WAIT_S = 1.0


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
    }
    scratch = work_dir or tempfile.mkdtemp(prefix="autocatpath-seed-")
    req_path = os.path.join(scratch, "request.json")
    out_path = os.path.join(scratch, "result.json")
    with open(req_path, "w", encoding="utf-8") as fh:
        _json.dump(request, fh)

    cmd = _child_cmd(req_path, out_path)
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


def poll_seed_partial_detached(handle: DetachedHandle) -> PollResult:
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
    (best-effort) after extracting what they need; the DONE state leaves it
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
            _cleanup_detached(scratch, failed=True)
            return result
        # Envelope parsed cleanly -> the child is done one way or another;
        # reap now, on the SUCCEEDING branch too, or it never happens. A bare
        # WNOHANG would usually observe the envelope (the child's last act)
        # pre-zombie, no-op, and leak the <defunct>; a short bounded spin
        # reaps it deterministically without risking an unbounded block on a
        # slow-teardown child in the poll loop (see _reap_zombie).
        _reap_zombie(pid, wait_s=_TERMINAL_REAP_WAIT_S)
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
        "infra": True,
    }
    _cleanup_detached(scratch, failed=True)
    return result


def finalize_seed_partial_detached(handle: DetachedHandle) -> None:
    """Remove a DONE job's scratch dir — call ONLY after persisting.

    The ``"done"`` branch of :func:`poll_seed_partial_detached` deliberately
    leaves the scratch dir (and its ``result.json`` envelope) on disk so
    that a failed DB persist retries on the next poll instead of discarding
    a finished run. Once the caller's persist has succeeded, this reclaims
    the disk. Best-effort and idempotent (missing dir is a no-op).
    """
    _cleanup_detached(handle["dir"])


def kill_seed_partial_detached(handle: DetachedHandle) -> None:
    """Best-effort SIGKILL of a detached submit's whole process group — the
    ssh_node wall-clock kill hook (§H piece 2), invoked once
    ``meta.deadline`` passes. ``os.killpg`` reaches every descendant the
    child may have spawned (MACE/CUDA workers), not just its own PID — the
    same reason :func:`submit_seed_partial_detached` starts a new session.
    Guards ``ProcessLookupError``/``PermissionError`` (already dead is the
    common case: this only fires once the compute has overrun its own wall
    budget by a full margin) and removes the scratch dir regardless of
    whether the kill landed. Also reaps the killed PID (``os.waitpid``,
    best-effort) when this happens to be the process that submitted it —
    same zombie concern :func:`_process_alive` explains: a killed child
    that's never reaped would otherwise sit defunct in this worker process
    for the rest of its life."""
    import os
    import signal

    pgid = handle.get("pgid")
    if pgid is not None:
        try:
            os.killpg(int(pgid), signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass
    pid = handle.get("pid")
    if pid is not None:
        try:
            os.waitpid(int(pid), 0)
        except (ChildProcessError, OSError):
            pass
    scratch = handle.get("dir")
    if scratch:
        # Log the tail before wiping: this path returns ``None`` (the
        # executor terminalizes from its own side), so unlike the poll
        # branches there is no channel to hand the child's output back on —
        # and it deleted the dir without ever reading it. Every wall-clock
        # kill therefore explained itself with nothing but the handle dict.
        # SIGKILL leaves no in-process buffer to flush, but with an
        # unbuffered child (:func:`_child_cmd`) the progress is already ON
        # DISK, so there is now something real to capture here.
        tail = _tail_logs(scratch)
        if tail:
            log.warning(
                "autocatpath_seed: wall-clock kill of pid=%s — child output:\n%s",
                handle.get("pid"),
                tail,
            )
        _cleanup_detached(scratch, failed=True)


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


def run_kinetics(config: dict[str, Any], artifact: PathwayArtifact) -> None:
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
        result = kinetics.solve(
            cfg, nodes, edges, net, refs, span=span, mari=None, log=lambda *a, **k: None
        )
        results_json["kinetics"] = result
    except Exception as exc:
        log.warning("autocatpath kinetics: solve failed", exc_info=True)
        results_json["kinetics_error"] = str(exc)


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
    with open(req_path, encoding="utf-8") as fh:
        req = json.load(fh)
    try:
        result = run_seed_partial(
            req["config"],
            int(req["seed"]),
            int(req["model_index"]),
            force_backend=req.get("force_backend"),
            slab_extxyz=req.get("slab_extxyz"),
            # Child logs go to stdout (parent captures + logs them); the JSON
            # result travels by file, so this can't corrupt the envelope.
            log=lambda *a, **k: print(*a, **k),
        )
        payload: dict[str, Any] = {"ok": True, "result": result}
    except Exception as exc:
        import traceback

        payload = {
            "ok": False,
            "error": f"{type(exc).__name__}: {exc}",
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
