"""Barrier trust-gate replay over a pathway's stored trust records.

Read-only diagnostic (gr515871): every input the barrier gate uses is
already stored on the ``pathway`` ref — ``results.trust`` records,
``results.route_steps``, ``results.trust_summary.barrier`` — so the gate
can be *replayed* without new compute and rendered as one row per check
(``view='trust'`` on the pathway kind).

Three seams:

* :func:`route_scope` derives the route's node set from ``route_steps``
  (each step is ``"a->b"``) — the engine never persists ``route_nodes``.
* :func:`replay_barrier_gate` re-derives ``blocked_by`` by
  ``autocatpath.pipeline._trust_summary``'s rule (a ``fail`` at
  ``severity: fatal`` whose ``step`` is a route step or whose ``state`` is
  a route node). Comparing it to the stored set catches a stale or
  hand-edited summary.
* :func:`gate_rows` renders one row per gating check: verdict, an input
  digest, the threshold it was judged against, and how many of its
  records block. A check that emitted no records renders ``not-run``
  with a reason — never ``pass`` (a silent check is not a passing one).

The engine itself is imported lazily and only for ``ENDPOINT_TOL_EV``
(the assumed tolerance when a record predates the ``tol_eV`` stamp,
gr508948): the module imports and tests run on a venv without the
``catalyst`` extra. ``struct_neb_runs.trust`` rows
(``neb-structure-builder.md``) share the record shape, so that item's
``view='neb'`` can render through :func:`gate_rows` too.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
from collections.abc import Iterable
from typing import Any

#: Gating checks in display order — autocatpath.trust.CHECKS minus
#: ``endpoint_identity`` (an input measurement, not a verdict gate).
#: Copied, not imported: the module must work on a venv without the
#: ``catalyst`` extra, and the check vocabulary is schema-pinned (a new
#: check the engine mints still renders — it appends after this list).
GATE_ORDER: tuple[str, ...] = (
    "endpoint_agreement",
    "detachment",
    "neb_convergence",
    "saddle_verified",
    "multistart",
    "relax_convergence",
    "wrong_binder",
    "clash",
    "reconstruction",
    "layer_intact",
    "fragment_merge",
    "hidden_intermediate",
)

#: Fallback when ``autocatpath.trust`` is not importable and no record
#: carries a stamped ``tol_eV`` — the engine constant's long-standing
#: value, rendered labelled ``assumed`` so a reader knows it isn't
#: measured provenance.
_DEFAULT_ENDPOINT_TOL_EV = 0.05

#: Strict-engine ruling (Reto 2026-10-25): only records minted under the
#: pathway's own ``autocatpath_version`` count toward the gate — the
#: endpoint basin comparison across engine versions is apples-to-oranges
#: (a bump can shift absolute energies), and cross-version pooling is out
#: everywhere. A record's engine is read from ``engine`` /
#: ``autocatpath_version`` when the writer stamped one; an unstamped
#: record counts as current (its provenance is unprovable — never
#: fabricate staleness). Stale records never gate; they render
#: ``stale, rerun on current`` instead.
#:
#: Seed independence (gr517804, R6/R7 ruling 2026-10-25): a neb-tier
#: barrier needs ≥2 DISTINCT same-version seeds in the UNION pooled
#: across the run and its ready siblings — one seed leaves
#: ``endpoint_agreement`` silent and the stored summary reads available
#: anyway (the pw336193 vacuous pass). Siblings share ``candidate_ref``
#: + engine + tier + the IS-geometry stamp + the same route/config
#: (:func:`sibling_key`); a sibling that never completed
#: (``meta.status`` other than ``ready``) contributes nothing, and a
#: reused seed counts once — ordering and overlap no longer matter, so
#: pw512178/pw513347 both running seeds 0,1 pool to a 2-seed union.
#: An unknowable seed set — no record ``seed`` fields and no declared
#: ``results.seeds`` — is NOT trusted: the gate emits ``seeds:unknown``,
#: or ``seeds:<n>-independent`` on a shortfall, alongside the record ids.
#:
#: Union agreement (round-2 review R1): counting pooled seeds is not
#: enough — endpoint agreement must be REPLAYED over the union, or a
#: 1-seed run plus a 1-seed sibling passes two vacuous checks and
#: harvests trusted with no comparison ever run.
#: :func:`sibling_evidence` returns the ready siblings' pooled seeds AND
#: their current-engine ``endpoint_identity`` records, and
#: :func:`endpoint_union_blocker` replays the per-state energy spread
#: across the merged seed set against the recorded ``tol_eV`` — fail
#: closed as ``endpoint:union-not-run`` when the union reached ≥2 only
#: via pooling and no cross-seed pair exists, ``endpoint:union-disagree``
#: when it disagrees. Harvest, ``view='trust'`` and the drylist share
#: the one helper.


def record_engine(rec: dict[str, Any]) -> str | None:
    """The engine version a trust record was minted under — ``engine`` or
    ``autocatpath_version``, whichever the writer stamped; ``None`` when
    unstamped (every record predating a stamp)."""
    for key in ("engine", "autocatpath_version"):
        v = rec.get(key)
        if isinstance(v, str) and v:
            return v
    return None


def engine_version_stale(reported: Any, floor: Any) -> bool:
    """Whether evidence minted on ``reported`` reads stale against the
    fleet's current ``floor`` (the env override or dist pin — gr518351
    ruling, 2026-10-26):

    * Both parse as versions (``packaging.version``) → ordering decides:
      ``reported < floor`` is stale, **newer is not** — a fleet running
      ahead of the pin keeps its verdicts.
    * Either is unparseable (an env override used as a re-key token, not
      a version — or a missing stamp) → exact equality, the pre-ruling
      behaviour, so a re-key still re-mints.

    This is the one comparison restale ranking
    (:func:`precis.quest.frontier.apply_network_demotion`), the harvest
    seed gate and ``view='trust'`` share — they must answer
    ``engine:stale`` identically."""
    from packaging.version import InvalidVersion, Version

    try:
        return Version(str(reported)) < Version(str(floor))
    except InvalidVersion:
        return str(reported) != str(floor)


def is_stale(rec: dict[str, Any], current_engine: str | None) -> bool:
    """``True`` when the record carries an engine stamp that differs from
    the pathway's current engine — it does not count toward the gate."""
    if not current_engine:
        return False
    engine = record_engine(rec)
    return engine is not None and engine != current_engine


def run_seeds(
    records: list[dict[str, Any]],
    *,
    engine: str | None = None,
    declared: Any = None,
) -> set[int] | None:
    """The distinct seed ints the run actually sampled at ``engine``:
    non-stale record seeds (a seed is only as real as its records). The
    declared ``results.seeds`` list counts only when NO record carries a
    seed field at all (pre-stamp / record-poor runs) — seeded records that
    are all stale mean the run sampled nothing at this engine, so the set
    is empty rather than the stale-claimed list (gr517804 R6). ``None``
    when neither source exists — unknowable, never guess."""
    seeded = [
        r
        for r in records
        if isinstance(r.get("seed"), int) and not isinstance(r.get("seed"), bool)
    ]
    if not seeded:
        if isinstance(declared, list):
            return {
                int(s)
                for s in declared
                if isinstance(s, int) and not isinstance(s, bool)
            }
        return None
    return {int(r["seed"]) for r in seeded if not is_stale(r, engine)}


def seed_execution_note(meta: dict[str, Any] | None) -> str:
    """Render launch provenance without treating it as independence evidence.

    Missing historical fields remain unknown: result/trust seeds describe
    usable evidence, not every child that started (including failed ones).
    Compare sets because launch order and repeated models do not change
    which requested seeds ran.
    """
    meta = meta or {}

    def seeds(key: str) -> list[int] | None:
        value = meta.get(key)
        if isinstance(value, list) and all(type(s) is int for s in value):
            return value
        return None

    requested = seeds("requested_seeds")
    executed = seeds("executed_seeds")

    def show(value: list[int] | None) -> str:
        return "unknown" if value is None else json.dumps(value, separators=(",", ":"))

    note = f"(requested {show(requested)}, executed {show(executed)})"
    if (
        requested is not None
        and executed is not None
        and set(requested) != set(executed)
    ):
        note += "; dispatch dropped or substituted seeds"
    return note


def seed_blocker(
    records: list[dict[str, Any]],
    *,
    engine: str | None = None,
    declared: Any = None,
    pooled: Iterable[int] = (),
    tier: str = "neb",
) -> str | None:
    """The synthetic blocker id for the seed-independence gate
    (gr517804 R6/R7): ``None`` when ≥2 distinct same-version seeds ran
    across the run plus its ready siblings (``pooled`` — union semantics,
    a reused seed counts once) or when the rule doesn't apply (it is
    neb-tier only); ``"seeds:unknown"`` when the run's own seed set is
    unknowable (no record ``seed`` fields and no declared list —
    unprovable is untrusted); otherwise ``"seeds:<n>-independent"`` with
    ``n`` the union size."""
    if tier != "neb":
        return None
    run = run_seeds(records, engine=engine, declared=declared)
    if run is None:
        return "seeds:unknown"
    n = len(run | {int(s) for s in pooled})
    if n >= 2:
        return None
    return f"seeds:{n}-independent"


def seed_row(
    records: list[dict[str, Any]],
    *,
    engine: str | None = None,
    declared: Any = None,
    pooled: Iterable[int] = (),
    tier: str = "neb",
    lookup_failed: bool = False,
) -> dict[str, Any]:
    """The gate-table row for the seed-independence rule — a precis-side
    gate like the absurd/twin rows: it renders for every pathway, counts
    ``blocking`` when it fails, and fails closed on an unknowable seed
    set (R7) — ``not-run`` only for a non-neb tier or a sibling-lookup
    failure, which must never read as a pass."""
    pooled_set = {int(s) for s in pooled}
    run = run_seeds(records, engine=engine, declared=declared)
    extra = pooled_set - (run or set())
    note = (
        f"; s{', s'.join(str(s) for s in sorted(extra))} pooled from ready sibling runs"
        if extra
        else ""
    )
    n = len((run or set()) | pooled_set)
    if tier != "neb":
        verdict, input_ = "not-run", f"rule is neb-tier only (tier={tier})"
        blocking: int | str = 0
    elif lookup_failed:
        verdict, input_, blocking = (
            "not-run",
            "sibling lookup failed — pooled seeds unprovable; retry the view",
            1,
        )
    elif run is None:
        verdict, input_, blocking = (
            "fail",
            "seed set unknowable (no record seed fields, no declared seeds)",
            1,
        )
    elif n >= 2:
        verdict, input_, blocking = "pass", f"{n} independent seeds{note}", 0
    else:
        verdict, input_, blocking = (
            "fail",
            f"{n} independent (needs ≥ 2){note}",
            1,
        )
    return {
        "gate": "seed independence (precis)",
        "verdict": verdict,
        "input": input_,
        "threshold": "≥ 2 distinct seeds, same engine (sibling union)",
        "blocking": blocking,
    }


def _geometry_id(meta: dict[str, Any]) -> str | None:
    """The same-geometry half of the sibling key: the exported slab's sha
    (``validation_manifest.inputs.slab_extxyz_sha256`` — the injected
    initial-structure geometry) when the manifest carried it; else the
    ``structure_refs`` value set, which is geometry-content-addressed
    (the whole-map compare is stricter than IS-only but is the only
    stored identity older pathways carry). ``None`` → the pathway can't
    prove its geometry and pools with itself only."""
    vm = meta.get("validation_manifest")
    inputs = vm.get("inputs") if isinstance(vm, dict) else None
    sha = inputs.get("slab_extxyz_sha256") if isinstance(inputs, dict) else None
    if isinstance(sha, str) and sha:
        return f"slab:{sha}"
    refs = meta.get("structure_refs")
    if isinstance(refs, dict):
        ids = sorted({str(v) for v in refs.values() if v is not None})
        if ids:
            return "refs:" + ",".join(ids)
    return None


def _reaction_id(meta: dict[str, Any]) -> str | None:
    """The same-route/config half of the sibling key: ``meta.reaction_key``
    (the dispatch stamp of the canonical spec — see
    ``compute._autocatpath_reaction_key``) wins; a pre-stamp pathway falls
    back to a hash of its stored normalized ``meta.config``. ``None`` → no
    provable route identity."""
    rk = meta.get("reaction_key")
    if isinstance(rk, str) and rk:
        return f"rk:{rk}"
    config = meta.get("config")
    if isinstance(config, dict) and config:
        return (
            "cfg:"
            + hashlib.sha256(json.dumps(config, sort_keys=True).encode()).hexdigest()
        )
    return None


def sibling_key(meta: dict[str, Any]) -> tuple[str, str, str, str, str] | None:
    """``(candidate_ref, engine, tier, geometry, reaction)`` — what two
    pathways must share to pool seeds into the union (gr517804 R6). One
    function shared by the harvest, the view and the drylist. ``None``
    when any half can't be proven from stored meta — empty
    ``candidate_ref``/engine, no geometry stamp, no route identity — so
    the pathway pools with itself only rather than misgrouping."""
    cand = meta.get("candidate_ref")
    engine = meta.get("autocatpath_version")
    if cand in (None, "") or not isinstance(engine, str) or not engine:
        return None
    geom = _geometry_id(meta)
    rxn = _reaction_id(meta)
    if geom is None or rxn is None:
        return None
    from precis.quest.compute import _pathway_tier

    return (str(cand), engine, _pathway_tier(meta), geom, rxn)


def _sibling_metas(
    store: Any,
    *,
    meta: dict[str, Any],
    exclude_ref_id: int | None = None,
) -> tuple[str, list[dict[str, Any]]]:
    """``(engine, ready same-key sibling metas)`` — the one store scan
    behind :func:`sibling_seeds` and :func:`sibling_evidence`. The query
    narrows by ``candidate_ref`` + engine and the full
    :func:`sibling_key` is re-checked per fetched meta (tier, geometry,
    route); a sibling that never completed is dropped here.
    ``("", [])`` when this pathway's own key can't be formed."""
    key = sibling_key(meta)
    if key is None:
        return "", []
    cand, engine = key[0], key[1]
    with store.pool.connection() as conn:
        rows = conn.execute(
            "SELECT ref_id FROM refs WHERE kind = 'pathway' "
            "AND retired_at IS NULL AND meta->>'candidate_ref' = %s "
            "AND meta->>'autocatpath_version' = %s",
            (cand, engine),
        ).fetchall()
    ids = {int(r[0]) for r in rows}
    if exclude_ref_id is not None:
        ids.discard(int(exclude_ref_id))
    if not ids:
        return engine, []
    metas: list[dict[str, Any]] = []
    for ref in store.fetch_refs_by_ids(ids).values():
        smeta = ref.meta if isinstance(ref.meta, dict) else {}
        if smeta.get("status") != "ready" or sibling_key(smeta) != key:
            continue
        metas.append(smeta)
    return engine, metas


def sibling_evidence(
    store: Any,
    *,
    meta: dict[str, Any],
    exclude_ref_id: int | None = None,
) -> tuple[set[int], list[dict[str, Any]]]:
    """The union material this pathway's READY same-key siblings
    contribute (gr517804 R6 + round-2 R1): ``(pooled seeds, pooled
    endpoint_identity records)``. Each returned record carries a ``seed``
    the sibling actually ran (:func:`run_seeds` rules apply per sibling)
    and is non-stale at the shared engine — the material
    :func:`endpoint_union_blocker` replays agreement over. ``(set(),
    [])`` when this pathway's key can't be formed."""
    engine, metas = _sibling_metas(store, meta=meta, exclude_ref_id=exclude_ref_id)
    seeds: set[int] = set()
    identity: list[dict[str, Any]] = []
    for smeta in metas:
        sresults = smeta.get("results")
        sresults = sresults if isinstance(sresults, dict) else {}
        srecs = [r for r in sresults.get("trust") or [] if isinstance(r, dict)]
        run = run_seeds(srecs, engine=engine, declared=sresults.get("seeds"))
        if not run:
            continue
        seeds |= run
        identity += [
            r
            for r in srecs
            if r.get("check") == "endpoint_identity"
            and r.get("seed") in run
            and not is_stale(r, engine)
        ]
    return seeds, identity


def sibling_seeds(
    store: Any,
    *,
    meta: dict[str, Any],
    exclude_ref_id: int | None = None,
) -> set[int]:
    """The distinct same-engine seeds this pathway's READY siblings
    already ran — the pool the seed-independence gate unions with the
    run's own seeds (gr517804 R6). Siblings share :func:`sibling_key`; a
    sibling that never completed (``meta.status`` other than ``ready``)
    contributes nothing, as does one whose seed evidence is unknowable or
    stale (:func:`run_seeds` rules apply per sibling). Union semantics
    make ordering irrelevant — the earlier/later ``ref_id`` claim model
    is gone. ``set()`` when this pathway's own key can't be formed."""
    return sibling_evidence(store, meta=meta, exclude_ref_id=exclude_ref_id)[0]


def endpoint_union_blocker(
    records: list[dict[str, Any]],
    sibling_identity: list[dict[str, Any]],
    scope: tuple[set[str] | None, set[str]],
    *,
    run: set[int] | None,
    union: set[int],
    engine: str | None = None,
    tol_eV: float,
) -> tuple[str | None, str]:
    """Replay endpoint agreement over the sibling union (gr517804
    round-2 R1): counting pooled seeds is not enough — the merged
    ``endpoint_identity`` evidence must show the union's seeds agreeing
    on each route state.

    Reads the pathway's own trust ``records`` plus ``sibling_identity``
    (:func:`sibling_evidence`'s second half, or the drylist's equivalent
    column), keeps ``endpoint_identity`` records on the route whose
    ``seed`` is in ``union``, and spreads each state's ``e_eV`` across
    every contributing seed — one state disagreeing beyond ``tol_eV``
    fails. Returns ``(blocker, input-text)``:

    * ``endpoint:union-disagree`` — a route state's merged energies span
      wider than ``tol_eV`` (names the worst state and its delta);
    * ``endpoint:union-not-run`` — the union reached ≥2 only via pooling
      (``run`` shorter than 2) and some route state with identity
      evidence has no cross-seed pair, so agreement is unprovable — the
      vacuous-pass shape one level up;
    * ``None`` — the merged evidence agrees (or the run stands on ≥2 own
      seeds, where the engine's own agreement governs).
    """
    by_state: dict[Any, dict[int, list[float]]] = {}
    for r in [*records, *sibling_identity]:
        if r.get("check") != "endpoint_identity" or not _on_route(r, scope):
            continue
        seed = r.get("seed")
        if not isinstance(seed, int) or isinstance(seed, bool) or seed not in union:
            continue
        state = r.get("state")
        if not state or is_stale(r, engine):
            continue
        ev = r.get("evidence")
        e = ev.get("e_eV") if isinstance(ev, dict) else None
        if not isinstance(e, int | float) or isinstance(e, bool):
            continue
        by_state.setdefault(state, {}).setdefault(seed, []).append(float(e))
    worst: tuple[float, Any] | None = None
    thin = sorted(str(s) for s, per in by_state.items() if len(per) < 2)
    for state, per_seed in by_state.items():
        if len(per_seed) < 2:
            continue
        es = [e for vals in per_seed.values() for e in vals]
        delta = max(es) - min(es)
        if delta > tol_eV and (worst is None or delta > worst[0]):
            worst = (delta, state)
    if worst is not None:
        delta, state = worst
        return (
            "endpoint:union-disagree",
            f"union replay: {state} endpoints {delta * 1000:g} meV apart "
            f"across seeds (tol {tol_eV:g} eV)",
        )
    if (run is None or len(run) < 2) and (thin or not by_state):
        missing = (
            f"no cross-seed pair on {', '.join(thin)}"
            if thin
            else "no endpoint_identity records"
        )
        return (
            "endpoint:union-not-run",
            f"union replay: {missing} — pooled-seed agreement unprovable",
        )
    n_states = sum(1 for per in by_state.values() if len(per) >= 2)
    return (
        None,
        f"union replay: {n_states} route states agree across "
        f"{len(union)} pooled seeds (tol {tol_eV:g} eV)",
    )


@dataclasses.dataclass(frozen=True)
class Thresholds:
    """The numbers each gate was judged against."""

    endpoint_tol_ev: float
    endpoint_tol_stamped: bool
    absurd_ev: float
    twin_tol_ev: float
    energy_thresh_ev: float | None


def endpoint_tol_ev() -> float:
    """The engine's endpoint-agreement tolerance, lazily; the baked-in
    default when autocatpath is absent."""
    try:
        from autocatpath.trust import ENDPOINT_TOL_EV
    except Exception:  # no catalyst extra, or an engine too old to carry it
        return _DEFAULT_ENDPOINT_TOL_EV
    return float(ENDPOINT_TOL_EV)


def thresholds_from(
    records: list[dict[str, Any]],
    config: dict[str, Any] | None,
) -> Thresholds:
    """Resolve display thresholds: the ``tol_eV`` stamped on an
    ``endpoint_agreement`` record wins (it is what the run was actually
    judged against); otherwise the engine constant, flagged assumed."""
    stamped: float | None = None
    for r in records:
        if r.get("check") != "endpoint_agreement":
            continue
        ev = r.get("evidence")
        tol = ev.get("tol_eV") if isinstance(ev, dict) else None
        if isinstance(tol, int | float) and not isinstance(tol, bool):
            stamped = float(tol)
            break
    from precis.quest.compute import _TWIN_BARRIER_TOL_EV
    from precis.quest.frontier import _BARRIER_ABSURD_EV

    search = (config or {}).get("search") or {}
    thresh = search.get("energy_thresh")
    return Thresholds(
        endpoint_tol_ev=stamped if stamped is not None else endpoint_tol_ev(),
        endpoint_tol_stamped=stamped is not None,
        absurd_ev=float(_BARRIER_ABSURD_EV),
        twin_tol_ev=float(_TWIN_BARRIER_TOL_EV),
        energy_thresh_ev=(
            float(thresh)
            if isinstance(thresh, int | float) and not isinstance(thresh, bool)
            else None
        ),
    )


def route_scope(
    results: dict[str, Any],
) -> tuple[set[str] | None, set[str]]:
    """``(route_steps, route_nodes)`` — the node set derived from each
    ``"a->b"`` route step (the engine derives it the same way and never
    stores it).

    ``route_steps`` is ``None`` when the results payload doesn't carry the
    key at all (older engine): callers then treat every fatal fail as
    on-route — the same convention ``_trust_context`` documents for the
    warnings view.
    """
    raw = results.get("route_steps")
    if not isinstance(raw, list):
        return None, set()
    steps = {str(s) for s in raw if isinstance(s, str)}
    nodes: set[str] = set()
    for s in steps:
        a, sep, b = s.partition("->")
        if sep:
            nodes.update((a, b))
    return steps, nodes


def _on_route(
    rec: dict[str, Any],
    scope: tuple[set[str] | None, set[str]],
) -> bool:
    """``_trust_summary``'s membership rule: the record's step is a route
    step OR its state is a route node. ``state``-only records (the
    ``endpoint_agreement`` records carry no step) are on-route through
    their state."""
    steps, nodes = scope
    if steps is None:
        return True
    return rec.get("step") in steps or rec.get("state") in nodes


def replay_barrier_gate(
    records: list[dict[str, Any]],
    scope: tuple[set[str] | None, set[str]],
    *,
    engine: str | None = None,
) -> list[str]:
    """The barrier blocked-set, recomputed from stored records by
    ``autocatpath.pipeline._trust_summary``'s rule: ids of ``fail`` +
    ``severity: fatal`` records on the route, sorted. ``["no_route"]``
    when the route exists but is empty — the engine's own sentinel.
    Records stamped to a different engine version (:func:`is_stale`)
    never count."""
    steps, _nodes = scope
    if steps is not None and not steps:
        return ["no_route"]
    return sorted(
        {
            str(r["id"])
            for r in records
            if r.get("verdict") == "fail"
            and r.get("severity") == "fatal"
            and r.get("id") is not None
            and _on_route(r, scope)
            and not is_stale(r, engine)
        }
    )


#: Evidence keys a record diagnostic renders as MEASURED values, in
#: display order (gr522897): what the check actually read — energy deltas,
#: counts, forces, curvatures. A record's other scalar evidence keys
#: append after these (a check minting an unstamped key still shows it).
_MEASURE_KEYS: tuple[str, ...] = (
    "delta_meV",
    "e_eV",
    "e_min_eV",
    "spread_eV",
    "barrier_eV",
    "n_valid",
    "n_at_min",
    "n_seeds",
    "n_imag",
    "imag_cm1",
    "collapsed_images",
    "desorbed_images",
    "ts_fmax",
    "curvature_eV",
    "converged",
    "d_nn_A",
    "disp_max_A",
    "bonds_broken",
    "bonds_formed",
)

#: Evidence keys naming the TOLERANCE the record was judged against —
#: stamped at mint (gr508948 stamped ``tol_eV``; ``bound_eV``/
#: ``cutoff_cm1`` ride the precis-side records) — with the short label
#: and unit each renders under. ``fmax`` is deliberately not here: on
#: engine records it is the convergence knob the run carried, not a
#: trust threshold — it renders under measured.
_TOL_KEYS: dict[str, tuple[str, str]] = {
    "tol_eV": ("tol", "eV"),
    "bound_eV": ("bound", "eV"),
    "cutoff_cm1": ("cutoff", "cm⁻¹"),
}

#: The rule label a check was judged under when no tolerance key is
#: stamped — the check's stated criterion, not a number we invent.
_CHECK_RULE: dict[str, str] = {
    "endpoint_agreement": "Δ endpoint energy across seeds",
    "detachment": "no mid-band fragment > 3.5 Å off slab (engine bound)",
    "saddle_verified": "TS interior, curvature < 0, climb residual ≤ 2·fmax",
    "neb_convergence": "converged",
    "collapsed_band": "0 collapsed or desorbed images",
    "multistart": "≥ 2 valid seeds at the minimum",
    "absurd_magnitude": "|barrier| within bound",
    "frequency": "exactly 1 imaginary mode past cutoff",
}

#: Record-level and evidence keys naming WHICH subject failed — a route
#: state, a band image, a seed, an IS/FS endpoint (gr522897: a refused
#: gate must say which endpoint/image, not just the check).
_WHERE_TOP: tuple[str, ...] = ("state", "step", "seed")
_WHERE_EV: tuple[str, ...] = ("image", "ts_image", "init_seed", "endpoint")

#: Evidence keys a diagnostic never double-reports — prose already
#: rendered, or a nested structure handled on its own.
_DIAG_SKIP: frozenset[str] = frozenset(
    {"reason", "detail", "events", "fingerprint", "model"}
)


def _fmt_ev_value(v: Any) -> str:
    return f"{v:g}" if isinstance(v, float) else str(v)


def record_diagnostic(rec: dict[str, Any]) -> dict[str, str]:
    """One trust record as a diagnostic row (gr522897): ``measured``
    (the values the check read), ``tol`` (the stamped tolerance, else the
    check's rule label — ``assumed`` never gets invented), ``where``
    (the route state / seed / band image that failed), ``reason`` (the
    minted prose). A caller can render a refusal as
    ``check verdict — measured vs tol — where — reason`` and a reader can
    tell a real disagreement from tolerance noise without opening the
    raw record."""
    ev = rec.get("evidence")
    ev = ev if isinstance(ev, dict) else {}
    parts: dict[str, list[str]] = {"measured": [], "tol": [], "where": []}
    for k in _MEASURE_KEYS:
        if k in ev and ev[k] is not None:
            parts["measured"].append(f"{k}={_fmt_ev_value(ev[k])}")
    for k, (label, unit) in _TOL_KEYS.items():
        if k in ev and ev[k] is not None:
            parts["tol"].append(f"{label} {_fmt_ev_value(ev[k])} {unit}")
    for k in _WHERE_TOP:
        v = rec.get(k)
        if v is not None:
            parts["where"].append(f"s{v}" if k == "seed" else str(v))
    for k in _WHERE_EV:
        if k in ev and ev[k] is not None:
            parts["where"].append(f"{k} {ev[k]}")
    events = ev.get("events")
    if isinstance(events, list):
        for e in events:
            if isinstance(e, dict):
                parts["where"].append(
                    f"img{e.get('image')} {e.get('fragment', '')}".rstrip()
                    + (f" {e['closest_A']} Å" if e.get("closest_A") is not None else "")
                )
    measured_keys = set(_MEASURE_KEYS)
    for k, v in ev.items():
        if (
            k in measured_keys
            or k in _TOL_KEYS
            or k in _WHERE_EV
            or k in _DIAG_SKIP
            or isinstance(v, bool)
            or not isinstance(v, (int, float, str))
        ):
            continue
        parts["measured"].append(f"{k}={_fmt_ev_value(v)}")
    tol = ", ".join(parts["tol"]) or _CHECK_RULE.get(str(rec.get("check")), "")
    return {
        "check": str(rec.get("check") or "?"),
        "verdict": str(rec.get("verdict") or "?"),
        "measured": ", ".join(parts["measured"]),
        "tol": tol,
        "where": "; ".join(parts["where"]),
        "reason": str(ev.get("reason") or ev.get("detail") or ""),
    }


def record_line(rec: dict[str, Any], *, sep: str = " ") -> str:
    """The one-line refusal digest: ``check<sep>verdict — measured (vs tol)
    — where — reason``, skipping empty slots. ``sep=': '`` renders the
    ``check: verdict`` head the harvest warnings list is pinned to."""
    d = record_diagnostic(rec)
    tail = d["measured"]
    if d["tol"]:
        tail = f"{tail} (vs {d['tol']})" if tail else f"vs {d['tol']}"
    if d["where"]:
        tail = f"{tail} — {d['where']}" if tail else d["where"]
    if d["reason"]:
        tail = f"{tail} — {d['reason']}" if tail else d["reason"]
    return f"{d['check']}{sep}{d['verdict']}" + (f" — {tail}" if tail else "")


def _subject(rec: dict[str, Any]) -> str:
    state, step = rec.get("state"), rec.get("step")
    if state and step:
        return f"{state}@{step}"
    return str(state or step or "?")


def _ev_short(rec: dict[str, Any], keys: tuple[str, ...]) -> str:
    ev = rec.get("evidence")
    if not isinstance(ev, dict):
        return ""
    return ", ".join(f"{k}={ev[k]}" for k in keys if k in ev and ev[k] is not None)


def _n_seeds(records: list[dict[str, Any]], results: dict[str, Any]) -> int:
    seeds = results.get("seeds")
    if isinstance(seeds, list) and seeds:
        return len(seeds)
    return len({r.get("seed") for r in records if r.get("seed") is not None})


def _check_verdict(
    recs: list[dict[str, Any]],
    scope: tuple[set[str] | None, set[str]],
    blocked: set[str],
) -> str:
    if not recs:
        return "not-run"
    if any(str(r.get("id")) in blocked for r in recs):
        return "fail"
    on_route = [r for r in recs if _on_route(r, scope)]
    if any(r.get("verdict") in ("fail", "marginal") for r in on_route):
        return "warn"
    return "pass"


def _not_run_reason(
    check: str,
    records: list[dict[str, Any]],
    scope: tuple[set[str] | None, set[str]],
    results: dict[str, Any],
) -> str:
    n_seeds = _n_seeds(records, results)
    if check == "endpoint_agreement" and n_seeds < 2:
        return "1 seed: agreement needs ≥ 2"
    if check == "endpoint_agreement":
        return "no endpoint_identity records"
    if check == "saddle_verified":
        return "no band"
    return "no records emitted"


def _endpoint_input(
    recs: list[dict[str, Any]],
    scope: tuple[set[str] | None, set[str]],
    results: dict[str, Any],
    blocked: set[str],
) -> str:
    steps, nodes = scope
    n_states = (
        len(nodes)
        if steps is not None
        else len({r.get("state") for r in recs if r.get("state")})
    )
    n_seeds = _n_seeds(recs, results)
    fails = [
        r for r in recs if r.get("verdict") == "fail" and str(r.get("id")) in blocked
    ]
    states_split = {r.get("state") for r in fails}
    out = f"{n_states} route states × {n_seeds} seeds"
    if fails:
        worst = max(
            fails,
            key=lambda r: (r.get("evidence") or {}).get("delta_meV") or 0.0,
        )
        delta = (worst.get("evidence") or {}).get("delta_meV")
        out += (
            f"; {len(states_split)} state{'s' if len(states_split) != 1 else ''} split; worst "
            f"{worst.get('state')} s{worst.get('seed')} +{delta} meV"
        )
    return out


def _generic_input(
    check: str,
    recs: list[dict[str, Any]],
    scope: tuple[set[str] | None, set[str]],
    blocked: set[str],
) -> str:
    steps, _ = scope
    on_route = [r for r in recs if _on_route(r, scope)]
    n_bad = sum(1 for r in recs if str(r.get("id")) in blocked)
    if check == "neb_convergence":
        n_steps = (
            len(steps)
            if steps is not None
            else len({r.get("step") for r in recs if r.get("step")})
        )
        n_seeds = len({r.get("seed") for r in recs if r.get("seed") is not None})
        out = f"{n_steps} route steps × {n_seeds} seeds"
    elif check == "saddle_verified":
        counts: dict[str, int] = {}
        for r in on_route:
            v = str(r.get("verdict") or "?")
            counts[v] = counts.get(v, 0) + 1
        out = " · ".join(f"{n} {v}" for v, n in sorted(counts.items())) or "0"
    elif check == "multistart":
        n_zero = sum(1 for r in recs if not ((r.get("evidence") or {}).get("n_valid")))
        out = (
            f"{len(recs)} records, all n_valid ≥ 1"
            if not n_zero
            else (f"{len(recs)} records, {n_zero} with n_valid = 0")
        )
    else:
        out = (
            f"{len(on_route)} on-route records"
            if on_route
            else (f"{len(recs)} records (none on route)")
        )
    if n_bad:
        first = next(r for r in recs if str(r.get("id")) in blocked)
        detail = _ev_short(
            first,
            ("delta_meV", "tol_eV", "image", "closest_A", "reason", "fmax"),
        )
        out += f"; first blocker {_subject(first)} s{first.get('seed')}" + (
            f": {detail}" if detail else ""
        )
    return out


def gate_rows(
    records: list[dict[str, Any]],
    scope: tuple[set[str] | None, set[str]],
    thresholds: Thresholds,
    results: dict[str, Any] | None = None,
    *,
    engine: str | None = None,
) -> list[dict[str, Any]]:
    """One row per gating check: ``gate``, ``verdict``, ``input`` digest,
    ``threshold``, ``blocking`` count of this check's records in the
    replayed blocked set. A silent check is ``not-run`` with a reason —
    never ``pass``. Checks the engine emitted but this list doesn't know
    append at the end rather than disappearing. Records stamped to an
    engine version other than ``engine`` are stale — they don't count,
    and the input digest says how many were set aside."""
    results = results if isinstance(results, dict) else {}
    live = [r for r in records if not is_stale(r, engine)]
    stale_by_check: dict[str, list[dict[str, Any]]] = {}
    for r in records:
        if is_stale(r, engine):
            stale_by_check.setdefault(str(r.get("check")), []).append(r)
    blocked = set(replay_barrier_gate(records, scope, engine=engine))
    checks = list(GATE_ORDER)
    seen = {r.get("check") for r in records}
    checks += sorted(
        c for c in seen if c and c not in checks and c != "endpoint_identity"
    )

    rows: list[dict[str, Any]] = []
    for check in checks:
        recs = [r for r in live if r.get("check") == check]
        stale = stale_by_check.get(check, [])
        verdict = _check_verdict(recs, scope, blocked)
        n_block = sum(1 for r in recs if str(r.get("id")) in blocked)
        if not recs:
            input_ = (
                _not_run_reason(check, records, scope, results) if not stale else ""
            )
        elif check == "endpoint_agreement":
            input_ = _endpoint_input(recs, scope, results, blocked)
        else:
            input_ = _generic_input(check, recs, scope, blocked)
        if stale:
            engines = "+".join(sorted({record_engine(r) or "?" for r in stale}))
            tail = f"{len(stale)} stale, engine {engines}"
            input_ = f"{input_}; {tail}" if input_ else tail
        if check == "endpoint_agreement":
            src = "stamped" if thresholds.endpoint_tol_stamped else "assumed"
            threshold = f"tol {thresholds.endpoint_tol_ev:g} eV ({src})"
        elif check == "neb_convergence":
            threshold = "converged"
        elif check == "saddle_verified":
            threshold = "ts not at endpoint, curvature < 0"
        elif check == "multistart":
            threshold = "n_valid ≥ 1"
        else:
            threshold = "any fatal fail"
        rows.append(
            {
                "gate": check,
                "verdict": verdict,
                "input": input_,
                "threshold": threshold,
                "blocking": n_block,
            }
        )
    return rows


def precis_guard_rows(
    candidate_meta: dict[str, Any] | None,
    *,
    barrier_ea: float | None,
    rate_limit_std: float | None,
    low_confidence: bool,
    thresholds: Thresholds,
    engine: str | None = None,
    current_engine: str | None = None,
) -> list[dict[str, Any]]:
    """The precis-side rows: the absurd-magnitude guard and the
    twin-disagreement guard (both read from the candidate's stamped meta —
    ``meta.candidate_ref``), the engine-staleness guard (the pathway's
    ``autocatpath_version`` vs the env-or-pinned current version — the
    same comparison the harvest's seed gate demotes on, so view='trust'
    answers ``engine:stale`` exactly when the harvest does), and the
    seed-spread readout that rides along for information and never
    gates."""
    cand = candidate_meta or {}
    rows: list[dict[str, Any]] = []
    if barrier_ea is None:
        rows.append(
            {
                "gate": "absurd magnitude (precis)",
                "verdict": "not-run",
                "input": "no barrier value",
                "threshold": f"≤ {thresholds.absurd_ev} eV",
                "blocking": 0,
            }
        )
    else:
        absurd = bool(cand.get("barrier_absurd"))
        rows.append(
            {
                "gate": "absurd magnitude (precis)",
                "verdict": "fail" if absurd else "pass",
                "input": f"|{barrier_ea:g}| eV",
                "threshold": f"≤ {thresholds.absurd_ev} eV",
                "blocking": 1 if absurd else 0,
            }
        )
    twin = cand.get("barrier_twin_disagreement")
    if twin:
        rows.append(
            {
                "gate": "twin disagreement (precis)",
                "verdict": "fail",
                "input": f"twin {twin} disagrees",
                "threshold": f"≤ {thresholds.twin_tol_ev} eV",
                "blocking": 1,
            }
        )
    else:
        geom = cand.get("geom_hash_c")
        tier = cand.get("barrier_fidelity") or cand.get("tier")
        detail = (
            f"no geom_hash_c twin stamped at tier {tier}"
            if geom and tier
            else "no twin disagreement stamped"
        )
        rows.append(
            {
                "gate": "twin disagreement (precis)",
                "verdict": "not-run",
                "input": detail,
                "threshold": f"≤ {thresholds.twin_tol_ev} eV",
                "blocking": 0,
            }
        )
    if engine is None:
        eng_v, eng_in = "not-run", "no autocatpath_version stamp"
    elif current_engine is None:
        eng_v, eng_in = "not-run", "no env override or dist pin to compare"
    elif engine_version_stale(engine, current_engine):
        eng_v, eng_in = (
            "fail",
            f"engine {engine}, current is {current_engine} — rerun to trust",
        )
    else:
        eng_v, eng_in = (
            "pass",
            f"engine {engine} not older than current {current_engine}",
        )
    rows.append(
        {
            "gate": "engine staleness (precis)",
            "verdict": eng_v,
            "input": eng_in,
            "threshold": (
                "engine not older than env-or-pin (non-version token: ≠ ⇒ stale)"
            ),
            "blocking": 1 if eng_v == "fail" else 0,
        }
    )
    spread_v = "warn" if low_confidence else "pass"
    if rate_limit_std is None:
        spread_v, spread_in = "not-run", "no spread recorded"
    else:
        spread_in = f"std {rate_limit_std:g} eV on the rate-limiting step"
    rows.append(
        {
            "gate": "spread (info, never gates)",
            "verdict": spread_v,
            "input": spread_in,
            "threshold": (
                f"energy_thresh {thresholds.energy_thresh_ev:g} eV"
                if thresholds.energy_thresh_ev is not None
                else "energy_thresh (unset)"
            ),
            "blocking": "—",
        }
    )
    return rows


def endpoint_rows(
    records: list[dict[str, Any]],
    scope: tuple[set[str] | None, set[str]],
    *,
    engine: str | None = None,
) -> tuple[list[dict[str, Any]], int]:
    """The per-state, per-seed endpoint table: one row per
    ``endpoint_identity`` record on a route state, annotated with the
    ``endpoint_agreement`` record's ``delta_meV``/verdict when one was
    minted for it. Failing states first; states whose seeds all agree on
    one fingerprint fold into the returned count instead of a row.
    Records stamped to another engine (:func:`is_stale`) render
    ``stale, rerun on current``, and a state touched by staleness never
    folds into the agree-count."""
    identity = [
        r
        for r in records
        if r.get("check") == "endpoint_identity"
        and r.get("state")
        and _on_route(r, scope)
    ]
    agree: dict[tuple[Any, Any, Any], dict[str, Any]] = {}
    for r in records:
        if r.get("check") == "endpoint_agreement" and r.get("state"):
            agree[(r.get("model"), r.get("state"), r.get("seed"))] = r
            agree.setdefault((None, r.get("state"), r.get("seed")), r)

    failing_states = {
        r.get("state")
        for r in agree.values()
        if r.get("verdict") == "fail" and not is_stale(r, engine)
    }
    stale_states = {
        r.get("state") for r in identity + list(agree.values()) if is_stale(r, engine)
    }
    rows: list[dict[str, Any]] = []
    folded = 0
    by_state: dict[Any, list[dict[str, Any]]] = {}
    for r in identity:
        by_state.setdefault(r["state"], []).append(r)
    for state, recs in by_state.items():
        fps = {(r.get("evidence") or {}).get("fingerprint") for r in recs}
        if state not in failing_states and state not in stale_states and len(fps) <= 1:
            folded += 1
            continue
        for r in sorted(recs, key=lambda x: (x.get("seed") or 0,)):
            a = agree.get((r.get("model"), state, r.get("seed"))) or agree.get(
                (None, state, r.get("seed"))
            )
            ev = r.get("evidence") or {}
            aev = (a or {}).get("evidence") or {}
            stale = is_stale(r, engine) or (a is not None and is_stale(a, engine))
            rows.append(
                {
                    "state": state,
                    "seed": r.get("seed"),
                    "e_eV": ev.get("e_eV"),
                    "Δ meV": aev.get("delta_meV", "—"),
                    "fingerprint": ev.get("fingerprint", "?"),
                    "verdict": (
                        "stale, rerun on current"
                        if stale
                        else (a or {}).get("verdict", "not compared")
                    ),
                }
            )
    rows.sort(
        key=lambda row: (
            row["state"] not in failing_states,
            str(row["state"]),
            row["seed"] if isinstance(row["seed"], int) else 0,
        )
    )
    return rows, folded
