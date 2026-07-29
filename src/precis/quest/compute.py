"""Quest compute dispatch — candidates become `structure` sims (slice 4b).

The local grind of the autonomous loop: a tick proposes candidate materials,
each becomes a `structure` that ``serves`` the quest (the graph *is* the memory
of explored space), we dispatch its relax on the GPU node (the ADR-0044 derived
compute lane, content-addressed so a re-proposed candidate is a cache hit), and
a later harvest reads the measures back into the logbook. Failed candidates stay
linked and get a ``ruled-out:`` tag so the proposer never re-treads them; the
converged ones feed the Pareto frontier (:mod:`precis.quest.frontier`).

A candidate carries an atomistic **structure spec** (``{cell, ops}``) — the
proposer's job (:mod:`precis.quest.tick`). A proposal with no structure spec is
still recorded as a logbook `hypothesis`, but mints no sim (a weak proposer just
produces no compute, which is *visible* rather than silently wrong).

Compute dispatch is **off by default** (``compute=False`` on the tick); the
manual ``precis quest tick --compute`` and the future autonomous dispatcher
(``PRECIS_QUEST_LOOP_ENABLED``, rung 4d) turn it on. ``dispatch_relax`` is a
thin, defensive wrapper (it degrades to a note on any error) and is the seam
tests monkeypatch to avoid real compute.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from precis.quest.logbook import MEASURED_BY, append_entry
from precis.store import Tag
from precis.structure.preflight import PreflightReason
from precis.structure.preflight import _preflight_enabled as _mlip_preflight_enabled
from precis.structure.preflight import preflight as _mlip_preflight

if TYPE_CHECKING:
    from precis.store import Store

log = logging.getLogger(__name__)

#: The GPU relax rung a quest dispatches by default (cheap ML potential).
_DEFAULT_FIDELITY = "ml"
_CANDIDATE_TAG = "candidate"


@dataclass(frozen=True)
class ComputeStep:
    candidates_created: int
    sims_dispatched: int
    results_harvested: int
    ruled_out: int
    notes: list[str]
    graduated: int = 0


def _canonical_spec(spec: dict[str, Any]) -> str:
    return json.dumps(spec, sort_keys=True, separators=(",", ":"))


def _candidate_slug(quest_id: int, spec: dict[str, Any]) -> str:
    """Content-addressed slug: the same material spec → the same structure."""
    digest = hashlib.sha256(_canonical_spec(spec).encode()).hexdigest()[:10]
    return f"q{quest_id}cand-{digest}"


def _hub_for(store: Store) -> Any:
    from precis.dispatch import Hub

    return Hub(store=store)


def ensure_candidate(
    store: Store, quest_id: int, proposal: dict[str, Any], *, hub: Any | None = None
) -> int | None:
    """Create (or reuse) the `structure` server for a proposal's spec.

    Returns the structure ref id, or ``None`` when the proposal carries no
    usable structure spec (nothing to simulate). Content-addressed: a repeat
    proposal of the same material returns the existing structure.
    """
    spec = proposal.get("structure")
    if not isinstance(spec, dict):
        return None
    # A candidate needs a cell — either given directly, or established by a bulk
    # template op (`slab`) / a `set_cell` op (a Pd(111) slab is 30+ atoms; the
    # proposer emits the compact `slab` op, not a hand-enumerated cell).
    ops = spec.get("ops") or []
    has_cell = "cell" in spec or (
        isinstance(ops, list)
        and any(
            isinstance(o, dict) and o.get("op") in ("slab", "set_cell") for o in ops
        )
    )
    if not has_cell:
        return None
    slug = _candidate_slug(quest_id, spec)
    existing = store.get_ref(kind="structure", id=slug)
    if existing is not None:
        return int(existing.id)

    hub = hub or _hub_for(store)
    from precis.handlers.structure import StructureHandler

    name = str(proposal.get("name") or slug)
    try:
        StructureHandler(hub=hub).put(id=slug, text=json.dumps(spec), title=name)
    except Exception:
        return None
    ref = store.get_ref(kind="structure", id=slug)
    if ref is None:  # pragma: no cover - put just created it
        return None
    with store.tx() as conn:
        store.add_link(
            src_ref_id=ref.id, dst_ref_id=quest_id, relation="serves", conn=conn
        )
        store.add_tag(ref.id, Tag.open(_CANDIDATE_TAG), set_by="system", conn=conn)
    return int(ref.id)


def dispatch_relax(
    store: Store,
    structure_ref_id: int,
    *,
    hub: Any | None = None,
    fidelity: str = _DEFAULT_FIDELITY,
    model: str | None = None,
    steps: int = 200,
    cell: str | None = None,
) -> str:
    """Dispatch a relax on a candidate structure (the derived compute lane).

    A thin, **defensive** wrapper over ``StructureHandler.edit(op='relax')``:
    it mints the content-addressed ``struct_relax`` job (idempotent — a second
    dispatch of the same geometry collapses onto the in-flight job). We do NOT
    pass ``requested_by`` — that would arm a ``derived_job_succeeded`` auto-check
    that *closes* the requester, and a quest never closes; the loop instead
    harvests measures when they land (:func:`harvest_measures`). Returns a short
    status note; never raises (a compute hiccup must not fail the tick).
    """
    refs = store.fetch_refs_by_ids({structure_ref_id})
    ref = refs.get(structure_ref_id)
    if ref is None or ref.slug is None:
        return f"relax skipped: structure {structure_ref_id} not found"
    hub = hub or _hub_for(store)
    from precis.handlers.structure import StructureHandler

    op: dict[str, Any] = {"op": "relax", "fidelity": fidelity, "steps": steps}
    if model is not None:
        op["model"] = model
    if cell is not None:
        op["cell"] = cell
    try:
        StructureHandler(hub=hub).edit(id=str(ref.slug), ops=[op])
    except Exception as e:
        return f"relax dispatch failed for {ref.slug}: {e}"
    return f"relax[{fidelity}] dispatched for {ref.slug}"


#: Env pin for the node that runs autocatpath (has the plugin + an ML backend). When
#: unset the job routes nowhere special and force-EMT keeps an in-process demo cheap.
_AUTOCATPATH_ROUTE_NODE_ENV = "PRECIS_AUTOCATPATH_ROUTE_NODE"


#: Env pin for the autocatpath NEB wall-time hint (see :func:`_autocatpath_wall_seconds`).
_AUTOCATPATH_WALL_SECONDS_ENV = "PRECIS_AUTOCATPATH_WALL_SECONDS"


def _autocatpath_wall_seconds() -> int:
    """Expected wall-time hint (s) for a autocatpath NEB, stamped into the job's
    ``resources`` so the ssh_node lease outlives a full-network run.

    Env-tunable (``PRECIS_AUTOCATPATH_WALL_SECONDS``, default 5400 = 90 min): a
    3×3×4 full ammonia-network run is ~15-20 min uncontended but can stretch
    under load. ssh_node leases at ``max(2h floor, wall_seconds + 1h margin)``,
    so 5400 → a 2.5h lease. Confirmed wired end-to-end (this value lands on
    the dispatched job's ``params.resources.wall_seconds``, which is exactly
    the field ``ssh_node._lease_seconds`` reads) by
    ``TestDispatchAutocatpath.test_wall_seconds_env_reaches_the_job_and_the_ssh_node_lease``
    in ``tests/test_quest_compute.py``.
    """
    try:
        n = int(os.environ.get(_AUTOCATPATH_WALL_SECONDS_ENV, "5400"))
    except ValueError:
        return 5400
    return max(60, min(86_400, n))


#: Engine-version token folded into the autocatpath idem key so deploying a new
#: autocatpath build auto-invalidates stale completed jobs. Without it, a
#: re-dispatch of the same (config, slab) dedupes onto the old completed job and
#: never exercises new engine code — the qu164903 "empty frontier" trap: all 21
#: candidates were pinned on autocatpath 0.1.1's desorption false-positives (102
#: phantom "detached" warnings → barrier_trusted=false) and never re-scored on
#: 0.4.0, which relaxes the same geometries cleanly (0 detached, trusted). The
#: ansible ``autocatpath`` role exports ``PRECIS_AUTOCATPATH_VERSION`` with the
#: installed version/git-sha; absent that, ``_AUTOCATPATH_CACHE_EPOCH`` is the
#: manual bump lever — changing it re-keys every candidate, forcing a clean
#: re-dispatch on the deployed engine.
_AUTOCATPATH_VERSION_ENV = "PRECIS_AUTOCATPATH_VERSION"
_AUTOCATPATH_CACHE_EPOCH = "0.4.0"


def _autocatpath_engine_token() -> str:
    """Engine-version component of the idem key — the deployed version pinned by
    the ansible role (``PRECIS_AUTOCATPATH_VERSION``), else the code-constant
    cache epoch. A change in this value re-keys every (config, slab) pair, so a
    new engine build re-evaluates candidates instead of reusing stale results."""
    return os.environ.get(_AUTOCATPATH_VERSION_ENV) or _AUTOCATPATH_CACHE_EPOCH


def _autocatpath_content_key(config: dict[str, Any], slab_extxyz: str) -> str:
    """Stable idempotency key for an (engine, reaction, exported slab) triple.

    Its own hash (not autocatpath's ``content_key``) so this stays precis-native — a
    re-dispatch of the same engine + geometry + reaction collapses onto the
    in-flight job, while a new engine build (a changed
    :func:`_autocatpath_engine_token`) deliberately misses the old job so the
    candidate is re-scored.
    """
    payload = (
        _autocatpath_engine_token()
        + "\n"
        + _canonical_spec(config)
        + "\n"
        + slab_extxyz
    )
    return hashlib.sha256(payload.encode()).hexdigest()


def dispatch_autocatpath(
    store: Store,
    structure_ref_id: int,
    config: dict[str, Any],
    *,
    hub: Any | None = None,
    force_backend: str | None = None,
) -> str:
    """Dispatch a autocatpath barrier evaluation on a candidate structure.

    Exports the candidate's (relaxed) geometry as extxyz, ensures a `pathway` ref
    for the write-back, and mints a ``autocatpath_explore`` job **pinned on the
    candidate** — so :func:`harvest_measures` finds it under the structure's
    compute lane (it queries ``parent_id = candidate``, unlike the standalone
    `pathway` handler which parents on the pathway ref). The job hydrates the
    extxyz into a prepared slab (autocatpath's injected-slab seam) and runs the
    reaction network on the routed node; on completion it emits a scalar
    ``barrier`` onto its own meta, which the harvest lifts onto the candidate.

    Precis-native (no autocatpath import — the `pathway` kind, if the plugin is
    installed, is reached only through the store) and **defensive**: degrades to a
    note on any error (missing plugin, unloadable scene) and never raises, so a
    compute hiccup can't fail the tick.
    """
    if not isinstance(config, dict) or not config:
        return (
            f"autocatpath skipped: no reaction config for structure {structure_ref_id}"
        )
    refs = store.fetch_refs_by_ids({structure_ref_id})
    ref = refs.get(structure_ref_id)
    if ref is None or ref.slug is None:
        return f"autocatpath skipped: structure {structure_ref_id} not found"
    hub = hub or _hub_for(store)

    # Export the candidate geometry — the injected-slab seam autocatpath consumes.
    try:
        from precis.structure import export

        scene, _handles = store.structure_load(structure_ref_id)
        if _mlip_preflight_enabled():
            # Tier-0 hard gate (gated, default off): a substrate the MLIP
            # can't handle mints no compute at all — cheaper than burning a
            # GPU NEB on a geometry that would fail anyway, and the proposer
            # gets a dead-end stamp so it stops re-treading the same
            # material. Isolated in its own try so a preflight-internal
            # hiccup (ASE/[dft] missing, or anything else) fails OPEN —
            # it must never be mistaken for the export failure below.
            try:
                verdict = _mlip_preflight(scene)
            except Exception as exc:
                log.debug(
                    "autocatpath preflight degraded (fail-open) for %s: %s",
                    ref.slug,
                    exc,
                )
                verdict = None
            if verdict is not None and not verdict.ok:
                _stamp_preflight_dead_end(
                    store, structure_ref_id, str(ref.slug), verdict.reasons
                )
                summary = "; ".join(r.message for r in verdict.reasons)
                return (
                    f"autocatpath skipped: {ref.slug} failed substrate preflight "
                    f"— {summary}"
                )
        # constraints=True → the slab's frozen bottom layers ride along as a
        # FixAtoms, so autocatpath's injected-slab relax/NEB keeps them fixed.
        slab_extxyz = export.to_extxyz(scene, constraints=True)
    except Exception as e:
        return f"autocatpath dispatch failed for {ref.slug}: export ({e})"

    node = os.environ.get(_AUTOCATPATH_ROUTE_NODE_ENV) or None
    # Routed → run the config's own backend on the pinned node; unrouted → EMT
    # (an in-process demo has no ML backend). An explicit override wins either way.
    force = force_backend or (None if node else "emt")
    # Routed nodes are the GPU boxes (topology: autocatpath → the CUDA node), so pin
    # the ML potential to cuda there — autocatpath's MLIPConfig.device defaults to
    # "cpu", which otherwise leaves the GPU idle and the NEB CPU-bound (~20×
    # slower). Copy the config so we neither mutate the caller's dict nor churn
    # the content key when unrouted; an explicit mlip.device wins (setdefault).
    run_config = config
    if node:
        run_config = {**config, "mlip": {**(config.get("mlip") or {})}}
        run_config["mlip"].setdefault("device", "cuda")
    key = _autocatpath_content_key(run_config, slab_extxyz)
    pslug = f"{ref.slug}-rx-{key[:10]}"

    # Ensure the pathway ref (status=computing) the job writes its graph back onto.
    try:
        existing = store.get_ref(kind="pathway", id=pslug)
        if existing is not None:
            pathway_ref_id = int(existing.id)
        else:
            with store.tx() as conn:
                pref = store.insert_ref(
                    kind="pathway",
                    slug=pslug,
                    title=f"pathway {pslug} (computing)",
                    meta={
                        "content_key": key,
                        "status": "computing",
                        "candidate_ref": structure_ref_id,
                    },
                    conn=conn,
                )
            pathway_ref_id = int(pref.id)
    except Exception as e:
        return f"autocatpath dispatch failed for {ref.slug}: pathway ref ({e})"

    # Mint the compute-lane job PINNED ON THE CANDIDATE (harvest queries parent_id).
    try:
        from precis.handlers.job import JobHandler

        JobHandler(hub=hub).put(
            job_type="autocatpath_explore",
            executor="ssh_node",
            parent_id=structure_ref_id,
            idem_key=f"autocatpath_explore:{key}",
            params={
                "pathway_ref_id": pathway_ref_id,
                "pathway_slug": pslug,
                "config": run_config,
                "slab_extxyz": slab_extxyz,
                "structure_ref": structure_ref_id,
                "force_backend": force,
                "content_key": key,
                "target_node": node,
                # Lease margin for a full reaction-network NEB: the ssh_node
                # lease is max(2h floor, wall_seconds + 1h margin), so this lifts
                # a slow (contended) full-network run's lease clear of its
                # runtime — otherwise it can lease-expire mid-run and get
                # stolen/restarted (the churn the autonomous loop must avoid).
                "resources": {"wall_seconds": _autocatpath_wall_seconds()},
            },
        )
    except Exception as e:
        return f"autocatpath dispatch failed for {ref.slug}: job mint ({e})"
    return (
        f"autocatpath[{force or 'config'}] dispatched for {ref.slug} → pathway {pslug}"
    )


def _serving_quest_id(store: Store, structure_ref_id: int) -> int | None:
    """The quest a candidate `structure` serves (the ``serves`` link
    :func:`ensure_candidate` writes on creation), or ``None`` if there isn't
    one (a candidate probed standalone, e.g. from a test)."""
    links = store.links_for(structure_ref_id, direction="out", relation="serves")
    for link in links:
        return int(link.dst_ref_id)
    return None


def _stamp_preflight_dead_end(
    store: Store,
    structure_ref_id: int,
    slug: str,
    reasons: list[PreflightReason],
) -> None:
    """One-shot dead-end stamp for a preflight-failing candidate — the
    dispatch-time mirror of :func:`harvest_measures`'s ``ruled-out:`` +
    ``dead-end`` pattern for a relax that failed to converge. Tags the
    candidate ``ruled-out:preflight`` and appends a `dead-end` logbook entry
    naming the candidate + its top reason(s), so the next tick's proposer
    sees this substrate as already-explored dead ground instead of
    re-proposing the same broken geometry.

    Idempotent-ish: a no-op once *any* ``ruled-out:`` tag is already present
    (mirrors :func:`harvest_measures`'s ``already_out`` guard) — a repeat
    dispatch attempt on the same still-broken candidate doesn't spam the
    logbook every tick.
    """
    if any(str(t).startswith("ruled-out:") for t in store.tags_for(structure_ref_id)):
        return
    store.add_tag(structure_ref_id, Tag.open("ruled-out:preflight"), set_by="system")
    quest_id = _serving_quest_id(store, structure_ref_id)
    if quest_id is None:
        return
    from precis.utils import handle_registry

    handle = (
        handle_registry.try_format("structure", structure_ref_id)
        or f"structure:{structure_ref_id}"
    )
    top = "; ".join(r.message for r in reasons[:2])
    append_entry(
        store,
        quest_id,
        text=f"ruled out {handle} ({slug}): failed substrate preflight — {top}",
        entry_type="dead-end",
        by=MEASURED_BY,
    )


#: Job-meta spellings that carry autocatpath's rate-limiting barrier (eV). The
#: `autocatpath_explore` job exposes a scalar summary so the quest can harvest it
#: without importing autocatpath or reading the (plugin-kind) `pathway` ref.
_AUTOCATPATH_BARRIER_KEYS: tuple[str, ...] = ("barrier", "rate_Ea", "rate_ea", "ea")
_AUTOCATPATH_SPAN_KEYS: tuple[str, ...] = ("span",)


def _num_measure(v: Any) -> float | None:
    """A numeric measure, or None (``bool`` is an ``int`` but never a measure)."""
    if isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return float(v)
    return None


def _autocatpath_measures_from_job(meta: dict[str, Any]) -> dict[str, float]:
    """Lift the scalar barrier/span from a completed `autocatpath_explore` job's meta.

    Reads a ``result`` sub-dict if present (the bridge's summary), else the meta
    top level. The presence of a numeric barrier IS the "done" signal — a
    still-running job carries none, so it is simply skipped.
    """
    src = meta.get("result") if isinstance(meta.get("result"), dict) else meta
    out: dict[str, float] = {}
    for k in _AUTOCATPATH_BARRIER_KEYS:
        v = _num_measure(src.get(k))
        if v is not None:
            out["barrier"] = v
            break
    for k in _AUTOCATPATH_SPAN_KEYS:
        v = _num_measure(src.get(k))
        if v is not None:
            out["span"] = v
            break
    # adsorption barrier the dissolving tether had to overcome to reseat a
    # desorbing endpoint (pathway max). A trust/annotation diagnostic — NOT a
    # Pareto objective (excluded from ranking via frontier._META_NON_MEASURE).
    v = _num_measure(src.get("adsorption_barrier"))
    if v is not None:
        out["adsorption_barrier"] = v
    return out


#: Warning substrings (case-sensitive, matched anywhere in a `pathway` ref's
#: ``meta['warnings']`` strings) that mark a harvested barrier as untrustworthy:
#: an NEB edge that never converged, or an adsorbate that desorbed off the slab
#: mid-relax. Kept as module constants so :func:`_pathway_quality` and any
#: future caller (e.g. a diagnostic report) match on the same strings.
_NEB_NOT_CONVERGED = "NEB not converged"
_ADSORBATE_DETACHED = "detached"
#: an endpoint that relaxed bound but through the WRONG atom — the reaction
#: label's ``*`` designates a different binder (autocatpath ``validate.binding_site_ok``).
#: A barrier off a mis-bound endpoint is as untrustworthy as one off a desorbed one.
_WRONG_BINDING_SITE = "wrong-site"


def _pathway_quality(meta: dict[str, Any]) -> dict[str, Any]:
    """Derive the trust verdict on a harvested barrier from its pathway's meta.

    ``meta`` is the linked `pathway` ref's meta (``meta['warnings']`` — a list
    of human-readable strings — and ``meta['low_confidence']``, a *separate*,
    less informative flag: a single-seed quest run always sets it (autocatpath's
    ``low_confidence = std>tol OR n<2``), so it rides along for visibility but
    never gates trust on its own). Counts warnings mentioning a non-converged
    NEB edge, a desorbed adsorbate, and a wrong-site (mis-bound) endpoint;
    ``barrier_trusted`` is False iff any of those counts is nonzero.
    """
    warnings = meta.get("warnings")
    warnings = warnings if isinstance(warnings, list) else []
    n_neb_failed = sum(1 for w in warnings if _NEB_NOT_CONVERGED in str(w))
    n_desorbed = sum(1 for w in warnings if _ADSORBATE_DETACHED in str(w))
    n_wrong_site = sum(1 for w in warnings if _WRONG_BINDING_SITE in str(w))
    return {
        "barrier_trusted": n_neb_failed == 0 and n_desorbed == 0 and n_wrong_site == 0,
        "barrier_neb_failed": n_neb_failed,
        "barrier_desorbed": n_desorbed,
        "barrier_wrong_site": n_wrong_site,
        "barrier_low_confidence": bool(meta.get("low_confidence")),
    }


def _fresh_autocatpath_jobs(
    store: Store, structure_ref_id: int, upto: int
) -> list[tuple[int, dict[str, Any]]]:
    """Completed `autocatpath_explore` jobs under a candidate, newer than ``upto``.

    Returns ``(job_ref_id, meta)`` oldest-first so harvest is deterministic and
    the idempotency bookmark advances monotonically.
    """
    with store.pool.connection() as conn:
        rows = conn.execute(
            "SELECT j.ref_id, j.meta FROM refs j "
            "WHERE j.parent_id = %s AND j.kind = 'job' AND j.deleted_at IS NULL "
            "AND j.meta->>'job_type' = 'autocatpath_explore' AND j.ref_id > %s "
            "ORDER BY j.ref_id ASC",
            (structure_ref_id, upto),
        ).fetchall()
    return [(int(r[0]), dict(r[1] or {})) for r in rows]


def _link_pathway(store: Store, structure_ref_id: int, pathway_ref_id: int) -> None:
    """Wire the evaluating `pathway` into the quest graph (idempotent).

    The autocatpath bridge creates the pathway ref; we link the candidate structure
    to it so a later by-intermediate view can find the per-path profile.
    Symmetric ``related-to`` (the relation the bridge already uses, valid on any
    ref). Defensive: a missing pathway / relation must never break the harvest.
    """
    try:
        existing = store.links_for(
            structure_ref_id, direction="both", relation="related-to"
        )
        if any(pathway_ref_id in (ln.src_ref_id, ln.dst_ref_id) for ln in existing):
            return
        store.add_link(
            src_ref_id=structure_ref_id,
            dst_ref_id=pathway_ref_id,
            relation="related-to",
            set_by="system",
        )
    except Exception:
        pass


def _latest_relax_job(
    store: Store, structure_ref_id: int
) -> tuple[str, dict[str, Any]] | None:
    """The latest ``struct_relax`` job's ``(STATUS, meta)`` under this candidate.

    ``meta`` carries ``failure_class`` when the job failed (``"infra"`` vs
    ``"non-convergence"`` — see :mod:`precis.workers.job_types.struct_relax`),
    which the harvest loop below reads to decide whether a failure is a real
    physical verdict on the candidate or just the executor dying.
    """
    with store.pool.connection() as conn:
        row = conn.execute(
            "SELECT t.value, j.meta FROM refs j "
            "JOIN ref_tags rt ON rt.ref_id = j.ref_id "
            "JOIN tags t ON t.tag_id = rt.tag_id "
            "WHERE j.parent_id = %s AND j.kind = 'job' AND j.deleted_at IS NULL "
            "AND j.meta->>'job_type' = 'struct_relax' AND t.namespace = 'STATUS' "
            "ORDER BY j.ref_id DESC LIMIT 1",
            (structure_ref_id,),
        ).fetchone()
    if row is None:
        return None
    return str(row[0]), dict(row[1] or {})


def _latest_autocatpath_job(
    store: Store, structure_ref_id: int
) -> tuple[str, dict[str, Any]] | None:
    """The latest ``autocatpath_explore`` job's ``(STATUS, meta)`` under this candidate.

    The sibling of :func:`_latest_relax_job` for the barrier lane. Unlike relax —
    where a ``failed`` job may carry a genuine *physical* verdict (non-convergence
    ⇒ rule the candidate out) — a failed ``autocatpath_explore`` is **always** a
    compute/infra failure: the NEB/barrier run crashed, which says nothing about
    whether the material has a viable pathway. So the harvest treats every autocatpath
    failure as retry-eligible (ADR 0064 §C) and never rules out on it.
    """
    with store.pool.connection() as conn:
        row = conn.execute(
            "SELECT t.value, j.meta FROM refs j "
            "JOIN ref_tags rt ON rt.ref_id = j.ref_id "
            "JOIN tags t ON t.tag_id = rt.tag_id "
            "WHERE j.parent_id = %s AND j.kind = 'job' AND j.deleted_at IS NULL "
            "AND j.meta->>'job_type' = 'autocatpath_explore' AND t.namespace = 'STATUS' "
            "ORDER BY j.ref_id DESC LIMIT 1",
            (structure_ref_id,),
        ).fetchone()
    if row is None:
        return None
    return str(row[0]), dict(row[1] or {})


def _mark_harvested(store: Store, structure_ref_id: int, upto_run_id: int) -> None:
    with store.tx() as conn:
        conn.execute(
            "UPDATE refs SET meta = meta || jsonb_build_object("
            "'quest_harvested_upto', %s::int) WHERE ref_id = %s",
            (upto_run_id, structure_ref_id),
        )


#: How many infra-failure retries a candidate's relax gets before the harvest
#: stops re-dispatching and files a gripe instead (see ``harvest_measures``).
#: Not an env dial (ADR 0064 §C) — retry-once-then-gripe is the whole point:
#: a higher ceiling would let a genuinely wedged executor silently spin.
_MAX_INFRA_RETRIES = 1


def _file_infra_gripe(
    store: Store,
    quest_id: int,
    handle: str,
    job_meta: dict[str, Any],
    *,
    hub: Any,
    lane: str = "relax",
) -> None:
    """File a bounded, visible gripe for a candidate whose ``lane`` sim
    (``relax`` or ``autocatpath``) has now infra-failed twice — never rules the
    candidate out (still no physical verdict), just surfaces the persistent
    executor problem for a human."""
    from precis.handlers.gripe import GripeHandler

    detail = {
        k: job_meta.get(k)
        for k in ("failure_class", "error", "note", "job_type")
        if k in job_meta
    }
    GripeHandler(hub=hub).put(
        text=(
            f"quest {quest_id} candidate {handle} {lane} sim infra-failing "
            "repeatedly (2×) — spark/executor. "
            f"Latest {lane} job failure detail: {detail}"
        ),
        tags=["quest-infra-failure"],
    )


def harvest_measures(
    store: Store,
    quest_id: int,
    *,
    by: str = "agent",
    hub: Any | None = None,
    relax_cell: str | None = None,
) -> ComputeStep:
    """Read finished sims back into the logbook + rule out failures.

    Every entry this function appends is a **system measurement** (a
    converged relax, a harvested autocatpath barrier, a ruled-out verdict) — so
    each is stamped ``by=MEASURED_BY`` ("system"), never the caller's ``by``
    (the model's own "agent" attribution). That is what makes a real
    measurement distinguishable from model narration in the logbook: gripes
    171148/171149 diagnosed a model-fabricated "result" entry (a barrier the
    model invented, not one autocatpath measured) reading as indistinguishable
    ground truth, which made the loop believe the quest was solved and stop
    proposing candidates. ``by`` is kept in the signature for call-site
    compat (and used elsewhere in this module, e.g. dispatch notes are not
    logbook entries) but is no longer used for these measured entries.

    For each candidate `structure` serving the quest:

    * newly-converged **relax** runs become `result` logbook entries (energy + a
      step-count cost proxy), tracked idempotently by ``meta.quest_harvested_upto``;
    * completed **autocatpath** (`autocatpath_explore`) jobs contribute the rate-limiting
      **barrier** (and span): lifted onto the candidate's own ``meta`` (where the
      generalised frontier reads it), the evaluating pathway linked into the quest
      graph, logged as a `result`, tracked by ``meta.quest_autocatpath_harvested_upto``;
    * a candidate whose latest relax job **failed for a genuine
      non-convergence reason** gets a one-shot ``ruled-out:relax-failed`` tag +
      a `dead-end` entry so the proposer stops re-treading it.
    * a candidate whose latest relax job failed with ``failure_class="infra"``
      (container/executor died — not a physical verdict) does NOT rule out —
      otherwise a container hiccup launders into "this material is unstable"
      in the live dossier. ADR 0064 §C: when ``hub`` is given, the *first*
      infra failure gets re-dispatched once (``meta.quest_infra_retries``
      tracks it) so the candidate goes back to non-terminal and the loop
      *awaits* it instead of drifting dry; a *second* infra failure files a
      bounded gripe instead of retrying again, and stays retry-eligible in
      neither sense (no third dispatch, never ruled out). ``hub=None``
      (dry preview / callers that don't exercise this) preserves the
      original note-only behaviour.
    * a candidate whose latest **autocatpath** (`autocatpath_explore`) job failed gets
      the *same* retry-once-then-gripe treatment on its own counter
      (``meta.quest_autocatpath_infra_retries``), but **never** ruled out: a failed
      autocatpath is always a crashed NEB (a compute/infra failure), never a
      physical "no viable pathway" verdict, so — unlike relax non-convergence —
      it carries no verdict on the material (ADR 0064 §C, barrier-lane mirror).
    """
    from precis.quest.gaps import _live_servers
    from precis.utils import handle_registry

    structures = [s for s in _live_servers(store, quest_id) if s.kind == "structure"]
    harvested = ruled_out = 0
    notes: list[str] = []
    for s in structures:
        handle = handle_registry.try_format("structure", s.id) or f"structure:{s.id}"
        name = (s.title or "").splitlines()[0] if s.title else handle
        upto = int((s.meta or {}).get("quest_harvested_upto", 0) or 0)
        runs = store.structure_runs(s.id)
        fresh = [r for r in runs if r.get("converged") and int(r.get("id", 0)) > upto]
        for r in sorted(fresh, key=lambda r: int(r.get("id", 0))):
            energy = r.get("energy")
            e_s = (
                f"E={energy:g} eV" if isinstance(energy, (int, float)) else "no energy"
            )
            append_entry(
                store,
                quest_id,
                text=(
                    f"relax result for {handle} ({name}): {e_s}, "
                    f"{r.get('n_steps')} steps, converged"
                ),
                entry_type="result",
                by=MEASURED_BY,
                cost=float(r.get("n_steps") or 0),
            )
            harvested += 1
        if fresh:
            _mark_harvested(store, s.id, max(int(r.get("id", 0)) for r in fresh))

        # Harvest autocatpath barriers: a completed `autocatpath_explore` job under this
        # candidate carries the rate-limiting barrier; lift it onto the
        # candidate's own meta (where the generalised frontier reads it), link
        # the evaluating pathway into the quest graph, and log a result entry.
        cp_upto = int((s.meta or {}).get("quest_autocatpath_harvested_upto", 0) or 0)
        cp_jobs = _fresh_autocatpath_jobs(store, s.id, cp_upto)
        cp_seen = cp_upto
        for job_id, jmeta in cp_jobs:
            measures = _autocatpath_measures_from_job(jmeta)
            if not measures:
                continue  # still running — do not advance the bookmark, retry next tick
            cp_seen = max(cp_seen, job_id)
            pathway_ref = jmeta.get("pathway_ref")
            if isinstance(pathway_ref, int) and not isinstance(pathway_ref, bool):
                # Defensive: an unfetchable / meta-less pathway ref stamps no
                # trust flags at all (treated as unknown by graduate_frontier),
                # rather than crashing the harvest.
                try:
                    pw_refs = store.fetch_refs_by_ids({pathway_ref})
                    pw_ref = pw_refs.get(pathway_ref)
                    pw_meta = pw_ref.meta if pw_ref is not None else None
                    if isinstance(pw_meta, dict):
                        measures.update(_pathway_quality(pw_meta))
                except Exception:
                    pass
            store.stamp_ref_meta(s.id, measures)
            if isinstance(pathway_ref, int) and not isinstance(pathway_ref, bool):
                _link_pathway(store, s.id, pathway_ref)
            b = measures.get("barrier")
            b_s = f"barrier={b:g} eV" if isinstance(b, (int, float)) else "measured"
            append_entry(
                store,
                quest_id,
                text=f"autocatpath result for {handle} ({name}): {b_s}",
                entry_type="result",
                by=MEASURED_BY,
            )
            harvested += 1
        if cp_seen > cp_upto:
            store.stamp_ref_meta(s.id, {"quest_autocatpath_harvested_upto": cp_seen})

        # Rule out a candidate whose relax job failed for a genuine physical
        # reason (once) — but NOT an infra failure (container/executor died),
        # which carries no verdict on the candidate. An infra failure instead
        # gets retried once (hub given), then gripes on a second occurrence
        # (ADR 0064 §C) — see the docstring above.
        already_out = any(str(t).startswith("ruled-out:") for t in store.tags_for(s.id))
        relax_job = _latest_relax_job(store, s.id)
        if not already_out and relax_job is not None and relax_job[0] == "failed":
            _status, job_meta = relax_job
            failure_class = job_meta.get("failure_class")
            if failure_class == "infra":
                retries = int((s.meta or {}).get("quest_infra_retries", 0) or 0)
                if hub is None:
                    notes.append(
                        f"infra failure for {handle} (retry-eligible, not ruled out)"
                    )
                elif retries < _MAX_INFRA_RETRIES:
                    dispatch_relax(store, s.id, hub=hub, cell=relax_cell)
                    store.stamp_ref_meta(s.id, {"quest_infra_retries": retries + 1})
                    notes.append(
                        f"infra failure for {handle} → re-dispatched "
                        f"(retry {retries + 1})"
                    )
                elif retries < _MAX_INFRA_RETRIES + 1:
                    _file_infra_gripe(store, quest_id, handle, job_meta, hub=hub)
                    store.stamp_ref_meta(
                        s.id, {"quest_infra_retries": _MAX_INFRA_RETRIES + 1}
                    )
                    notes.append(f"infra failure persists for {handle} → gripe filed")
                else:
                    # Already gripe-filed on a prior harvest — dedup, no re-file.
                    notes.append(
                        f"infra failure persists for {handle} (gripe already filed)"
                    )
            else:
                store.add_tag(s.id, Tag.open("ruled-out:relax-failed"), set_by="system")
                append_entry(
                    store,
                    quest_id,
                    text=f"ruled out {handle} ({name}): relax failed to converge",
                    entry_type="dead-end",
                    by=MEASURED_BY,
                )
                ruled_out += 1
                notes.append(f"ruled-out {handle}")

        # Autocatpath (barrier-lane) infra failure — the ADR 0064 §C mirror of the
        # relax infra branch above, on the *barrier* lane. Unlike relax (where a
        # failed job can be a physical non-convergence verdict → rule out), a
        # failed ``autocatpath_explore`` is ALWAYS a compute/infra failure: the NEB
        # run crashed, which says nothing about the material — so it NEVER rules
        # out. Same retry-once-then-gripe shape on its own per-candidate counter
        # (``quest_autocatpath_infra_retries``); the re-dispatch puts a fresh sim
        # back in flight so the loop *awaits* it instead of reading the crash as
        # a dry tick (the laundering §C names). Skipped for an already-ruled-out
        # candidate (a dead geometry earns no more barrier compute), and — like
        # relax — note-only when ``hub`` is absent (dry preview) or the quest
        # has no reaction config to re-dispatch against.
        cp_ruled_out = any(
            str(t).startswith("ruled-out:") for t in store.tags_for(s.id)
        )
        autocatpath_job = _latest_autocatpath_job(store, s.id)
        if (
            not cp_ruled_out
            and autocatpath_job is not None
            and autocatpath_job[0] == "failed"
        ):
            _cp_status, cp_job_meta = autocatpath_job
            cp_retries = int(
                (s.meta or {}).get("quest_autocatpath_infra_retries", 0) or 0
            )
            reaction = _quest_reaction_config(store, quest_id)
            if hub is None or reaction is None:
                notes.append(
                    f"autocatpath infra failure for {handle} "
                    "(retry-eligible, not ruled out)"
                )
            elif cp_retries < _MAX_INFRA_RETRIES:
                dispatch_autocatpath(store, s.id, reaction, hub=hub)
                store.stamp_ref_meta(
                    s.id, {"quest_autocatpath_infra_retries": cp_retries + 1}
                )
                notes.append(
                    f"autocatpath infra failure for {handle} → re-dispatched "
                    f"(retry {cp_retries + 1})"
                )
            elif cp_retries < _MAX_INFRA_RETRIES + 1:
                _file_infra_gripe(
                    store, quest_id, handle, cp_job_meta, hub=hub, lane="autocatpath"
                )
                store.stamp_ref_meta(
                    s.id, {"quest_autocatpath_infra_retries": _MAX_INFRA_RETRIES + 1}
                )
                notes.append(
                    f"autocatpath infra failure persists for {handle} → gripe filed"
                )
            else:
                # Already gripe-filed on a prior harvest — dedup, no re-file.
                notes.append(
                    f"autocatpath infra failure persists for {handle} (gripe already filed)"
                )
    return ComputeStep(
        candidates_created=0,
        sims_dispatched=0,
        results_harvested=harvested,
        ruled_out=ruled_out,
        notes=notes,
    )


def _quest_reaction_config(store: Store, quest_id: int) -> dict[str, Any] | None:
    """The reaction `R` a barrier quest evaluates every candidate against.

    Stored on the quest's ``meta.reaction_config`` (a parsed autocatpath config, e.g.
    ``{substrate: 'NO', target: 'NH3', network: 'ammonia'}`` for NO→NH₃ on Pd).
    Absent → the quest ranks on relax measures only (no barrier lane); present →
    each new candidate also gets a autocatpath evaluation.
    """
    refs = store.fetch_refs_by_ids({quest_id})
    ref = refs.get(quest_id)
    cfg = (ref.meta or {}).get("reaction_config") if ref is not None else None
    return cfg if isinstance(cfg, dict) and cfg else None


def _candidate_struct_ids(store: Store, quest_id: int) -> list[int]:
    """The `structure` candidates serving a quest — the barrier/relax targets.

    A quest's ``serves`` in-links mix candidate structures with linked papers,
    the dossier draft, coordinator todos, memories, etc.; only the structures
    are compute candidates, so both re-dispatch and reset filter to this set
    rather than acting on a paper (which would fail an autocatpath export).
    """
    ids = {
        int(link.src_ref_id)
        for link in store.links_for(quest_id, direction="in", relation="serves")
    }
    refs = store.fetch_refs_by_ids(ids)
    return [i for i in ids if (r := refs.get(i)) is not None and r.kind == "structure"]


def redispatch_candidates(
    store: Store,
    quest_id: int,
    *,
    hub: Any | None = None,
    include_ruled_out: bool = False,
) -> str:
    """Re-dispatch a autocatpath barrier eval for every candidate of a quest.

    The maintenance action behind P0: after an engine deploy bumps
    :func:`_autocatpath_engine_token`, each candidate's idem key changes, so this
    mints *fresh* ``autocatpath_explore`` jobs on the deployed engine instead of
    deduping onto stale ones. Idempotent per engine token — with an unchanged
    token every call collapses onto the in-flight/completed job, so re-running is
    safe. Ruled-out candidates are skipped by default (a dead geometry earns no
    more compute); pass ``include_ruled_out=True`` to also re-evaluate candidates
    whose rule-out was decided on now-suspect stale barriers.
    """
    hub = hub or _hub_for(store)
    reaction = _quest_reaction_config(store, quest_id)
    if reaction is None:
        return f"redispatch skipped: quest {quest_id} has no reaction_config"
    n = 0
    for sid in _candidate_struct_ids(store, quest_id):
        if not include_ruled_out and any(
            str(t).startswith("ruled-out:") for t in store.tags_for(sid)
        ):
            continue
        note = dispatch_autocatpath(store, sid, reaction, hub=hub)
        if note.startswith("autocatpath["):
            n += 1
    return f"re-dispatched {n} candidate(s) on the deployed engine"


#: Candidate-meta keys the barrier lane stamps. :func:`reset_compute` nulls them
#: so a stale (untrusted) barrier stops showing as an `(excluded)` frontier cell
#: while the deployed engine re-scores — the harvest re-stamps real values.
#: Deliberately EXCLUDES ``quest_autocatpath_harvested_upto``: the harvest bookmark
#: is left intact so the old, already-harvested stale jobs stay at/below it and are
#: not re-processed — only the fresh redispatch jobs (higher ref ids) are harvested.
#: Nulling it to 0 would make the next harvest re-read the stale completed job and
#: re-stamp the very barrier this reset just cleared.
_AUTOCATPATH_MEASURE_KEYS: tuple[str, ...] = (
    "barrier",
    "span",
    "barrier_trusted",
    "barrier_neb_failed",
    "barrier_desorbed",
    "barrier_wrong_site",
    "barrier_low_confidence",
)


def reset_compute(
    store: Store,
    quest_id: int,
    *,
    keep_dossier: bool = False,
) -> str:
    """Surgically wipe a quest's barrier-lane compute history for a clean
    re-run — WITHOUT discarding the candidate designs or their linked papers.

    The counterpart to :func:`redispatch_candidates` when an engine improvement
    invalidates not just the numbers but the *conclusions* drawn from them. For
    every candidate structure serving the quest it nulls the stamped barrier
    measures + quality flags (so the frontier shows an honest "awaiting" rather
    than a stale `(excluded)` cell — but keeps the harvest bookmark, so the old
    stale jobs aren't re-read and re-stamped; only the fresh redispatch jobs
    land), drops every ``ruled-out:*``
    tag (rule-outs decided on stale barriers must not survive), and drops the
    ``needs-experiment`` graduation tag (a milestone earned on an untrusted
    barrier). Quest-level: unless ``keep_dossier``, resets the dossier to a stub
    (the next tick regenerates it from clean data — otherwise the discovery agent
    keeps reasoning from its confabulated conclusions) and logs a ``decision``
    boundary entry. The relax ``energy`` (a separate lane) is left intact. Run
    :func:`redispatch_candidates` afterwards to re-score on the deployed engine.
    """
    from precis.quest.dossier import rewrite_dossier

    struct_ids = _candidate_struct_ids(store, quest_id)
    cleared_tags = 0
    for sid in struct_ids:
        store.stamp_ref_meta(sid, {k: None for k in _AUTOCATPATH_MEASURE_KEYS})
        for t in store.tags_for(sid):
            ts = str(t)
            if ts.startswith("ruled-out:") or ts == "needs-experiment":
                store.remove_tag(sid, Tag.open(ts))
                cleared_tags += 1
    if not keep_dossier:
        rewrite_dossier(
            store,
            quest_id,
            "# (dossier reset)\n\nPrior barriers were computed by a stale engine "
            "and invalidated; any conclusions built on them are void. Re-running "
            "on the deployed engine — this regenerates from the fresh, trusted "
            "results.\n",
        )
    append_entry(
        store,
        quest_id,
        text=(
            f"compute history reset for a clean re-run across {len(struct_ids)} "
            f"candidate(s): nulled barrier measures + dropped {cleared_tags} stale "
            "ruled-out/graduation tag(s); prior barriers were stale-engine and are "
            "invalidated. Next: `precis quest redispatch`."
        ),
        entry_type="decision",
        by=MEASURED_BY,
    )
    return (
        f"reset {len(struct_ids)} candidate(s): nulled measures + dropped "
        f"{cleared_tags} stale tag(s)"
        + ("" if keep_dossier else " + reset dossier")
        + f" — now run `precis quest redispatch {quest_id}`"
    )


def run_compute_step(
    store: Store,
    quest_id: int,
    proposals: list[dict[str, Any]],
    *,
    hub: Any | None = None,
    dispatch: bool = True,
    by: str = "agent",
) -> ComputeStep:
    """Turn a tick's proposals into candidates + sims, then harvest results.

    Each candidate gets a **relax** (the stability / formation-energy lane) and,
    when the quest declares a reaction (``meta.reaction_config``), a **autocatpath**
    evaluation (the barrier lane) — both on the same structure. They are
    independent measurements (autocatpath relaxes the injected slab internally), so
    they co-dispatch; no cross-tick sequencing is needed for first light.

    ``dispatch=False`` records candidates without minting compute (useful for a
    dry preview). Always harvests any already-finished sims at the end.
    """
    hub = hub or _hub_for(store)
    reaction = _quest_reaction_config(store, quest_id) if dispatch else None
    # A reaction quest's candidates are catalyst slabs — relax the box in-plane
    # (the a/b vectors, c-axis/vacuum pinned) so stability is judged on a
    # *relaxed* slab, not one strained by the bulk-derived lattice constant.
    relax_cell = "inplane" if reaction is not None else None
    created = dispatched = 0
    notes: list[str] = []
    for p in proposals or []:
        if not isinstance(p, dict):
            continue
        sid = ensure_candidate(store, quest_id, p, hub=hub)
        if sid is None:
            continue
        created += 1
        if dispatch:
            note = dispatch_relax(store, sid, hub=hub, cell=relax_cell)
            notes.append(note)
            if note.startswith("relax["):
                dispatched += 1
            if reaction is not None:
                cnote = dispatch_autocatpath(store, sid, reaction, hub=hub)
                notes.append(cnote)
                if cnote.startswith("autocatpath["):
                    dispatched += 1

    harvest = harvest_measures(store, quest_id, by=by, hub=hub, relax_cell=relax_cell)
    notes.extend(harvest.notes)

    # Graduate any frontier candidate that has crossed the quest's ceiling
    # (slice 4e) — a deed + a real-world-experiment gap for a human.
    from precis.quest.graduate import graduate_frontier

    graduated = graduate_frontier(store, quest_id, by=by)
    if graduated:
        notes.append(f"graduated {len(graduated)} candidate(s) → needs-experiment")

    return ComputeStep(
        candidates_created=created,
        sims_dispatched=dispatched,
        results_harvested=harvest.results_harvested,
        ruled_out=harvest.ruled_out,
        notes=notes,
        graduated=len(graduated),
    )


__all__ = [
    "ComputeStep",
    "dispatch_autocatpath",
    "dispatch_relax",
    "ensure_candidate",
    "harvest_measures",
    "redispatch_candidates",
    "reset_compute",
    "run_compute_step",
]
