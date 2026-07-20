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
import os
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from precis.quest.logbook import append_entry
from precis.store import Tag

if TYPE_CHECKING:
    from precis.store import Store

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


#: Env pin for the node that runs catpath (has the plugin + an ML backend). When
#: unset the job routes nowhere special and force-EMT keeps an in-process demo cheap.
_CATPATH_ROUTE_NODE_ENV = "PRECIS_CATPATH_ROUTE_NODE"


def _catpath_wall_seconds() -> int:
    """Expected wall-time hint (s) for a catpath NEB, stamped into the job's
    ``resources`` so the ssh_node lease outlives a full-network run.

    Env-tunable (``PRECIS_CATPATH_WALL_SECONDS``, default 5400 = 90 min): a
    3×3×4 full ammonia-network run is ~15-20 min uncontended but can stretch
    under load. ssh_node leases at ``max(2h floor, wall_seconds + 1h margin)``,
    so 5400 → a 2.5h lease.
    """
    try:
        n = int(os.environ.get("PRECIS_CATPATH_WALL_SECONDS", "5400"))
    except ValueError:
        return 5400
    return max(60, min(86_400, n))


def _catpath_content_key(config: dict[str, Any], slab_extxyz: str) -> str:
    """Stable idempotency key for a (reaction, exported slab) pair.

    Its own hash (not catpath's ``content_key``) so this stays precis-native — a
    re-dispatch of the same geometry + reaction collapses onto the in-flight job.
    """
    payload = _canonical_spec(config) + "\n" + slab_extxyz
    return hashlib.sha256(payload.encode()).hexdigest()


def dispatch_catpath(
    store: Store,
    structure_ref_id: int,
    config: dict[str, Any],
    *,
    hub: Any | None = None,
    force_backend: str | None = None,
) -> str:
    """Dispatch a catpath barrier evaluation on a candidate structure.

    Exports the candidate's (relaxed) geometry as extxyz, ensures a `pathway` ref
    for the write-back, and mints a ``catpath_explore`` job **pinned on the
    candidate** — so :func:`harvest_measures` finds it under the structure's
    compute lane (it queries ``parent_id = candidate``, unlike the standalone
    `pathway` handler which parents on the pathway ref). The job hydrates the
    extxyz into a prepared slab (catpath's injected-slab seam) and runs the
    reaction network on the routed node; on completion it emits a scalar
    ``barrier`` onto its own meta, which the harvest lifts onto the candidate.

    Precis-native (no catpath import — the `pathway` kind, if the plugin is
    installed, is reached only through the store) and **defensive**: degrades to a
    note on any error (missing plugin, unloadable scene) and never raises, so a
    compute hiccup can't fail the tick.
    """
    if not isinstance(config, dict) or not config:
        return f"catpath skipped: no reaction config for structure {structure_ref_id}"
    refs = store.fetch_refs_by_ids({structure_ref_id})
    ref = refs.get(structure_ref_id)
    if ref is None or ref.slug is None:
        return f"catpath skipped: structure {structure_ref_id} not found"
    hub = hub or _hub_for(store)

    # Export the candidate geometry — the injected-slab seam catpath consumes.
    try:
        from precis.structure import export

        scene, _handles = store.structure_load(structure_ref_id)
        # constraints=True → the slab's frozen bottom layers ride along as a
        # FixAtoms, so catpath's injected-slab relax/NEB keeps them fixed.
        slab_extxyz = export.to_extxyz(scene, constraints=True)
    except Exception as e:
        return f"catpath dispatch failed for {ref.slug}: export ({e})"

    node = os.environ.get(_CATPATH_ROUTE_NODE_ENV) or None
    # Routed → run the config's own backend on the pinned node; unrouted → EMT
    # (an in-process demo has no ML backend). An explicit override wins either way.
    force = force_backend or (None if node else "emt")
    # Routed nodes are the GPU boxes (topology: catpath → the CUDA node), so pin
    # the ML potential to cuda there — catpath's MLIPConfig.device defaults to
    # "cpu", which otherwise leaves the GPU idle and the NEB CPU-bound (~20×
    # slower). Copy the config so we neither mutate the caller's dict nor churn
    # the content key when unrouted; an explicit mlip.device wins (setdefault).
    run_config = config
    if node:
        run_config = {**config, "mlip": {**(config.get("mlip") or {})}}
        run_config["mlip"].setdefault("device", "cuda")
    key = _catpath_content_key(run_config, slab_extxyz)
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
        return f"catpath dispatch failed for {ref.slug}: pathway ref ({e})"

    # Mint the compute-lane job PINNED ON THE CANDIDATE (harvest queries parent_id).
    try:
        from precis.handlers.job import JobHandler

        JobHandler(hub=hub).put(
            job_type="catpath_explore",
            executor="ssh_node",
            parent_id=structure_ref_id,
            idem_key=f"catpath_explore:{key}",
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
                "resources": {"wall_seconds": _catpath_wall_seconds()},
            },
        )
    except Exception as e:
        return f"catpath dispatch failed for {ref.slug}: job mint ({e})"
    return f"catpath[{force or 'config'}] dispatched for {ref.slug} → pathway {pslug}"


#: Job-meta spellings that carry catpath's rate-limiting barrier (eV). The
#: `catpath_explore` job exposes a scalar summary so the quest can harvest it
#: without importing catpath or reading the (plugin-kind) `pathway` ref.
_CATPATH_BARRIER_KEYS: tuple[str, ...] = ("barrier", "rate_Ea", "rate_ea", "ea")
_CATPATH_SPAN_KEYS: tuple[str, ...] = ("span",)


def _num_measure(v: Any) -> float | None:
    """A numeric measure, or None (``bool`` is an ``int`` but never a measure)."""
    if isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return float(v)
    return None


def _catpath_measures_from_job(meta: dict[str, Any]) -> dict[str, float]:
    """Lift the scalar barrier/span from a completed `catpath_explore` job's meta.

    Reads a ``result`` sub-dict if present (the bridge's summary), else the meta
    top level. The presence of a numeric barrier IS the "done" signal — a
    still-running job carries none, so it is simply skipped.
    """
    src = meta.get("result") if isinstance(meta.get("result"), dict) else meta
    out: dict[str, float] = {}
    for k in _CATPATH_BARRIER_KEYS:
        v = _num_measure(src.get(k))
        if v is not None:
            out["barrier"] = v
            break
    for k in _CATPATH_SPAN_KEYS:
        v = _num_measure(src.get(k))
        if v is not None:
            out["span"] = v
            break
    return out


def _fresh_catpath_jobs(
    store: Store, structure_ref_id: int, upto: int
) -> list[tuple[int, dict[str, Any]]]:
    """Completed `catpath_explore` jobs under a candidate, newer than ``upto``.

    Returns ``(job_ref_id, meta)`` oldest-first so harvest is deterministic and
    the idempotency bookmark advances monotonically.
    """
    with store.pool.connection() as conn:
        rows = conn.execute(
            "SELECT j.ref_id, j.meta FROM refs j "
            "WHERE j.parent_id = %s AND j.kind = 'job' AND j.deleted_at IS NULL "
            "AND j.meta->>'job_type' = 'catpath_explore' AND j.ref_id > %s "
            "ORDER BY j.ref_id ASC",
            (structure_ref_id, upto),
        ).fetchall()
    return [(int(r[0]), dict(r[1] or {})) for r in rows]


def _link_pathway(store: Store, structure_ref_id: int, pathway_ref_id: int) -> None:
    """Wire the evaluating `pathway` into the quest graph (idempotent).

    The catpath bridge creates the pathway ref; we link the candidate structure
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


def _latest_relax_job_status(store: Store, structure_ref_id: int) -> str | None:
    with store.pool.connection() as conn:
        row = conn.execute(
            "SELECT t.value FROM refs j "
            "JOIN ref_tags rt ON rt.ref_id = j.ref_id "
            "JOIN tags t ON t.tag_id = rt.tag_id "
            "WHERE j.parent_id = %s AND j.kind = 'job' AND j.deleted_at IS NULL "
            "AND j.meta->>'job_type' = 'struct_relax' AND t.namespace = 'STATUS' "
            "ORDER BY j.ref_id DESC LIMIT 1",
            (structure_ref_id,),
        ).fetchone()
    return str(row[0]) if row else None


def _mark_harvested(store: Store, structure_ref_id: int, upto_run_id: int) -> None:
    with store.tx() as conn:
        conn.execute(
            "UPDATE refs SET meta = meta || jsonb_build_object("
            "'quest_harvested_upto', %s::int) WHERE ref_id = %s",
            (upto_run_id, structure_ref_id),
        )


def harvest_measures(store: Store, quest_id: int, *, by: str = "agent") -> ComputeStep:
    """Read finished sims back into the logbook + rule out failures.

    For each candidate `structure` serving the quest:

    * newly-converged **relax** runs become `result` logbook entries (energy + a
      step-count cost proxy), tracked idempotently by ``meta.quest_harvested_upto``;
    * completed **catpath** (`catpath_explore`) jobs contribute the rate-limiting
      **barrier** (and span): lifted onto the candidate's own ``meta`` (where the
      generalised frontier reads it), the evaluating pathway linked into the quest
      graph, logged as a `result`, tracked by ``meta.quest_catpath_harvested_upto``;
    * a candidate whose latest relax job **failed** gets a one-shot
      ``ruled-out:relax-failed`` tag + a `dead-end` entry so the proposer stops
      re-treading it.
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
                by=by,
                cost=float(r.get("n_steps") or 0),
            )
            harvested += 1
        if fresh:
            _mark_harvested(store, s.id, max(int(r.get("id", 0)) for r in fresh))

        # Harvest catpath barriers: a completed `catpath_explore` job under this
        # candidate carries the rate-limiting barrier; lift it onto the
        # candidate's own meta (where the generalised frontier reads it), link
        # the evaluating pathway into the quest graph, and log a result entry.
        cp_upto = int((s.meta or {}).get("quest_catpath_harvested_upto", 0) or 0)
        cp_jobs = _fresh_catpath_jobs(store, s.id, cp_upto)
        cp_seen = cp_upto
        for job_id, jmeta in cp_jobs:
            cp_seen = max(cp_seen, job_id)
            measures = _catpath_measures_from_job(jmeta)
            if not measures:
                continue  # not finished (no scalar barrier yet)
            store.stamp_ref_meta(s.id, measures)
            pathway_ref = jmeta.get("pathway_ref")
            if isinstance(pathway_ref, int) and not isinstance(pathway_ref, bool):
                _link_pathway(store, s.id, pathway_ref)
            b = measures.get("barrier")
            b_s = f"barrier={b:g} eV" if isinstance(b, (int, float)) else "measured"
            append_entry(
                store,
                quest_id,
                text=f"catpath result for {handle} ({name}): {b_s}",
                entry_type="result",
                by=by,
            )
            harvested += 1
        if cp_seen > cp_upto:
            store.stamp_ref_meta(s.id, {"quest_catpath_harvested_upto": cp_seen})

        # Rule out a candidate whose relax job failed (once).
        already_out = any(str(t).startswith("ruled-out:") for t in store.tags_for(s.id))
        if not already_out and _latest_relax_job_status(store, s.id) == "failed":
            store.add_tag(s.id, Tag.open("ruled-out:relax-failed"), set_by="system")
            append_entry(
                store,
                quest_id,
                text=f"ruled out {handle} ({name}): relax failed to converge",
                entry_type="dead-end",
                by=by,
            )
            ruled_out += 1
            notes.append(f"ruled-out {handle}")
    return ComputeStep(
        candidates_created=0,
        sims_dispatched=0,
        results_harvested=harvested,
        ruled_out=ruled_out,
        notes=notes,
    )


def _quest_reaction_config(store: Store, quest_id: int) -> dict[str, Any] | None:
    """The reaction `R` a barrier quest evaluates every candidate against.

    Stored on the quest's ``meta.reaction_config`` (a parsed catpath config, e.g.
    ``{substrate: 'NO', target: 'NH3', network: 'ammonia'}`` for NO→NH₃ on Pd).
    Absent → the quest ranks on relax measures only (no barrier lane); present →
    each new candidate also gets a catpath evaluation.
    """
    refs = store.fetch_refs_by_ids({quest_id})
    ref = refs.get(quest_id)
    cfg = (ref.meta or {}).get("reaction_config") if ref is not None else None
    return cfg if isinstance(cfg, dict) and cfg else None


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
    when the quest declares a reaction (``meta.reaction_config``), a **catpath**
    evaluation (the barrier lane) — both on the same structure. They are
    independent measurements (catpath relaxes the injected slab internally), so
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
                cnote = dispatch_catpath(store, sid, reaction, hub=hub)
                notes.append(cnote)
                if cnote.startswith("catpath["):
                    dispatched += 1

    harvest = harvest_measures(store, quest_id, by=by)
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
    "dispatch_catpath",
    "dispatch_relax",
    "ensure_candidate",
    "harvest_measures",
    "run_compute_step",
]
