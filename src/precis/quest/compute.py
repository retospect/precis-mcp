"""Quest compute dispatch — candidates become `structure` sims (slice 4b).

The local grind of the autonomous loop: a tick proposes candidate materials,
each becomes a `structure` that ``serves`` the quest (the graph *is* the memory
of explored space), we dispatch its relax on the GPU node (the intent-vs-compute job lanes derived
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

import functools
import hashlib
import importlib.metadata
import json
import logging
import os
import re
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

#: The tier ladder (docs/proposals — catpath ``search.screening``/``template``
#: bridge): **screening** (relax-only, ``template=parked``, catpath emits no
#: barrier scalar — cheap thermodynamic ranking) → **neb** (full NEB over the
#: coadsorbed template — catpath >= 0.10 resolves an unset ``template`` to
#: ``"coadsorbed"`` for ammonia — pruned by the fast-screening NEB stack) →
#: **verify** (the same coadsorbed network, exhaustive: no best_first
#: pruning, every step re-refined). The two NEB tiers differ by search
#: rigor, not network topology. Fidelity is monotonic in this order —
#: :data:`_TIER_FIDELITY` is the single source of truth other modules
#: (:mod:`precis.quest.graduate`) compare against.
_TIER_SCREENING = "screening"
_TIER_NEB = "neb"
_TIER_VERIFY = "verify"
_TIERS: tuple[str, ...] = (_TIER_SCREENING, _TIER_NEB, _TIER_VERIFY)
_TIER_FIDELITY: dict[str, int] = {_TIER_SCREENING: 0, _TIER_NEB: 1, _TIER_VERIFY: 2}

#: Quest-meta promotion caps (human-set at seed time — see
#: :func:`precis.quest.catalyst_seed.seed_catalyst_quest` — never written by
#: the tick/LLM loop, same convention as ``rubric_composite``).
_DEFAULT_TIER_PROMOTE_NEB = 2
_DEFAULT_TIER_PROMOTE_VERIFY = 3


def _apply_tier_config(config: dict[str, Any], tier: str) -> dict[str, Any]:
    """Overlay a ladder ``tier`` onto a catpath reaction config.

    * ``screening`` — relax-only ranking: ``search.screening=True`` +
      ``template="parked"``. catpath's own ``results.json`` then carries no
      barrier scalar at all (never special-cased here — the harvest side
      just sees an empty/thermo-only summary and lets that flow, see
      :func:`_autocatpath_measures_from_job`).
    * ``neb`` — the straight-to-NEB refine tier, overlaid with autocatpath's
      fast-screening NEB stack (three ``search`` knobs, each a *default* an
      explicit caller key overrides; a caller-pinned ``neb_schedule`` suppresses
      the whole overlay, preserving the "hand-tuned NEB config wins wholesale"
      contract). ``template`` is left unset: catpath >= 0.10 resolves that to
      ``"coadsorbed"`` for ammonia (fragment parking is explicit opt-in only),
      so this tier already measures the full network — N2/N2O coupling and the
      NH2OH branch included — and pinning it here would only churn the content
      key:

      - ``neb_schedule="best_first"`` (0.7): relax every endpoint first, then
        run NEBs frontier-first on the lowest-optimistic-span route and prune
        any route whose optimistic span (a thermodynamic *lower bound* on its
        true TS) can't come within ``neb_margin`` of the best refined span.
        Pruning is provably safe — skips work, never hides a competitive route
        — buying back the NEB cost of far-uphill side-product forks.
      - ``neb_optimizer="neb-ode"`` (0.8): ASE's adaptive NEBOptimizer in place
        of dense-Hessian BFGS — benchmarked HERE on Pd(111)+N* at ~5× fewer
        MLIP evals for the same barrier in the screening regime (the per-seed
        cost lever; docs/backlog/autocatpath-seed-wall-overruns.md).
      - ``neb_batched=True`` (0.9, MACE-dtype fix 0.9.1, tether-guard fix
        0.11): one MLIP forward per step over all interior images instead of a
        serial loop — GPU-utilisation on a batch-capable backend, physics-
        identical with a runtime self-check that degrades to serial on any
        mismatch. Composes with ``neb-ode`` on the single-band path (unlike the
        inter-band ``neb_pool_size``, which the pipeline doesn't feed yet).

      Together these target the seed-wall overrun: fewer edges NEB'd
      (best_first), fewer evals per edge (neb-ode), and each eval GPU-saturated
      (batched).
    * ``verify`` — the same NEB search over the same coadsorbed network
      (``template="coadsorbed"`` pinned explicitly, version-proof) but left
      exhaustive (no best_first overlay): the authoritative final pass
      re-refines every step, removing best_first's honest-absence caveats on
      the winning candidate. Verify differs from neb by search rigor only.

    The overlay folds into :func:`_autocatpath_content_key` automatically
    (the changed dict), so each tier of the same candidate+reaction content-
    addresses onto its own job/pathway — no separate idem-key plumbing
    needed.
    """
    if tier == _TIER_SCREENING:
        cfg = {**config, "search": {**(config.get("search") or {}), "screening": True}}
        cfg["template"] = "parked"
        return cfg
    if tier == _TIER_VERIFY:
        return {**config, "template": "coadsorbed"}
    # neb (or an unrecognized tier): the fast-screening NEB stack (best_first
    # scheduling + neb-ode optimizer + intra-band batching) unless the caller
    # pinned a schedule explicitly (then the whole hand-tuned NEB config wins).
    search = config.get("search") or {}
    if "neb_schedule" in search:
        return config
    return {
        **config,
        "search": {
            **search,
            "neb_schedule": "best_first",
            "neb_optimizer": "neb-ode",
            "neb_batched": True,
        },
    }


@dataclass(frozen=True)
class ComputeStep:
    candidates_created: int
    sims_dispatched: int
    results_harvested: int
    ruled_out: int
    notes: list[str]
    graduated: int = 0
    #: How many of this step's proposals hashed onto an ALREADY-EXISTING
    #: candidate (content-addressed dedup) — see :func:`_ensure_candidate_detail`.
    #: Each dup also gets a one-line ``observation`` in the quest logbook (so
    #: the proposer sees the miss next tick via :func:`precis.quest.tick._logbook_tail`)
    #: in addition to this count.
    duplicate_proposals: int = 0


def _canonical_spec(spec: dict[str, Any]) -> str:
    return json.dumps(spec, sort_keys=True, separators=(",", ":"))


def _candidate_slug(quest_id: int, spec: dict[str, Any]) -> str:
    """Content-addressed slug: the same material spec → the same structure."""
    digest = hashlib.sha256(_canonical_spec(spec).encode()).hexdigest()[:10]
    return f"q{quest_id}cand-{digest}"


def _hub_for(store: Store) -> Any:
    from precis.dispatch import Hub

    return Hub(store=store)


def _geom_hash(scene: Any) -> str:
    """Canonical geometry hash — species + rounded scaled (fractional)
    positions, sorted, ``sha256[:12]`` (Slice 4c, candidate dedup). Two
    candidates that resolve to the same atoms in the same positions hash
    identically regardless of proposal name/spec-formatting, so
    :func:`precis.quest.frontier._flag_geom_duplicates` can flag a proposer
    re-discovering an existing material under a new name.

    Reads the precis-native :class:`~precis.structure.scene.Scene` (already
    loaded via ``store.structure_load`` at candidate-creation time) rather than
    materialising ASE ``Atoms`` — the candidate spec is built into a ``Scene``
    by :class:`~precis.handlers.structure.StructureHandler`'s own ``put`` (via
    ``precis.structure.ops.apply_ops``), so this needs no extra dependency and
    no second materialisation.
    """
    rows = sorted(
        (a.element, tuple(round(float(x), 3) for x in a.frac))
        for a in scene.atoms.values()
    )
    payload = json.dumps(rows, separators=(",", ":"))
    return hashlib.sha256(payload.encode()).hexdigest()[:12]


def _dup_status_summary(store: Store, existing: Any) -> str:
    """One-line status for a duplicate-proposal note: a ``ruled-out:*`` tag,
    else an already-harvested barrier, else ``pending`` (no verdict yet)."""
    ruled = next(
        (
            str(t)
            for t in store.tags_for(existing.id)
            if str(t).startswith("ruled-out:")
        ),
        None,
    )
    if ruled is not None:
        return ruled
    meta = existing.meta or {}
    barrier = meta.get("barrier")
    if isinstance(barrier, (int, float)) and not isinstance(barrier, bool):
        return f"evaluated barrier={barrier:g}"
    return "pending"


def _note_duplicate_proposal(
    store: Store, quest_id: int, proposal: dict[str, Any], slug: str, existing: Any
) -> None:
    """Log a `duplicate proposal` observation so the proposer sees the miss
    next tick (:func:`precis.quest.tick._logbook_tail` feeds the tick prompt) —
    today's silent cache-hit gives the model no signal that it re-proposed
    something already tried."""
    name = str(proposal.get("name") or slug)
    status = _dup_status_summary(store, existing)
    append_entry(
        store,
        quest_id,
        text=f"duplicate proposal: {name} is already candidate {slug} (status: {status})",
        entry_type="observation",
        by=MEASURED_BY,
    )


def _resolve_parent_structure(
    store: Store, quest_id: int, parent_ref: Any
) -> Any | None:
    """Resolve a proposal's optional ``parent`` field to a live candidate
    `structure` ref of THIS quest, or ``None`` when unresolvable.

    ``parent_ref`` is normally the parent candidate's own content-addressed
    slug (``q<quest_id>cand-<digest>``, what :func:`_candidate_slug` mints —
    the same string the proposer sees echoed back in prior ticks), but a bare
    ref id or a universal handle (e.g. ``st5``) also resolves. A structure
    outside this quest's own candidate set does not count as lineage.
    """
    ident = str(parent_ref or "").strip()
    if not ident:
        return None
    ref = None
    if ident.lstrip("-").isdigit():
        ref = store.get_ref(kind="structure", id=int(ident))
    if ref is None:
        ref = store.get_ref(kind="structure", id=ident)
    if ref is None:
        from precis.utils.mentions import resolve_handle_target

        target = resolve_handle_target(store, ident)
        if target is not None:
            cand = store.fetch_refs_by_ids({target.dst_ref_id}).get(target.dst_ref_id)
            if cand is not None and cand.kind == "structure":
                ref = cand
    if ref is None or ref.kind != "structure":
        return None
    servers = store.links_for(quest_id, direction="in", relation="serves")
    if ref.id not in {ln.src_ref_id for ln in servers}:
        return None
    return ref


def _link_parent_if_present(
    store: Store, quest_id: int, proposal: dict[str, Any], child_ref_id: int
) -> str | None:
    """Wire the optional ``parent`` field (a proposal that *varies* an
    existing candidate) as a ``derived-from`` link, child → parent — the same
    relation :meth:`precis.handlers.structure.StructureHandler.derive` uses,
    so the frontier tree (:func:`precis.quest.frontier.render_frontier_tree`)
    reads one lineage vocabulary regardless of how the edge was made.

    Returns a one-line note when ``parent`` is present but unresolvable (never
    raises, never fails the proposal — a lineage miss is just weaker context
    for the frontier tree); ``None`` otherwise (absent, self-referential, or
    successfully linked).
    """
    parent_ref = proposal.get("parent")
    if not parent_ref:
        return None
    parent = _resolve_parent_structure(store, quest_id, parent_ref)
    if parent is None:
        return (
            f"lineage skipped: parent {parent_ref!r} does not resolve to a live "
            "candidate of this quest"
        )
    if int(parent.id) == int(child_ref_id):
        return None  # self-reference — silently skip, not an error
    try:
        store.add_link(
            src_ref_id=child_ref_id, dst_ref_id=parent.id, relation="derived-from"
        )
    except Exception as e:
        return f"lineage link failed for parent {parent_ref!r}: {e}"
    return None


def _ensure_candidate_detail(
    store: Store, quest_id: int, proposal: dict[str, Any], *, hub: Any | None = None
) -> tuple[int | None, bool, str | None]:
    """:func:`ensure_candidate`'s full internals — ``(ref_id, was_duplicate,
    note)``. Split out so :func:`run_compute_step` can count dups + surface
    lineage notes without changing :func:`ensure_candidate`'s public
    ``int | None`` contract (existing direct callers/tests rely on it).
    """
    spec = proposal.get("structure")
    if not isinstance(spec, dict):
        return None, False, None
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
        return None, False, None
    slug = _candidate_slug(quest_id, spec)
    existing = store.get_ref(kind="structure", id=slug)
    if existing is not None:
        _note_duplicate_proposal(store, quest_id, proposal, slug, existing)
        note = _link_parent_if_present(store, quest_id, proposal, int(existing.id))
        return int(existing.id), True, note

    hub = hub or _hub_for(store)
    from precis.handlers.structure import StructureHandler

    name = str(proposal.get("name") or slug)
    try:
        StructureHandler(hub=hub).put(id=slug, text=json.dumps(spec), title=name)
    except Exception:
        log.warning(
            "ensure_candidate: StructureHandler.put raised for quest %s slug %s "
            "— candidate not created",
            quest_id,
            slug,
            exc_info=True,
        )
        return None, False, None
    ref = store.get_ref(kind="structure", id=slug)
    if ref is None:  # pragma: no cover - insert_ref cite_key reclaim prevents this
        # put reported success but the slug still won't resolve to a live ref.
        # The cite_key reclaim in insert_ref (gr201814) closes the known path
        # here; reaching this now means a *different* orphaning bug. Never
        # silent — an uncounted candidate is exactly the invisible stall
        # gr201814 chased. Warn loudly and skip (don't raise: one bad candidate
        # must not discard the whole compute step's harvest).
        log.warning(
            "ensure_candidate: put succeeded but slug %s did not resolve to a "
            "live structure for quest %s — candidate orphaned, skipping wire",
            slug,
            quest_id,
        )
        return None, False, None
    with store.tx() as conn:
        store.add_link(
            src_ref_id=ref.id, dst_ref_id=quest_id, relation="serves", conn=conn
        )
        store.add_tag(ref.id, Tag.open(_CANDIDATE_TAG), set_by="system", conn=conn)
    try:
        scene, _handles = store.structure_load(ref.id)
        store.stamp_ref_meta(ref.id, {"geom_hash": _geom_hash(scene)})
    except Exception:
        log.debug(
            "ensure_candidate: geom_hash stamp failed for %s", slug, exc_info=True
        )
    note = _link_parent_if_present(store, quest_id, proposal, int(ref.id))
    return int(ref.id), False, note


def ensure_candidate(
    store: Store, quest_id: int, proposal: dict[str, Any], *, hub: Any | None = None
) -> int | None:
    """Create (or reuse) the `structure` server for a proposal's spec.

    Returns the structure ref id, or ``None`` when the proposal carries no
    usable structure spec (nothing to simulate). Content-addressed: a repeat
    proposal of the same material returns the existing structure (and logs a
    `duplicate proposal` observation — :func:`_note_duplicate_proposal`). An
    optional ``proposal['parent']`` (the slug/handle of a candidate this one
    varies) is wired as a ``derived-from`` lineage link
    (:func:`_link_parent_if_present`). See :func:`_ensure_candidate_detail`
    for the dup-count / lineage-note detail this wrapper discards.
    """
    return _ensure_candidate_detail(store, quest_id, proposal, hub=hub)[0]


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
#: 0.4.0, which relaxes the same geometries cleanly (0 detached, trusted).
#: The token is DERIVED from the ``autocatpath`` floor pin in precis's own
#: dist metadata (:func:`_autocatpath_pinned_version`), because every engine
#: adoption already bumps that pin in the same commit — one lever, no
#: separate hand-bump to forget (the 0.4.0/0.6.0/0.7.0 adoptions each had to
#: remember this constant; see the shipped
#: autocatpath-060-selectivity-objectives proposal, git history). The env
#: var stays
#: as an ops escape hatch (force a re-key without a ship); the constant is a
#: last-resort fallback for a venv with no precis dist metadata. Remaining
#: caveat (unchanged from before): the token tracks the *pin*, not what
#: spark's GPU venv actually runs — an engine release adopted without a pin
#: bump still dedup-pins stale jobs, so keep bumping the pin with every
#: adoption.
_AUTOCATPATH_VERSION_ENV = "PRECIS_AUTOCATPATH_VERSION"
#: Kept in sync with the pyproject ``autocatpath`` pin floor so the
#: metadata-less fallback re-keys with the primary derivation; the pin bump is
#: the real re-key lever (:func:`_autocatpath_pinned_version` wins whenever
#: dist metadata exists, which it always does on a dispatch host).
_AUTOCATPATH_CACHE_EPOCH = "0.12.1"

#: Precis-side summary-contract revision folded into the idem keys
#: alongside the engine token: a completed job's reusable artifact
#: includes the scalar summary stamped by _dispatch_common.summarize, so
#: when THAT contract changes (new/renamed measure keys) completed jobs
#: are stale for harvest even on the same engine. Bump on any
#: summarize() output-schema change. s2 = engine-scorecard margins
#: (selectivity_margin/trap_margin/poison_margin off results.score)
#: replacing the 0.5.2 span-based lifts.
_AUTOCATPATH_SUMMARY_REV = "s2"

#: Matches the ``autocatpath`` requirement (any extras) in dist metadata,
#: e.g. ``autocatpath>=0.7; extra == "catalyst"`` / ``autocatpath[mace]>=0.7``.
_AUTOCATPATH_REQ_RE = re.compile(r"^autocatpath\s*(?:\[[^]]*\])?\s*[<>=!~(]")


@functools.cache
def _autocatpath_pinned_version() -> str | None:
    """Floor of the ``autocatpath`` pin in the installed ``precis-mcp`` dist
    metadata, normalized to three components (``0.7`` → ``0.7.0`` — keeps the
    derived token byte-identical to the hand-bumped epochs it replaces, so
    adopting this derivation re-keys nothing). Extras-marked requirements are
    listed in metadata regardless of which extras a venv installed, so this
    works on dispatch hosts that never install the engine itself (compute
    keeps its no-``autocatpath``-import rule). ``None`` when precis isn't
    installed as a dist or carries no ``>=`` pin."""
    try:
        reqs = importlib.metadata.requires("precis-mcp") or []
    except importlib.metadata.PackageNotFoundError:
        return None
    floors: list[str] = []
    for req in reqs:
        if not _AUTOCATPATH_REQ_RE.match(req):
            continue
        m = re.search(r">=\s*([0-9][0-9A-Za-z.]*)", req.split(";", 1)[0])
        if not m:
            continue
        parts = m.group(1).split(".")
        while len(parts) < 3:
            parts.append("0")
        floors.append(".".join(parts))
    if not floors:
        return None

    # The pin appears once per extra (catalyst, catalyst-gpu); if they ever
    # diverge, the HIGHEST floor is the one that must re-key (under-keying is
    # the dedup-pin trap this whole derivation exists to close).
    def _floor_key(v: str) -> tuple[int, ...]:
        out = []
        for p in v.split("."):
            d = re.match(r"\d+", p)
            out.append(int(d.group()) if d else 0)
        return tuple(out)

    return max(floors, key=_floor_key)


def _autocatpath_engine_token() -> str:
    """Engine-version component of the idem key: ``PRECIS_AUTOCATPATH_VERSION``
    env override if set, else the pin-derived version, else the code-constant
    fallback. A change in this value re-keys every (config, slab) pair, so a
    new engine build re-evaluates candidates instead of reusing stale results."""
    return (
        os.environ.get(_AUTOCATPATH_VERSION_ENV)
        or _autocatpath_pinned_version()
        or _AUTOCATPATH_CACHE_EPOCH
    )


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
        + "+"
        + _AUTOCATPATH_SUMMARY_REV
        + "\n"
        + _canonical_spec(config)
        + "\n"
        + slab_extxyz
    )
    return hashlib.sha256(payload.encode()).hexdigest()


def _autocatpath_seed_content_key(
    config: dict[str, Any], slab_extxyz: str, seed: int, model_index: int
) -> str:
    """Idem key for ONE ``(model, seed)`` fan-out unit (§B-1, gr180096): the
    same (engine, reaction, exported slab) triple :func:`_autocatpath_content_key`
    hashes, plus which seed / ``mlip.specs()`` entry. The engine-version fold
    is the same standing fix as the base key — MUST stay in the payload so a
    redeployed autocatpath re-keys every seed instead of dedup-pinning a
    stale partial (the qu164903 trap this whole module's docstring warns
    about, now scoped per-seed).
    """
    payload = (
        _autocatpath_engine_token()
        + "+"
        + _AUTOCATPATH_SUMMARY_REV
        + "\n"
        + _canonical_spec(config)
        + "\n"
        + slab_extxyz
        + f"\nseed={seed}\nmodel_index={model_index}"
    )
    return hashlib.sha256(payload.encode()).hexdigest()


#: Precis-native mirror of autocatpath's ``SearchConfig.seeds`` default
#: (``[0, 1, 2]``) — used only when a config omits ``search.seeds``. Compute
#: dispatch stays "no autocatpath import" (see :func:`dispatch_autocatpath`'s
#: docstring): this module never imports the ``autocatpath`` package, so the
#: fan-out shape is read straight off the plain config dict. Keep in sync
#: with ``autocatpath.config.SearchConfig``.
_AUTOCATPATH_DEFAULT_SEEDS: tuple[int, ...] = (0, 1, 2)


def _autocatpath_mlip_specs(config: dict[str, Any]) -> list[tuple[str, str | None]]:
    """Precis-native mirror of ``autocatpath.config.MLIPConfig.specs()`` —
    the ``(backend, model)`` pairs a run covers. Multi-model
    (``mlip.models``) splits into one spec per entry (``"backend:model"`` or
    a bare model name against the top-level backend); otherwise it's the
    single ``(backend, model)`` pair. Read straight off the plain config
    dict rather than importing ``autocatpath.config.Config`` — see
    :func:`dispatch_autocatpath`'s docstring for why. Keep in sync with
    ``MLIPConfig.specs()``.
    """
    mlip = config.get("mlip") or {}
    backend = str(mlip.get("backend") or "emt")
    models = mlip.get("models") or []
    if models:
        out: list[tuple[str, str | None]] = []
        for m in models:
            m = str(m)
            if ":" in m:
                b, mm = m.split(":", 1)
                out.append((b, mm or None))
            else:
                out.append((backend, m))
        return out
    return [(backend, mlip.get("model"))]


def _autocatpath_search_seeds(config: dict[str, Any]) -> list[int]:
    """Precis-native mirror of ``autocatpath.config.SearchConfig.seeds``
    default. See :func:`_autocatpath_mlip_specs` for why this reads the
    plain dict rather than importing autocatpath."""
    seeds = (config.get("search") or {}).get("seeds")
    if not seeds:
        return list(_AUTOCATPATH_DEFAULT_SEEDS)
    return [int(s) for s in seeds]


def _find_child_todo_by_content_key(
    store: Store, parent_id: int, content_key: str
) -> int | None:
    """A live ``kind='todo'`` child of ``parent_id`` whose
    ``meta.content_key`` matches — the content-addressing seam
    :func:`dispatch_autocatpath`'s tree mint uses so a re-dispatch reuses the
    existing aggregate / per-seed todo instead of minting a duplicate
    (regardless of that todo's current status — open, doing, or done)."""
    with store.pool.connection() as conn:
        row = conn.execute(
            "SELECT ref_id FROM refs WHERE kind = 'todo' AND parent_id = %s "
            "AND deleted_at IS NULL AND meta->>'content_key' = %s "
            "ORDER BY ref_id LIMIT 1",
            (parent_id, content_key),
        ).fetchone()
    return int(row[0]) if row else None


def _ensure_autocatpath_todo(
    store: Store,
    *,
    parent_id: int,
    content_key: str,
    title: str,
    meta: dict[str, Any],
) -> int:
    """Content-addressed ``get-or-create`` for one node of the autocatpath
    fan-out tree (the aggregate todo, or one per-seed todo under it).

    Reused (not re-minted) on a repeat :func:`dispatch_autocatpath` call for
    the same ``(parent, content_key)`` — this is the "retry skips completed
    seeds" contract: a seed whose todo already exists, in ANY state
    (queued/running/done), is left alone rather than duplicated.

    Uses ``store.insert_ref`` directly rather than ``TodoHandler.put`` —
    same reason the pathway ref just above is a raw ``insert_ref``: this
    tree's parent is the candidate `structure` ref (compute-lane),
    and ``TodoHandler.put``'s ``check_parent_exists`` guard only accepts
    another ``todo`` as a NEW todo's parent (the human-facing intent tree's
    invariant) — these nodes are internal compute-lane machinery, not part
    of that tree, so they bypass the handler layer the same way the
    `pathway` ref does.
    """
    existing = _find_child_todo_by_content_key(store, parent_id, content_key)
    if existing is not None:
        return existing
    with store.tx() as conn:
        ref = store.insert_ref(
            kind="todo",
            slug=None,
            title=title,
            meta={**meta, "content_key": content_key},
            parent_id=parent_id,
            conn=conn,
        )
        store.add_tag(ref.id, Tag.open("ephemeral"), set_by="system", conn=conn)
    return int(ref.id)


def _seed_todo_handled(store: Store, seed_todo_id: int) -> bool:
    """Does the seed leaf at ``seed_todo_id`` need no fresh ``autocatpath_seed``
    job — i.e. is it *handled* rather than *stuck*?

    True iff either:

    * the todo's own ``STATUS`` is terminal-closed (``done`` / ``won't-do``);
      or
    * it already has a live ``kind='job'`` child whose ``STATUS`` is anything
      but ``failed``/``cancelled`` — i.e. ``succeeded``, or still non-terminal
      (``queued``/``running``/…).

    This is the exact dual of :mod:`precis.workers.auto_check_evaluators.
    child_job_succeeded`'s own resolution predicate ("any child job
    ``STATUS:succeeded`` and no live sibling todo"), deliberately: a seed
    whose job just succeeded but whose auto_check pass hasn't yet flipped the
    todo to ``done`` (auto_check runs one dispatch tick behind — see that
    evaluator's guard 2) still reads as *handled* here via the job's own
    STATUS, never re-minted out from under the pending flip.

    Only a seed with **no** job at all (a todo minted by a prior dispatch that
    crashed before ``jobs.put`` landed), or one whose only job(s) are
    ``failed``/``cancelled``, comes back ``False`` — :func:`dispatch_autocatpath`'s
    fan-out then falls through to :func:`_ensure_autocatpath_todo` (a
    get-or-create — reuses this SAME todo, never mints a duplicate) and a
    fresh ``jobs.put``. The re-mint is safe even if this predicate and the
    idem-key check below ever raced: ``JobHandler._lookup_idem``
    (``handlers/job.py``) independently treats terminal statuses as
    non-blocking, so a fresh ``jobs.put`` with the same ``idem_key`` mints a
    new job under the existing todo rather than either erroring or silently
    no-op'ing — this predicate decides *whether* to call ``jobs.put``, idem
    backstops what happens if it's called anyway.
    """
    with store.pool.connection() as conn:
        row = conn.execute(
            """
            SELECT
              COALESCE(
                (SELECT t.value FROM ref_tags rt JOIN tags t ON t.tag_id = rt.tag_id
                  WHERE rt.ref_id = st.ref_id AND t.namespace = 'STATUS' LIMIT 1),
                'open'
              ) IN ('done', 'won''t-do')
              OR EXISTS (
                    SELECT 1 FROM refs j
                      JOIN ref_tags rt2 ON rt2.ref_id = j.ref_id
                      JOIN tags t2 ON t2.tag_id = rt2.tag_id
                     WHERE j.parent_id = st.ref_id AND j.kind = 'job'
                       AND j.deleted_at IS NULL
                       AND t2.namespace = 'STATUS'
                       AND t2.value NOT IN ('failed', 'cancelled')
                  )
            FROM refs st WHERE st.ref_id = %(sid)s
            """,
            {"sid": seed_todo_id},
        ).fetchone()
    # A missing todo (shouldn't happen — the caller only calls this with an
    # id it just found via _find_child_todo_by_content_key) reads as handled
    # rather than stuck: fail toward "leave it alone", not toward re-minting.
    return bool(row[0]) if row else True


def dispatch_autocatpath(
    store: Store,
    structure_ref_id: int,
    config: dict[str, Any],
    *,
    hub: Any | None = None,
    force_backend: str | None = None,
    tier: str = _TIER_NEB,
) -> str:
    """Dispatch a autocatpath barrier evaluation on a candidate structure.

    ``tier`` (default ``"neb"`` — today's straight-to-NEB shape, byte-
    identical to before the ladder existed) selects which rung of the
    **screening → neb → verify** ladder this run is:
    :func:`_apply_tier_config` overlays the tier onto ``config`` BEFORE the
    device/route logic below, so the tier folds into the content-addressed
    idem key automatically — a promotion (a fresh tier on an
    already-dispatched candidate) is just another :func:`dispatch_autocatpath`
    call, content-addressed onto its own job/pathway rather than clobbering
    the prior tier's. The pathway ref this call ensures/reuses is stamped
    ``meta.tier`` at creation (read back by :func:`_find_tier_pathway`, the
    promotion-eligibility + `refines`-lineage seam).

    §B-1 (gr180096, the spark wedge fix): exports the candidate's (relaxed)
    geometry as extxyz, ensures a `pathway` ref for the write-back, then
    mints a **job tree** pinned on the candidate instead of one monolith
    job — the whole network x N seeds x full NEB used to run as ONE
    ~90-min in-process ``autocatpath_explore`` job that overran its lease
    and was SIGTERM-deaf. Now:

    ```
    structure (candidate)
      └─ T_agg  (todo, meta.executor=ssh_node, job_type=autocatpath_aggregate)
           ├─ T_seed_0  (todo, meta.auto_check=child_job_succeeded)
           │    └─ job  (autocatpath_seed, seed=0, model_index=0)
           ├─ T_seed_1  (todo, ...)
           │    └─ job  (autocatpath_seed, seed=1, model_index=0)
           ├─ ...  one per (model_index, seed) in cfg.mlip.specs() x cfg.search.seeds
           └─ (once every T_seed_* is STATUS:done) → the dispatch worker
              mints T_agg's own job (autocatpath_aggregate) under T_agg.
    ```

    Each ``T_seed_*``'s own job is minted HERE, synchronously, content-
    addressed on ``sha(run_config, slab_extxyz, seed, model_index,
    autocatpath_version)`` (:func:`_autocatpath_seed_content_key` — the
    version MUST be in the key, same standing fix as
    :func:`_autocatpath_content_key`) — so a re-dispatch (retry) reuses any
    seed todo that already exists rather than duplicating it, and a killed
    seed only loses that seed's own compute. Reuse is **status-aware**
    (:func:`_seed_todo_handled`), not blanket: a seed that's ``done``,
    ``won't-do``, or already carries a succeeded/live job is left alone; one
    whose only job(s) infra-failed (``failed``/``cancelled`` — a autocatpath
    barrier crash is never a physical verdict, see :func:`_latest_autocatpath_job`'s
    docstring) gets a fresh job minted under the SAME todo instead of wedging
    the whole candidate behind a dead seed forever. This is the automatic,
    bounded repair path :func:`_stuck_seed_failure` + ``harvest_measures``'s
    seed-lane ladder rides.

    ``T_agg`` deliberately carries NO job of its own yet — its
    ``meta.executor``/``job_type``/``params`` are set, but minting is left
    to the **existing** dispatch worker, whose ordinary candidate query
    already excludes a parent todo with a live (non-done) child todo. So
    ``T_agg`` only becomes dispatchable once every seed todo under it
    resolves via the **existing** ``child_job_succeeded`` auto_check
    evaluator — no new coordinator, no bespoke wait/yield state machine.
    The two-level nesting (seed job -> seed todo -> T_agg, not seed job ->
    T_agg directly) is load-bearing: a bare seed job as T_agg's direct
    child would satisfy ``child_job_succeeded`` on the FIRST seed's
    success, not the aggregate's own (the gpu-priority seed-chunking
    design). See ``docs/backlog/autocatpath-integration.md`` §3.8.

    The aggregate job (``precis_pathway.aggregate_job``) combines the seed
    partials in-process (pure numpy, ``aggregate_partials`` — no ML deps)
    and emits the SAME scalar ``barrier`` contract onto its own meta that
    the legacy monolith did, so :func:`harvest_measures` needs only a
    ``_fresh_autocatpath_jobs`` query update, not a harvest-logic change.

    Precis-native (no autocatpath import — the `pathway` kind, if the plugin is
    installed, is reached only through the store; the fan-out shape itself is
    read off the plain config dict, see :func:`_autocatpath_mlip_specs` /
    :func:`_autocatpath_search_seeds`) and **defensive**: degrades to a note
    on any error (missing plugin, unloadable scene) and never raises, so a
    compute hiccup can't fail the tick. The one exception is the gr172886
    null-route guard below: on a real multi-node cluster with no GPU host
    advertised in ``resource_slots``, this raises loudly rather than silently
    minting an unrouted junk-EMT job.
    """
    if not isinstance(config, dict) or not config:
        return (
            f"autocatpath skipped: no reaction config for structure {structure_ref_id}"
        )
    config = _apply_tier_config(config, tier)
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
    # Env unset → resolve the GPU node from the runtime capability map rather
    # than degrading to an unrouted EMT job (gr172886). The env-set path (the
    # coordinator daemon) never touches resource_slots, so this adds no new
    # dependency to it.
    if node is None:
        slots = store.all_resource_slots()
        gpu_hosts = {s.host for s in slots if s.resource == "gpu" and s.capacity > 0}
        if gpu_hosts:
            node = sorted(gpu_hosts)[0]
        elif len({s.host for s in slots}) > 1:
            # A real multi-node cluster with no GPU advertised is a prod
            # misconfiguration — minting anyway would silently run junk EMT
            # instead of the intended MACE-on-GPU. Fail loud. Empty/single-host
            # resource_slots is the dev/CI shape (no cluster to misroute onto),
            # so that still falls through to the in-process EMT path below.
            hosts = {s.host for s in slots}
            raise RuntimeError(
                f"autocatpath dispatch for {ref.slug}: no GPU route node — env "
                f"{_AUTOCATPATH_ROUTE_NODE_ENV} unset and no host advertises the "
                f"'gpu' resource, but resource_slots spans {sorted(hosts)}. "
                "Refusing to mint an unrouted EMT job on a cluster (gr172886). "
                f"Set {_AUTOCATPATH_ROUTE_NODE_ENV} or fix the GPU host's "
                "heartbeat/resource_slots."
            )
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
        # dtype=mixed (autocatpath 0.6): float32 coarse relax → float64 refine to
        # fmax; NEB tops stay float64, so reported barriers keep float64 accuracy
        # while the per-eval descent runs at ~float32 speed. The seed-wall-overrun
        # lever (paired with best_first NEB scheduling below). Explicit override wins.
        run_config["mlip"].setdefault("dtype", "mixed")
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
                        # dispatch-time tier stamp — the promotion-eligibility
                        # + `refines`-lineage seam (:func:`_find_tier_pathway`)
                        # reads this even while the pathway is still
                        # "computing" (before catpath's own results.json is
                        # available to derive it from).
                        "tier": tier,
                    },
                    conn=conn,
                )
            pathway_ref_id = int(pref.id)
    except Exception as e:
        return f"autocatpath dispatch failed for {ref.slug}: pathway ref ({e})"

    # Fan-out shape: (model_index, seed) pairs — read off the plain config
    # dict, no autocatpath import (see the docstring above).
    specs = _autocatpath_mlip_specs(run_config)
    seeds = _autocatpath_search_seeds(run_config)

    try:
        from precis.handlers.job import JobHandler

        jobs = JobHandler(hub=hub)

        # T_agg: content-addressed on the SAME key as the pathway ref, so a
        # re-dispatch reuses the existing tree instead of minting a
        # duplicate. No job minted here — see the docstring: the dispatch
        # worker mints T_agg's own job once every seed todo below it is done.
        agg_todo_id = _ensure_autocatpath_todo(
            store,
            parent_id=structure_ref_id,
            content_key=key,
            title=f"autocatpath aggregate: {ref.slug} → {pslug}",
            meta={
                "executor": "ssh_node",
                "job_type": "autocatpath_aggregate",
                "params": {
                    "pathway_ref_id": pathway_ref_id,
                    "pathway_slug": pslug,
                    "config": run_config,
                    "force_backend": force,
                    "content_key": key,
                    "target_node": node,
                    "resources": {"wall_seconds": _autocatpath_wall_seconds()},
                },
            },
        )

        minted = 0
        for model_index in range(len(specs)):
            for seed in seeds:
                skey = _autocatpath_seed_content_key(
                    run_config, slab_extxyz, seed, model_index
                )
                seed_todo_id = _find_child_todo_by_content_key(store, agg_todo_id, skey)
                if seed_todo_id is not None and _seed_todo_handled(store, seed_todo_id):
                    # done/won't-do, or already has a succeeded/live job —
                    # leave it alone (§B-1's original "retry skips it").
                    continue
                # No seed todo yet, OR one exists but its only job(s) are
                # failed/cancelled — this is the automatic infra-repair path
                # (docs/backlog — qu164903): re-mint a fresh seed job under
                # the SAME todo (get-or-create below reuses it) rather than
                # leaving the candidate wedged behind a dead seed forever.
                minted += 1
                seed_todo_id = _ensure_autocatpath_todo(
                    store,
                    parent_id=agg_todo_id,
                    content_key=skey,
                    title=(
                        f"autocatpath seed {seed} model#{model_index}: "
                        f"{ref.slug} → {pslug}"
                    ),
                    meta={"auto_check": {"type": "child_job_succeeded"}},
                )
                jobs.put(
                    job_type="autocatpath_seed",
                    executor="ssh_node",
                    parent_id=seed_todo_id,
                    idem_key=f"autocatpath_seed:{skey}",
                    # gpu slot: serialize seeds one-at-a-time per GPU
                    # (gr192371) — meta.requires, not SPEC.requires.
                    requires={"gpu": 1},
                    params={
                        "config": run_config,
                        "slab_extxyz": slab_extxyz,
                        "seed": seed,
                        "model_index": model_index,
                        "force_backend": force,
                        "content_key": skey,
                        "target_node": node,
                        # Provenance only: lets the seed job stamp its own
                        # meta.pathway_ref so the pathway page's run-job links
                        # reach the per-seed run_log chunks, not just the
                        # aggregate's transcript.
                        "pathway_ref_id": pathway_ref_id,
                        # Per-seed lease margin — minutes-scale in practice
                        # (one model, one seed), but sized the same as
                        # before: cheap insurance, and the wedge fix is the
                        # job's SHORT compute duration, not a tighter lease.
                        "resources": {"wall_seconds": _autocatpath_wall_seconds()},
                    },
                )
    except Exception as e:
        return f"autocatpath dispatch failed for {ref.slug}: tree mint ({e})"

    total = len(specs) * len(seeds)
    return (
        f"autocatpath[{force or 'config'}] dispatched {total} seed(s) "
        f"({minted} new) + aggregate for {ref.slug} → pathway {pslug}"
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

#: CHE electrochemistry scalars (the pathway potential lever)
#: that ride the SAME harvest path — and the SAME trust gate —
#: as the barrier: ``_dispatch_common.finish`` already stamps these verbatim
#: onto the job's own meta (a straight pass-through of catpath's
#: ``results_json``), so lifting them here is the same "read the job meta"
#: move as the barrier/span above. ``span_at_UL`` is deliberately NOT lifted
#: onto the candidate — it stays a job-meta-only diagnostic.
_AUTOCATPATH_ELECTRO_KEYS: tuple[str, ...] = ("U_L", "U_opt", "span_at_Uopt", "P_side")

#: Selectivity / poisoning scalars (catpath >= 0.6.0 engine scorecard,
#: ``results_json.score``; ``precis_pathway._dispatch_common._selectivity_scalars``
#: derives them onto the job meta). Ranking measures like the barrier:
#: ``selectivity_margin`` (eV, maximize — worst branch-point margin: side
#: climb minus the competing main-route climb at the same fork),
#: ``trap_margin`` (eV, maximize — best-route span minus the worst
#: OFF-route state's escape climb; absent when there are no off-route
#: states), ``poison_margin`` (eV, maximize — worst screened poison's
#: ``delta_vs_substrate``, engine-computed).
_AUTOCATPATH_SELECTIVITY_KEYS: tuple[str, ...] = (
    "selectivity_margin",
    "trap_margin",
    "poison_margin",
)
#: Naming context riding alongside the scalars — never measures (strings /
#: dicts; ``frontier._META_NON_MEASURE`` excludes them): the most competitive
#: side product, the deepest trap state, the per-species poison verdicts,
#: and (engine >= 0.6.0 scorecard) which axis limits this candidate plus its
#: one-line ``worst_problem`` statement.
#: The tick prompt surfaces these to the discovery agent / literature step.
_AUTOCATPATH_SELECTIVITY_CONTEXT_KEYS: tuple[str, ...] = (
    "side_worst",
    "trap_worst",
    "poison_verdicts",
    "limiting_factor",
    "worst_problem",
)


def _num_measure(v: Any) -> float | None:
    """A numeric measure, or None (``bool`` is an ``int`` but never a measure)."""
    if isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return float(v)
    return None


def _autocatpath_measures_from_job(meta: dict[str, Any]) -> dict[str, Any]:
    """Lift the scalar barrier/span from a completed `autocatpath_explore` job's meta.

    Reads a ``result`` sub-dict if present (the bridge's summary), else the meta
    top level. The presence of a numeric barrier IS the "done" signal — a
    still-running job carries none, so it is simply skipped.
    """
    result = meta.get("result")
    src = result if isinstance(result, dict) else meta
    out: dict[str, Any] = {}
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
    # CHE electro scalars (U_L, U_opt, span_at_Uopt, P_side — see
    # _AUTOCATPATH_ELECTRO_KEYS) ride the same harvest + trust gate as the
    # barrier (frontier._candidate_from_structure pops all of them together
    # when the pathway is untrusted). U_L_abs is derived here (the rubric
    # minimizes |U_L|, not U_L's sign) so it lands alongside U_L with no
    # separate harvest step.
    for k in _AUTOCATPATH_ELECTRO_KEYS:
        v = _num_measure(src.get(k))
        if v is not None:
            out[k] = v
    if "U_L" in out:
        out["U_L_abs"] = abs(out["U_L"])
    # Selectivity/poisoning: the three ranking scalars plus their non-numeric
    # naming context (side/trap state names, poison verdicts) — the context
    # rides onto the candidate meta for display + the tick prompt, and
    # frontier's non-measure filter keeps it out of the objective vector.
    for k in _AUTOCATPATH_SELECTIVITY_KEYS:
        v = _num_measure(src.get(k))
        if v is not None:
            out[k] = v
    for k in _AUTOCATPATH_SELECTIVITY_CONTEXT_KEYS:
        ctx = src.get(k)
        if isinstance(ctx, (str, dict)) and ctx:
            out[k] = ctx
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
    """Completed autocatpath result jobs under a candidate, newer than ``upto``.

    Two shapes, both parented under the candidate (§B-1, gr180096):

    * legacy flat — a ``autocatpath_explore`` job directly on the candidate
      (pre-fan-out rows; the job_type stays registered so these don't
      error-loop, see ``precis_pathway/job.py``);
    * the fan-out's aggregate — a ``autocatpath_aggregate`` job one level
      down, under the aggregate todo (``T_agg``, itself a direct child of
      the candidate — see :func:`dispatch_autocatpath`'s docstring for the
      full tree). Both emit the SAME scalar-barrier contract onto their own
      job meta, so :func:`_autocatpath_measures_from_job` reads either
      shape unchanged.

    Returns ``(job_ref_id, meta)`` oldest-first so harvest is deterministic and
    the idempotency bookmark advances monotonically.
    """
    with store.pool.connection() as conn:
        rows = conn.execute(
            """
            SELECT j.ref_id, j.meta FROM refs j
             WHERE j.kind = 'job' AND j.deleted_at IS NULL AND j.ref_id > %(upto)s
               AND (
                     (j.parent_id = %(sid)s
                      AND j.meta->>'job_type' = 'autocatpath_explore')
                  OR (j.meta->>'job_type' = 'autocatpath_aggregate'
                      AND j.parent_id IN (
                            SELECT ref_id FROM refs
                             WHERE parent_id = %(sid)s AND kind = 'todo'
                               AND deleted_at IS NULL
                          ))
               )
             ORDER BY j.ref_id ASC
            """,
            {"sid": structure_ref_id, "upto": upto},
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


def _find_tier_pathway(store: Store, structure_ref_id: int, tier: str) -> int | None:
    """The ref id of the ``tier``-rung `pathway` dispatched for this candidate
    (in-flight or complete), or ``None`` — the promotion-eligibility +
    `refines`-lineage seam.

    Reads the dispatch-time ``meta.tier`` stamp (:func:`dispatch_autocatpath`),
    so this sees an in-flight (``status="computing"``) pathway too, not just a
    finished one — promotion must not re-spend its budget on a candidate
    whose next-tier run is already dispatched but not yet harvested. A
    pathway minted before the ladder existed carries no ``tier`` key and
    never matches — a legacy candidate's pre-ladder pathways are simply
    invisible to this query, matching the ladder-off default (no promotion/
    lineage bookkeeping regresses onto legacy data).
    """
    with store.pool.connection() as conn:
        row = conn.execute(
            "SELECT ref_id FROM refs WHERE kind = 'pathway' AND deleted_at IS NULL "
            "AND meta->>'candidate_ref' = %s AND meta->>'tier' = %s "
            "ORDER BY ref_id DESC LIMIT 1",
            (str(structure_ref_id), tier),
        ).fetchone()
    return int(row[0]) if row else None


def _pathway_tier(pw_meta: dict[str, Any] | None) -> str:
    """The ladder tier a completed pathway belongs to.

    Prefers the dispatch-time stamp (:func:`dispatch_autocatpath`'s
    ``meta.tier``, present on every ladder-aware dispatch); falls back to
    reading catpath's own verbatim ``meta.results`` (``results.screening`` /
    ``results.template`` / ``results.neb_schedule`` — the contract this
    whole ladder is built on, see the module docstring) for a pathway
    dispatched before the stamp existed or minted outside precis. Since
    catpath >= 0.10 runs coadsorbed at BOTH NEB tiers (the ammonia default),
    ``template == "coadsorbed"`` alone no longer implies verify — the neb
    tier's fast-screening overlay is the disambiguator: catpath emits the
    per-step ``results.neb_schedule`` dict only when best_first ran, so a
    coadsorbed result WITH it is neb (pruned) and WITHOUT it is verify
    (exhaustive). A pathway with no signal at all (or no pathway —
    ``pw_meta=None``) defaults to ``"neb"``, today's straight-to-NEB shape —
    the ladder-off behaviour this feature must not regress.
    """
    if isinstance(pw_meta, dict):
        tier = pw_meta.get("tier")
        if tier in _TIERS:
            return str(tier)
        results = pw_meta.get("results")
        results = results if isinstance(results, dict) else {}
        if results.get("screening") is True:
            return _TIER_SCREENING
        if results.get("template") == "coadsorbed" and not results.get("neb_schedule"):
            return _TIER_VERIFY
    return _TIER_NEB


def _canonicalize_barrier(
    existing_meta: dict[str, Any], measures: dict[str, Any], tier: str
) -> None:
    """Mutate ``measures`` (about to be stamped onto the candidate) so the
    ranked ``barrier``/``span`` reflect the HIGHEST-fidelity pathway
    (exhaustive=verify > best_first-pruned=neb), never silently discarding a
    superseded value: when a fresh **verify**-tier barrier supersedes an
    existing **neb**-tier one, the outgoing pruned value moves to
    ``barrier_screen`` (kept as calibration data — the pruned→exhaustive
    delta). ``barrier_tier``
    always tracks which tier the candidate's current ``barrier`` came from
    (read by :mod:`precis.quest.graduate`'s verify-gate and
    :mod:`precis.quest.frontier`'s ``Candidate.flags``).

    A screening-tier run never carries a ``barrier`` at all (catpath omits
    it), so this is a no-op whenever ``measures`` has none — the "screening
    contributes no barrier" contract flows through untouched rather than
    being special-cased here.
    """
    if "barrier" not in measures:
        return
    if (
        existing_meta.get("barrier_tier") == _TIER_NEB
        and tier == _TIER_VERIFY
        and "barrier" in existing_meta
    ):
        measures["barrier_screen"] = existing_meta["barrier"]
    measures["barrier_tier"] = tier


def _bump_tier_stamp(
    existing_meta: dict[str, Any], measures: dict[str, Any], tier: str
) -> None:
    """Stamp ``measures['tier']`` = the highest ladder tier with a completed
    run on this candidate (screening < neb < verify) — independent of
    ``barrier`` (a screening run completes with thermodynamic measures but no
    barrier at all, and still earns the stamp)."""
    current = existing_meta.get("tier")
    current_rank = _TIER_FIDELITY.get(current, -1) if isinstance(current, str) else -1
    if _TIER_FIDELITY.get(tier, 0) >= current_rank:
        measures["tier"] = tier


def _link_refines(
    store: Store, structure_ref_id: int, verify_pathway_ref_id: int
) -> None:
    """When a verify-tier pathway lands for a candidate that also has a
    neb-tier pathway, wire verify → ``refines`` → neb — "a
    higher-fidelity treatment of the same object" (the closed relation
    vocab, ``src/precis/data/skills/precis-relations.md``; the slug itself
    already exists in the DB/``Relation`` literal — minted for taproot claim
    hubs, migration 0100 — this is a second, unrelated use of the same
    general-purpose relation). Idempotent (:meth:`Store.add_link`'s own
    dedup) and defensive: no neb sibling, or any lookup/link failure, is a
    silent no-op — never a harvest failure.
    """
    try:
        neb_id = _find_tier_pathway(store, structure_ref_id, _TIER_NEB)
        if neb_id is None or neb_id == verify_pathway_ref_id:
            return
        store.add_link(
            src_ref_id=verify_pathway_ref_id, dst_ref_id=neb_id, relation="refines"
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
    """The latest autocatpath job's ``(STATUS, meta)`` under this candidate,
    across BOTH shapes (mirrors :func:`_fresh_autocatpath_jobs`):

    * legacy flat — a ``autocatpath_explore`` job directly on the candidate
      (pre-fan-out; retired by 47332ad3, nothing mints these anymore);
    * the fan-out's aggregate — a ``autocatpath_aggregate`` job one level
      down, under the aggregate todo (``T_agg``, itself a direct child of
      the candidate — see :func:`dispatch_autocatpath`'s docstring).

    The sibling of :func:`_latest_relax_job` for the barrier lane. Unlike relax —
    where a ``failed`` job may carry a genuine *physical* verdict (non-convergence
    ⇒ rule the candidate out) — a failed autocatpath job is **always** a
    compute/infra failure: the NEB/barrier run crashed, which says nothing about
    whether the material has a viable pathway. So the harvest treats every autocatpath
    failure as retry-eligible and never rules out on it. Watching both
    shapes matters for two reasons: the retry lane must see failures of the CURRENT
    path (``autocatpath_aggregate``, minted by :func:`dispatch_autocatpath`'s
    seed/aggregate fan-out) — the legacy-only query left it blind to those; and a
    failed legacy ``autocatpath_explore`` is always stale pre-fan-out signal (gr191615
    — see the amnesty branch in :func:`harvest_measures`), never current-path noise.
    """
    with store.pool.connection() as conn:
        row = conn.execute(
            "SELECT t.value, j.meta FROM refs j "
            "JOIN ref_tags rt ON rt.ref_id = j.ref_id "
            "JOIN tags t ON t.tag_id = rt.tag_id "
            "WHERE j.kind = 'job' AND j.deleted_at IS NULL "
            "AND t.namespace = 'STATUS' "
            "AND ((j.parent_id = %(sid)s "
            "      AND j.meta->>'job_type' = 'autocatpath_explore') "
            "  OR (j.meta->>'job_type' = 'autocatpath_aggregate' "
            "      AND j.parent_id IN ( "
            "            SELECT ref_id FROM refs "
            "             WHERE parent_id = %(sid)s AND kind = 'todo' "
            "               AND deleted_at IS NULL "
            "          ))) "
            "ORDER BY j.ref_id DESC LIMIT 1",
            {"sid": structure_ref_id},
        ).fetchone()
    if row is None:
        return None
    return str(row[0]), dict(row[1] or {})


def _stuck_seed_failure(
    store: Store, structure_ref_id: int
) -> tuple[str, dict[str, Any]] | None:
    """The fallback :func:`_latest_autocatpath_job` can't see: a candidate
    wedged behind a dead **seed**, with no aggregate lane even minted yet.

    ``_latest_autocatpath_job`` only watches ``autocatpath_explore`` (legacy)
    and ``autocatpath_aggregate`` jobs — but the aggregate never mints while
    any per-seed todo under ``T_agg`` is still open (its own auto_check
    gate, see :func:`dispatch_autocatpath`'s docstring), so a seed that
    infra-failed and stays failed leaves the candidate with NO autocatpath
    job of either watched shape at all — invisible to the retry ladder,
    silently wedged forever (qu164903: 9 candidates lost this way). This
    function is the seed-level fallback that makes that state visible.

    Returns ``("failed", newest_failed_seed_job_meta)`` iff — in one query —
    the candidate is wedged:

    * **no** ``autocatpath_aggregate`` job exists yet under any ``T_agg``
      child of ``structure_ref_id`` (any status — an aggregate already
      minted, even a failed one, means the wedge is gone and
      :func:`_latest_autocatpath_job` is the truth instead); **and**
    * at least one still-open seed todo (``kind='todo'``, not
      ``done``/``won't-do``, a child of a ``T_agg`` child of the candidate)
      has a seed job whose ``STATUS`` is ``failed``/``cancelled`` **and no**
      seed job whose ``STATUS`` is anything else — i.e. it reads
      :func:`_seed_todo_handled` ``== False`` from the SQL side, restricted
      to seeds that have actually tried and failed (not merely "no job
      yet" — a candidate mid-initial-dispatch, todo committed but
      ``jobs.put`` not yet reached, must never false-fire this as a
      "failure").

    Returns ``None`` otherwise (nothing wedged, or an aggregate already
    exists so the ordinary ladder owns it). The literal status string
    ``"failed"`` is returned regardless of whether the underlying seed job's
    own STATUS is ``failed`` or ``cancelled`` — the caller
    (``harvest_measures``) only branches on ``== "failed"``, and both seed
    outcomes are equally infra-repairable, never a physical verdict (same
    reasoning as :func:`_latest_autocatpath_job`'s own docstring).

    **Known masking edge, intentionally accepted.** A candidate can carry
    TWO+ independent ``T_agg`` trees (a config/tier change re-dispatches
    under a fresh content key — see :func:`dispatch_autocatpath`'s
    docstring). If an OLDER tree has a permanently-dead seed while a NEWER
    tree's aggregate later SUCCEEDS, ``_latest_autocatpath_job`` (highest
    ``ref_id`` across ALL trees) returns that newer succeeded job — a
    non-``"failed"`` status short-circuits ``harvest_measures``'s outer
    ``if`` before this function ever runs, via the ``or``'s short-circuit
    (only invoked when ``_latest_autocatpath_job`` is ``None``, and never
    consulted again once it isn't). So the old tree's stuck seed is never
    repaired or gripe-filed — it becomes permanently invisible clutter
    under the candidate. This is NOT a bug: the candidate has
    forward-progressed via the newer tree, no compute or barrier signal is
    lost, and the stale sub-tree is dormant (no live job, no lease held) —
    just an orphaned todo subtree a human would need to notice manually
    (e.g. via the nursery) to prune. Only a candidate stuck behind a dead
    seed with NO surviving/successful tree at all is this function's
    concern.
    """
    with store.pool.connection() as conn:
        row = conn.execute(
            """
            SELECT j.meta
              FROM refs t_agg
              JOIN refs seed_todo ON seed_todo.parent_id = t_agg.ref_id
                                  AND seed_todo.kind = 'todo'
                                  AND seed_todo.deleted_at IS NULL
              JOIN refs j ON j.parent_id = seed_todo.ref_id
                          AND j.kind = 'job' AND j.deleted_at IS NULL
                          AND j.meta->>'job_type' = 'autocatpath_seed'
              JOIN ref_tags rt ON rt.ref_id = j.ref_id
              JOIN tags t ON t.tag_id = rt.tag_id
                          AND t.namespace = 'STATUS'
                          AND t.value IN ('failed', 'cancelled')
             WHERE t_agg.parent_id = %(sid)s
               AND t_agg.kind = 'todo' AND t_agg.deleted_at IS NULL
               AND COALESCE(
                     (SELECT t2.value FROM ref_tags rt2
                        JOIN tags t2 ON t2.tag_id = rt2.tag_id
                       WHERE rt2.ref_id = seed_todo.ref_id
                         AND t2.namespace = 'STATUS' LIMIT 1),
                     'open'
                   ) NOT IN ('done', 'won''t-do')
               AND NOT EXISTS (
                     SELECT 1 FROM refs j2
                       JOIN ref_tags rt3 ON rt3.ref_id = j2.ref_id
                       JOIN tags t3 ON t3.tag_id = rt3.tag_id
                      WHERE j2.parent_id = seed_todo.ref_id AND j2.kind = 'job'
                        AND j2.deleted_at IS NULL
                        AND t3.namespace = 'STATUS'
                        AND t3.value NOT IN ('failed', 'cancelled')
                   )
               AND NOT EXISTS (
                     SELECT 1 FROM refs agg_job
                      WHERE agg_job.kind = 'job' AND agg_job.deleted_at IS NULL
                        AND agg_job.meta->>'job_type' = 'autocatpath_aggregate'
                        AND agg_job.parent_id IN (
                              SELECT ref_id FROM refs
                               WHERE parent_id = %(sid)s AND kind = 'todo'
                                 AND deleted_at IS NULL
                            )
                   )
             ORDER BY j.ref_id DESC LIMIT 1
            """,
            {"sid": structure_ref_id},
        ).fetchone()
    if row is None:
        return None
    return "failed", dict(row[0] or {})


def _mark_harvested(store: Store, structure_ref_id: int, upto_run_id: int) -> None:
    with store.tx() as conn:
        conn.execute(
            "UPDATE refs SET meta = meta || jsonb_build_object("
            "'quest_harvested_upto', %s::int) WHERE ref_id = %s",
            (upto_run_id, structure_ref_id),
        )


#: How many infra-failure retries a candidate's relax gets before the harvest
#: stops re-dispatching and files a gripe instead (see ``harvest_measures``).
#: Not an env dial — retry-once-then-gripe is the whole point:
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


#: The seed-lane repair ladder's own retry window — deliberately NOT
#: ``quest_autocatpath_infra_retries`` (the aggregate/explore counter above).
#: That counter is non-windowed (retry-once-EVER per candidate) and is reset
#: to 0 by the gr191615 amnesty branch — reusing it here would let a stale
#: amnesty reset silently re-arm the seed lane too, or (the opposite failure)
#: let one long-lived candidate's earlier aggregate-lane retry permanently
#: exhaust the UNRELATED seed lane's single shot. A transient GPU-node
#: hiccup that infra-kills several correlated seeds is a *fresh* event each
#: time it recurs, independent of whatever the aggregate lane did months
#: earlier — so the seed lane needs its own **windowed** budget (mirroring
#: ``handlers/_job_bubble.py``'s ``_bump_orphan_retry_count``, window 6h):
#: ``_MAX_INFRA_RETRIES`` free re-dispatches per rolling window, then one
#: gripe, then silence until the window rolls over and a fresh failure
#: re-arms it. One re-dispatch call re-mints EVERY currently-stuck seed
#: under the candidate at once (:func:`dispatch_autocatpath`'s fan-out), so
#: a correlated multi-seed node kill costs exactly one window slot, not one
#: per seed.
_SEED_INFRA_RETRY_WINDOW_HOURS = 6

#: ``jsonb_set``-in-one-``UPDATE`` window read/bump for
#: ``meta.quest_seed_infra_retries`` — same shape as
#: ``handlers/_job_bubble.py``'s ``_BUMP_ORPHAN_RETRY_SQL``: the count resets
#: to 1 (a fresh window) when ``quest_seed_infra_retries_window_start`` is
#: absent or older than the window, otherwise it keeps climbing. A single
#: atomic ``UPDATE … RETURNING`` — Postgres serialises concurrent writers on
#: the row, no separate lock needed.
_BUMP_SEED_INFRA_RETRY_SQL = """
    UPDATE refs
       SET meta = jsonb_set(
             jsonb_set(
               COALESCE(meta, '{}'::jsonb),
               '{quest_seed_infra_retries}',
               to_jsonb(
                 CASE
                   WHEN (meta->>'quest_seed_infra_retries_window_start') IS NULL
                     OR (meta->>'quest_seed_infra_retries_window_start')::timestamptz
                        < now() - %(window)s::interval
                   THEN 1
                   ELSE COALESCE((meta->>'quest_seed_infra_retries')::int, 0) + 1
                 END
               ),
               true
             ),
             '{quest_seed_infra_retries_window_start}',
             to_jsonb(
               CASE
                 WHEN (meta->>'quest_seed_infra_retries_window_start') IS NULL
                   OR (meta->>'quest_seed_infra_retries_window_start')::timestamptz
                      < now() - %(window)s::interval
                 THEN now()
                 ELSE (meta->>'quest_seed_infra_retries_window_start')::timestamptz
               END
             ),
             true
           )
     WHERE ref_id = %(sid)s
 RETURNING (meta->>'quest_seed_infra_retries')::int
"""


def _seed_infra_retry_count(store: Store, structure_ref_id: int) -> int:
    """Read-only: the candidate's CURRENT-window ``quest_seed_infra_retries``.

    Returns 0 (a fresh window, no write) when the window is absent or has
    lapsed — mirrors :data:`_BUMP_SEED_INFRA_RETRY_SQL`'s own reset branch,
    but without bumping, so ``harvest_measures`` can branch on the ladder
    rung *before* deciding whether to bump at all (only the retry and gripe
    rungs bump — the "already gripe-filed, dedup" rung must not keep
    climbing every tick, see the ladder below)."""
    with store.pool.connection() as conn:
        row = conn.execute(
            """
            SELECT CASE
                     WHEN (meta->>'quest_seed_infra_retries_window_start') IS NULL
                       OR (meta->>'quest_seed_infra_retries_window_start')::timestamptz
                          < now() - %(window)s::interval
                     THEN 0
                     ELSE COALESCE((meta->>'quest_seed_infra_retries')::int, 0)
                   END
              FROM refs WHERE ref_id = %(sid)s
            """,
            {
                "window": f"{_SEED_INFRA_RETRY_WINDOW_HOURS} hours",
                "sid": structure_ref_id,
            },
        ).fetchone()
    return int(row[0]) if row and row[0] is not None else 0


def _bump_seed_infra_retry_count(store: Store, structure_ref_id: int) -> int:
    """Increment ``meta.quest_seed_infra_retries``, windowed. Returns the new
    (post-bump) count. See :func:`_seed_infra_retry_count` for the paired
    read-only half and :data:`_SEED_INFRA_RETRY_WINDOW_HOURS` for why this
    counter is windowed and independent of ``quest_autocatpath_infra_retries``."""
    with store.pool.connection() as conn:
        row = conn.execute(
            _BUMP_SEED_INFRA_RETRY_SQL,
            {
                "window": f"{_SEED_INFRA_RETRY_WINDOW_HOURS} hours",
                "sid": structure_ref_id,
            },
        ).fetchone()
        conn.commit()
    return int(row[0]) if row and row[0] is not None else 0


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
      in the live dossier. Dossier-owned-by-process: when ``hub`` is given, the *first*
      infra failure gets re-dispatched once (``meta.quest_infra_retries``
      tracks it) so the candidate goes back to non-terminal and the loop
      *awaits* it instead of drifting dry; a *second* infra failure files a
      bounded gripe instead of retrying again, and stays retry-eligible in
      neither sense (no third dispatch, never ruled out). ``hub=None``
      (dry preview / callers that don't exercise this) preserves the
      original note-only behaviour.
    * a candidate whose latest **autocatpath** job failed gets the *same*
      retry-once-then-gripe treatment on its own counter
      (``meta.quest_autocatpath_infra_retries``), but **never** ruled out: a failed
      autocatpath is always a crashed NEB (a compute/infra failure), never a
      physical "no viable pathway" verdict, so — unlike relax non-convergence —
      it carries no verdict on the material (barrier-lane mirror).
      A failed legacy ``autocatpath_explore`` job (retired by the seed/aggregate
      fan-out, 47332ad3 — nothing mints one anymore) instead gets a one-shot
      **amnesty**: re-dispatched via the current path with the counter reset to
      0, bypassing the ladder entirely, since the poison-fail defect that spent
      it is fixed and the failure carries no signal against the current run
      (gr191615).
    * a candidate wedged behind a dead **seed** with no aggregate lane even
      minted yet — invisible to the two bullets above, since neither an
      ``autocatpath_explore`` nor an ``autocatpath_aggregate`` job exists in
      that state (see :func:`_stuck_seed_failure`) — gets the *same*
      retry-once-then-gripe shape, but on its OWN **windowed** counter
      (``meta.quest_seed_infra_retries``, :func:`_seed_infra_retry_count` /
      :func:`_bump_seed_infra_retry_count`, 6h window) rather than
      ``quest_autocatpath_infra_retries`` — see
      :data:`_SEED_INFRA_RETRY_WINDOW_HOURS` for why the two counters must
      stay independent. The re-dispatch is a plain
      :func:`dispatch_autocatpath` call, which re-mints every currently-stuck
      seed under the candidate at once (status-aware fan-out,
      :func:`_seed_todo_handled`) — this closes the qu164903 class of bug
      (9 lost candidates: an infra-killed seed job used to stay
      ``STATUS:failed`` forever, with nothing watching it).
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
        # Local running view of the candidate's own meta — updated after each
        # stamp below so tier canonicalization (:func:`_canonicalize_barrier`
        # / :func:`_bump_tier_stamp`) sees THIS harvest call's own prior
        # writes, not just what was on disk when the outer `for s in
        # structures` loop fetched `s` (multiple completed jobs can land in
        # one harvest pass).
        candidate_meta = dict(s.meta or {})
        for job_id, jmeta in cp_jobs:
            measures = _autocatpath_measures_from_job(jmeta)
            if not measures:
                continue  # still running — do not advance the bookmark, retry next tick
            cp_seen = max(cp_seen, job_id)
            pathway_ref = jmeta.get("pathway_ref")
            pw_meta: dict[str, Any] | None = None
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
            tier = _pathway_tier(pw_meta)
            _canonicalize_barrier(candidate_meta, measures, tier)
            _bump_tier_stamp(candidate_meta, measures, tier)
            store.stamp_ref_meta(s.id, measures)
            candidate_meta.update(measures)
            if isinstance(pathway_ref, int) and not isinstance(pathway_ref, bool):
                _link_pathway(store, s.id, pathway_ref)
                if tier == _TIER_VERIFY:
                    _link_refines(store, s.id, pathway_ref)
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
        # — see the docstring above.
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

        # Autocatpath (barrier-lane) infra failure — the dossier-owned-by-process mirror of the
        # relax infra branch above, on the *barrier* lane. Unlike relax (where a
        # failed job can be a physical non-convergence verdict → rule out), a
        # failed autocatpath job is ALWAYS a compute/infra failure: the NEB
        # run crashed, which says nothing about the material — so it NEVER rules
        # out. Retry-once-then-gripe on a per-candidate counter; the re-dispatch
        # puts a fresh sim back in flight so the loop *awaits* it instead of
        # reading the crash as a dry tick (the laundering §C names). Skipped for
        # an already-ruled-out candidate (a dead geometry earns no more barrier
        # compute), and — like relax — note-only when ``hub`` is absent (dry
        # preview) or the quest has no reaction config to re-dispatch against.
        #
        # `_latest_autocatpath_job` watches two shapes (legacy explore, current
        # aggregate); `_stuck_seed_failure` is a THIRD, seed-level fallback for a
        # state neither shape can see — a dead seed with no aggregate minted yet
        # (see its own docstring, qu164903). The `or` preserves
        # aggregate-preferring semantics: an aggregate of ANY status existing
        # means the seed lane has already resolved (the aggregate only mints
        # once every seed todo under it is done), so the fallback only speaks
        # when no aggregate lane exists at all.
        cp_ruled_out = any(
            str(t).startswith("ruled-out:") for t in store.tags_for(s.id)
        )
        autocatpath_job = _latest_autocatpath_job(store, s.id) or _stuck_seed_failure(
            store, s.id
        )
        if (
            not cp_ruled_out
            and autocatpath_job is not None
            and autocatpath_job[0] == "failed"
        ):
            _cp_status, cp_job_meta = autocatpath_job
            reaction = _quest_reaction_config(store, quest_id)
            # gr191615 amnesty: nothing has minted an `autocatpath_explore` job
            # since the seed/aggregate fan-out landed (47332ad3) — every candidate
            # now runs `autocatpath_aggregate` (see dispatch_autocatpath). So a
            # *failed* explore job seen here is always from the since-fixed
            # poison-fail era: it spent quest_autocatpath_infra_retries on a
            # defect that no longer exists, which strands the candidate behind an
            # already-exhausted counter for a failure with no current bearing.
            # Re-dispatch bypasses the ladder and resets the counter so the fresh
            # current-path run gets its own full §C retry-once-then-gripe.
            # dispatch_autocatpath is content-addressed per engine token
            # (T_agg/seed content keys), so a repeat tick before the new
            # aggregate lands just collapses onto the same tree rather than
            # double-dispatching; the amnesty stops firing on its own once that
            # aggregate job becomes the latest job under the candidate.
            if cp_job_meta.get("job_type") == "autocatpath_explore":
                if hub is None or reaction is None:
                    notes.append(
                        f"stale-era autocatpath failure for {handle} "
                        "(amnesty-eligible, not ruled out)"
                    )
                else:
                    dispatch_autocatpath(store, s.id, reaction, hub=hub)
                    store.stamp_ref_meta(s.id, {"quest_autocatpath_infra_retries": 0})
                    notes.append(
                        f"stale-era autocatpath failure for {handle} → amnesty "
                        "re-dispatch via seed/aggregate"
                    )
            elif cp_job_meta.get("job_type") == "autocatpath_seed":
                # The `_stuck_seed_failure` fallback — its own WINDOWED counter
                # (`quest_seed_infra_retries`), deliberately not
                # `quest_autocatpath_infra_retries` (see
                # `_SEED_INFRA_RETRY_WINDOW_HOURS`'s docstring for why the two
                # must stay independent). Same retry-once-then-gripe SHAPE as
                # the aggregate branch below, windowed instead of monotonic.
                if hub is None or reaction is None:
                    notes.append(
                        f"stuck seed for {handle} (retry-eligible, not re-dispatched)"
                    )
                else:
                    sk_retries = _seed_infra_retry_count(store, s.id)
                    if sk_retries < _MAX_INFRA_RETRIES:
                        dispatch_autocatpath(store, s.id, reaction, hub=hub)
                        _bump_seed_infra_retry_count(store, s.id)
                        notes.append(
                            f"stuck seed for {handle} → re-dispatched "
                            f"(retry {sk_retries + 1})"
                        )
                    elif sk_retries < _MAX_INFRA_RETRIES + 1:
                        _file_infra_gripe(
                            store,
                            quest_id,
                            handle,
                            cp_job_meta,
                            hub=hub,
                            lane="autocatpath-seed",
                        )
                        _bump_seed_infra_retry_count(store, s.id)
                        notes.append(f"stuck seed persists for {handle} → gripe filed")
                    else:
                        # Already gripe-filed within this window — dedup, no
                        # re-file (the counter only bumps on the retry/gripe
                        # rungs above, so it stays put until the window rolls).
                        notes.append(
                            f"stuck seed persists for {handle} (gripe already filed)"
                        )
            else:
                cp_retries = int(
                    (s.meta or {}).get("quest_autocatpath_infra_retries", 0) or 0
                )
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
                        store,
                        quest_id,
                        handle,
                        cp_job_meta,
                        hub=hub,
                        lane="autocatpath",
                    )
                    store.stamp_ref_meta(
                        s.id,
                        {"quest_autocatpath_infra_retries": _MAX_INFRA_RETRIES + 1},
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


def _tier_ladder_enabled(store: Store, quest_id: int) -> bool:
    """``meta.tier_ladder`` — human-set at seed time (default ``True`` for a
    quest minted via :func:`precis.quest.catalyst_seed.seed_catalyst_quest`,
    see its own docstring), never written by the tick/LLM loop. Absent (a
    quest predating the ladder, or one seeded with ``tier_ladder=False``)
    keeps today's straight-to-NEB behaviour: :func:`run_compute_step`
    dispatches a new candidate's first autocatpath run at ``tier="neb"`` and
    :func:`promote_tiers` never fires.
    """
    ref = store.get_ref(kind="quest", id=quest_id)
    return bool((ref.meta or {}).get("tier_ladder")) if ref is not None else False


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


def _promotion_sort_key(store: Store, quest_id: int, c: Any) -> float:
    """Best-first sort key for a promotion candidate: the quest's declared
    composite objective when configured, else its primary rubric objective —
    the same ranking :func:`precis.quest.frontier.leaderboard` sorts each
    band by. Missing/unmeasured sinks to the bottom (``float('inf')``), same
    convention as ``leaderboard``'s own ``_sort_key``.
    """
    from precis.quest.frontier import _objectives_for, _rubric_composite_for

    composite = _rubric_composite_for(store, quest_id)
    if composite is not None:
        v = c.measures.get(composite["key"])
        return v if isinstance(v, (int, float)) else float("inf")
    objs = _objectives_for(store, quest_id)
    if not objs:
        return float("inf")
    key, sense = objs[0]
    v = c.measures.get(key)
    if v is None:
        return float("inf")
    return v if sense == "min" else -v


def promote_tiers(
    store: Store, quest_id: int, *, hub: Any | None = None, by: str = "agent"
) -> list[str]:
    """Code-driven tier-ladder promotion — mints the NEXT rung's dispatch for
    whichever candidates earned it. Never an LLM decision (no proposal, no
    prompt): purely a function of the harvested measures + human-set caps.

    A no-op unless the quest opted into the ladder (``meta.tier_ladder``,
    see :func:`_tier_ladder_enabled`) and declares a reaction
    (``meta.reaction_config``). Two independent promotions, each a fresh
    :func:`dispatch_autocatpath` call at the next tier — content-addressed
    (the tier folds into the config, hence the idem key), so a promotion
    call that lands on an already-in-flight candidate just collapses onto
    the same job/pathway rather than duplicating it:

    * **screening → neb** — up to ``meta.tier_promote_neb`` (default
      :data:`_DEFAULT_TIER_PROMOTE_NEB`) live, non-ruled-out candidates
      whose highest completed run is ``screening`` (``structure.meta.tier``)
      and that have no neb-tier pathway dispatched yet
      (:func:`_find_tier_pathway`), ranked best-first
      (:func:`_promotion_sort_key`) on the screening tier's thermodynamic
      measures (U_L_abs / span / …).
    * **neb → verify** — up to ``meta.tier_promote_verify`` (default
      :data:`_DEFAULT_TIER_PROMOTE_VERIFY`) **frontier** (Pareto
      non-dominated) candidates with a trusted neb-tier barrier
      (``barrier_trusted is True`` + a ``barrier`` measure — the neb tier is
      the only source of a trusted `barrier` before a verify run lands, since
      screening emits none) and no verify-tier pathway dispatched yet, same
      ranking.

    Returns one short note per promotion dispatched; never raises (a
    promotion bug must not cost an already-successful harvest/graduation
    result — the caller, :func:`run_compute_step`, additionally wraps this
    in its own try/except, matching the defensive convention the
    frontier-tree regen in :mod:`precis.quest.tick` uses).
    """
    notes: list[str] = []
    try:
        if not _tier_ladder_enabled(store, quest_id):
            return notes
        reaction = _quest_reaction_config(store, quest_id)
        if reaction is None:
            return notes
        qref = store.get_ref(kind="quest", id=quest_id)
        qmeta = qref.meta or {} if qref is not None else {}
        cap_neb = int(qmeta.get("tier_promote_neb", _DEFAULT_TIER_PROMOTE_NEB) or 0)
        cap_verify = int(
            qmeta.get("tier_promote_verify", _DEFAULT_TIER_PROMOTE_VERIFY) or 0
        )
        hub = hub or _hub_for(store)

        from precis.quest.frontier import _candidate_from_structure, quest_frontier
        from precis.quest.gaps import _live_servers

        structures = [
            s for s in _live_servers(store, quest_id) if s.kind == "structure"
        ]

        # screening → neb
        if cap_neb > 0:
            eligible = []
            for s in structures:
                if (s.meta or {}).get("tier") != _TIER_SCREENING:
                    continue
                if any(str(t).startswith("ruled-out:") for t in store.tags_for(s.id)):
                    continue
                if _find_tier_pathway(store, s.id, _TIER_NEB) is not None:
                    continue
                eligible.append(_candidate_from_structure(store, s))
            eligible.sort(key=lambda c: _promotion_sort_key(store, quest_id, c))
            for c in eligible[:cap_neb]:
                note = dispatch_autocatpath(
                    store, c.ref_id, reaction, hub=hub, tier=_TIER_NEB
                )
                notes.append(f"promoted {c.handle} screening→neb: {note}")

        # neb → verify (frontier candidates only)
        if cap_verify > 0:
            fr = quest_frontier(store, quest_id)
            eligible_v = [
                c
                for c in fr.frontier
                if c.flags.get("barrier_trusted") is True
                and c.measures.get("barrier") is not None
                and _find_tier_pathway(store, c.ref_id, _TIER_VERIFY) is None
            ]
            eligible_v.sort(key=lambda c: _promotion_sort_key(store, quest_id, c))
            for c in eligible_v[:cap_verify]:
                note = dispatch_autocatpath(
                    store, c.ref_id, reaction, hub=hub, tier=_TIER_VERIFY
                )
                notes.append(f"promoted {c.handle} neb→verify: {note}")
    except Exception:
        log.exception("promote_tiers: promotion pass failed for quest %s", quest_id)
    return notes


#: Candidate-meta keys the barrier lane stamps. :func:`reset_compute` nulls them
#: so a stale (untrusted) barrier stops showing as an `(excluded)` frontier cell
#: while the deployed engine re-scores — the harvest re-stamps real values.
#: Deliberately EXCLUDES ``quest_autocatpath_harvested_upto``: the harvest bookmark
#: is left intact so the old, already-harvested stale jobs stay at/below it and are
#: not re-processed — only the fresh redispatch jobs (higher ref ids) are harvested.
#: Nulling it to 0 would make the next harvest re-read the stale completed job and
#: re-stamp the very barrier this reset just cleared.
#: ``barrier_screen`` / ``barrier_tier`` / ``tier`` are the tier-ladder's own
#: bookkeeping (:func:`_canonicalize_barrier` / :func:`_bump_tier_stamp`) —
#: cleared alongside the barrier itself so a reset candidate's stale tier
#: provenance can't mis-canonicalize the FIRST fresh redispatch result (e.g.
#: reading a stale ``barrier_tier="verify"`` and wrongly refusing to stamp a
#: fresh neb-tier barrier as canonical).
_AUTOCATPATH_MEASURE_KEYS: tuple[str, ...] = (
    "barrier",
    "span",
    "barrier_trusted",
    "barrier_neb_failed",
    "barrier_desorbed",
    "barrier_wrong_site",
    "barrier_low_confidence",
    "barrier_screen",
    "barrier_tier",
    "tier",
    # selectivity/poisoning scalars + context — same engine, same staleness
    *_AUTOCATPATH_SELECTIVITY_KEYS,
    *_AUTOCATPATH_SELECTIVITY_CONTEXT_KEYS,
    # legacy 0.5.2-era keys — still on prod candidates measured before the
    # scorecard swap; resets must clear them too
    "side_span_margin",
    "trap_depth",
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
    # A new candidate's FIRST autocatpath run defaults to the cheap
    # screening tier when the quest opted into the ladder (relax-only
    # thermodynamic ranking before spending a full NEB) — a ladder-off quest
    # (the default for anything not seeded via seed_catalyst_quest, and every
    # pre-ladder quest/test) keeps today's straight-to-neb dispatch.
    initial_tier = (
        _TIER_SCREENING
        if reaction is not None and _tier_ladder_enabled(store, quest_id)
        else _TIER_NEB
    )
    created = dispatched = duplicates = 0
    notes: list[str] = []
    for p in proposals or []:
        if not isinstance(p, dict):
            continue
        sid, was_dup, lineage_note = _ensure_candidate_detail(
            store, quest_id, p, hub=hub
        )
        if sid is None:
            continue
        created += 1
        if was_dup:
            duplicates += 1
        if lineage_note is not None:
            notes.append(lineage_note)
        if dispatch:
            note = dispatch_relax(store, sid, hub=hub, cell=relax_cell)
            notes.append(note)
            if note.startswith("relax["):
                dispatched += 1
            if reaction is not None:
                cnote = dispatch_autocatpath(
                    store, sid, reaction, hub=hub, tier=initial_tier
                )
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

    # Tier-ladder promotion (code-driven, no LLM surface) — mint the NEXT
    # rung's dispatch for whichever candidates earned it. Own try/except (not
    # just the caller's) so a promotion-pass bug never discards an already-
    # successful harvest/graduation result computed above.
    try:
        promo_notes = promote_tiers(store, quest_id, hub=hub, by=by)
    except Exception:
        log.exception("run_compute_step: tier promotion failed for quest %s", quest_id)
        promo_notes = []
    notes.extend(promo_notes)

    return ComputeStep(
        candidates_created=created,
        sims_dispatched=dispatched,
        results_harvested=harvest.results_harvested,
        ruled_out=harvest.ruled_out,
        notes=notes,
        graduated=len(graduated),
        duplicate_proposals=duplicates,
    )


__all__ = [
    "ComputeStep",
    "dispatch_autocatpath",
    "dispatch_relax",
    "ensure_candidate",
    "harvest_measures",
    "promote_tiers",
    "redispatch_candidates",
    "reset_compute",
    "run_compute_step",
]
