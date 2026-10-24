"""The ``wall_rate`` op — the catpath → ``wall-rate-constant`` binding of
docs/backlog/se-multiphysics.md slice 1.

Handler-level because it reads the store (the same reason
``layout_chain`` is): ``{"op":"wall_rate","block":…,"datum":"patch:…",
"source":"quest:<id>","species":…,"roughness"?:1.0}`` finds a trusted
catpath solve among the refs that ``serves`` the quest — trusted is the
presence of ``log_tof`` — and writes a ``<species>/wall-rate-constant``
measure on the patch with ``origin='proposed'``.

The rate per site-area is ``r = TOF/(A_site·N_A)`` mol m⁻² s⁻¹ with
``A_site = site_area_A2·1e-20`` m² (Å²→m² made explicit here; one
top-layer atom = 1 site). The first-order constant is the secant at the
solve's stated reactant partial pressure ``p``: ``k_s = r/c_wall`` with
``c_wall = p·1e5/(R T)`` (bar→Pa explicit; gas phase only — liquid-phase
rates are refused, no solvation model, spec Q3). A stated ``roughness``
multiplies ``k_s``.

Langmuir–Hinshelwood rates are not first order, so the binding measures
the apparent order: two more catpath solves at ``p(1 ± δ)``, ``δ = 0.1``,
give ``α = ln(TOF₊/TOF₋)/ln((1+δ)/(1−δ))``. ``|α−1| ≤ 0.2`` holds the
first-order form; outside that the card's ``apparent_order`` line limits
``k_s`` to conversion X ≲ 0.2. The probe solves go through the
``_solve_tof`` seam — a stored solve record cannot carry the kinetics
machinery, so an unprobed measure records ``alpha: null``.
``log_tof_p5``/``log_tof_p95`` give the measure's ``min``/``max`` band
(the conversion band reads off those endpoints downstream).

Refusals carry a reason, never a silent number: no trusted solve,
``tof ≤ 0`` (net reverse / failed solve), missing site area, T or p, a
liquid-phase source.
"""

from __future__ import annotations

import math
from typing import Any

from precis.errors import BadInput
from precis_se.measures import SCOPE_SEP
from precis_se.ops import OpError, apply_ops

R_GAS = 8.314462618  # J/(mol·K)
N_A = 6.02214076e23  # /mol
DELTA = 0.1  # the apparent-order pressure perturbation

_MEASURAND = "wall-rate-constant"


def _num(v: Any) -> float | None:
    if isinstance(v, bool) or not isinstance(v, int | float):
        return None
    return float(v)


def _solve_tof(record: dict[str, Any], p_bar: float) -> float | None:
    """A second catpath solve at reactant pressure ``p_bar`` — the
    apparent-order probe. ``record['solve_tof']`` (a callable a store
    read cannot carry) is the seam: a caller — or a test — injects the
    kinetics replay; without it ``None`` and α stays unmeasured."""
    solver = record.get("solve_tof")
    if callable(solver):
        out = _num(solver(p_bar))
        return out
    return None


def _kinetics_view(meta: dict[str, Any]) -> dict[str, Any]:
    """The D8 flip's read of a stored meta (gr514392). The same lift
    ``precis_pathway._dispatch_common._kinetics_scalars`` applies to the
    solve payload — the ``kinetics_v2`` record first, the v1 ``kinetics``
    block only where the record lacks the field — fed whatever records the
    meta carries (top-level keys, or a pathway ref's nested ``results``).
    ``log_tof`` and its band arrive only on a TRUSTED solve, exactly the
    harvest's gate; the record's own ``environment`` block supplies
    ``T_K`` and the ``{species: bar}`` pressure map a v1 ``kinetics``
    dict used to state. ``site_area_A2`` is a v1-only field (the record
    does not carry it): it is lifted off whichever block has it, and a
    post-retire meta without it is an honest refusal, not a guess.
    Reached lazily — ``precis_pathway`` is a sibling plugin; without it,
    or on a record the flip cannot read, the view is empty and the v1
    read below is unchanged."""
    try:
        from precis_pathway._dispatch_common import _kinetics_scalars
    except ImportError:
        return {}
    stored = meta.get("results")
    results = dict(stored) if isinstance(stored, dict) else {}
    for key in ("kinetics", "kinetics_v2", "kinetics_sweep", "kinetics_error"):
        if key in meta:
            results[key] = meta[key]
    if not any(results.get(k) for k in ("kinetics", "kinetics_v2", "kinetics_error")):
        return {}
    try:
        scalars = _kinetics_scalars(results)
    except Exception:
        return {}
    out: dict[str, Any] = {
        k: scalars[k]
        for k in ("tof", "log_tof", "log_tof_p5", "log_tof_p95")
        if k in scalars
    }
    env = None
    for rec in (results.get("kinetics_v2"), results.get("kinetics")):
        if not isinstance(rec, dict):
            continue
        if env is None and isinstance(rec.get("environment"), dict):
            env = rec["environment"]
        if out.get("site_area_A2") is None:
            area = _num(rec.get("site_area_A2"))
            if area is not None:
                out["site_area_A2"] = area
    if isinstance(env, dict):
        t = _num(env.get("T"))
        if t is not None:
            out["T_K"] = t
        gases = env.get("gases")
        inputs = gases.get("inputs") if isinstance(gases, dict) else None
        if isinstance(inputs, dict) and inputs:
            out["p_bar"] = dict(inputs)
    return out


def _record_from_meta(ref: Any) -> dict[str, Any]:
    """The solve record a serving candidate's meta carries: the quest
    scalars (``tof``, ``log_tof*``) plus the solve's stated conditions
    (``site_area_A2`` Å², ``T_K``, ``p_bar`` — a scalar or a
    ``{species: bar}`` map — and ``phase``). The D8 record is read first
    (:func:`_kinetics_view`), the v1 ``kinetics`` dict only fills the gaps
    it leaves — the merge order the D8 reader flip set."""
    meta = dict(ref.meta or {})
    kin = meta.get("kinetics")
    kin = dict(kin) if isinstance(kin, dict) else {}
    merged = {**kin, **_kinetics_view(meta), **meta}

    def pick(*keys: str) -> float | None:
        for k in keys:
            v = _num(merged.get(k))
            if v is not None:
                return v
        return None

    p = merged.get("p_bar")
    return {
        "ref": ref,
        "tof": pick("tof"),
        "log_tof": pick("log_tof"),
        "log_tof_p5": pick("log_tof_p5"),
        "log_tof_p95": pick("log_tof_p95"),
        "site_area_A2": pick("site_area_A2"),
        "T_K": pick("T_K", "temperature_K"),
        "p_bar": p,
        "phase": str(merged.get("phase") or "").strip().lower() or None,
        "solve_tof": merged.get("solve_tof"),
    }


def _trusted_solve(store: Any, source: str, species: str) -> dict[str, Any]:
    """The trusted solve record behind ``source`` — ``quest:<id>``
    scans the refs that ``serves`` it for the candidate carrying the
    best trusted ``log_tof``; ``structure:<id>`` reads that ref's own
    meta."""
    kind, _, sid = source.partition(":")
    sid = sid.strip()
    if not sid:
        raise BadInput(f"wall_rate: source must be '<kind>:<id>', got {source!r}")
    # Numeric kinds (quest) address by ref_id; slug kinds (structure) by
    # cite_key — get_ref picks the path on the argument's type.
    ref_id: int | str = int(sid) if sid.isdigit() else sid
    if kind == "structure":
        ref = store.get_ref(kind="structure", id=ref_id)
        if ref is None:
            raise BadInput(f"wall_rate: no structure {sid!r}")
        return _record_from_meta(ref)
    if kind != "quest":
        raise BadInput(
            f"wall_rate: source is 'quest:<id>' (or 'structure:<id>'), got {source!r}"
        )
    qref = store.get_ref(kind="quest", id=ref_id)
    if qref is None:
        raise BadInput(f"wall_rate: no quest {sid!r}")
    try:
        links = store.links_for(qref.id, direction="in", relation="serves")
    except TypeError:
        links = store.links_for(qref.id, direction="in")
    src_ids = {
        l.src_ref_id for l in links if getattr(l, "relation", "serves") == "serves"
    }
    cands = list(store.fetch_refs_by_ids(src_ids).values())
    records = [
        (r["log_tof"], r)
        for s in cands
        for r in [_record_from_meta(s)]
        if r["tof"] is not None and r["log_tof"] is not None
    ]
    if not records:
        raise BadInput(
            f"wall_rate: no trusted catpath solve serves quest {sid!r} — "
            "trusted is the presence of log_tof; run the kinetics first",
            next="dispatch the microkinetics solve, then re-run wall_rate",
        )
    records.sort(key=lambda t: t[0])
    rec = records[-1][1]
    rec["quest_id"] = qref.id
    return rec


def _pressure(record: dict[str, Any], species: str) -> float | None:
    """The solve's stated reactant partial pressure in bar — a scalar
    ``p_bar`` or the species' entry of a ``{species: bar}`` map."""
    p = record.get("p_bar")
    if isinstance(p, dict):
        return _num(p.get(species))
    return _num(p)


def op_wall_rate(store: Any, tree: Any, op: dict[str, Any]) -> str:
    """Apply ``{"op":"wall_rate",…}``: bind a trusted catpath TOF to a
    ``<species>/wall-rate-constant`` patch measure (origin ``proposed``)."""
    block = str(op.get("block") or "").strip()
    datum = str(op.get("datum") or "").strip()
    source = str(op.get("source") or "").strip()
    species = str(op.get("species") or "").strip()
    for key, val in (
        ("block", block),
        ("datum", datum),
        ("source", source),
        ("species", species),
    ):
        if not val:
            raise BadInput(
                f"wall_rate needs {key}= — "
                '{"op":"wall_rate","block":…,"datum":"patch:…",'
                '"source":"quest:<id>","species":…}'
            )
    node = tree.blocks.get(block)
    if node is None:
        raise BadInput(f"wall_rate: no block {block!r}")
    try:
        roughness = float(op.get("roughness") or 1.0)
    except (TypeError, ValueError):
        raise BadInput(
            f"wall_rate: roughness must be a number, got {op.get('roughness')!r}"
        ) from None
    if roughness <= 0:
        raise BadInput("wall_rate: roughness must be > 0")

    rec = _trusted_solve(store, source, species)
    tof = rec["tof"]
    if rec["log_tof"] is None:
        raise BadInput(
            f"wall_rate: the solve behind {source!r} is untrusted (no "
            "log_tof — a failed or unverified catpath run); a raw tof "
            "never binds a rate",
        )
    if tof is None or tof <= 0:
        raise BadInput(
            f"wall_rate: tof {tof!r} ≤ 0 — a net-reverse or failed solve "
            "carries no forward rate"
        )
    if rec["phase"] not in (None, "gas", "vapour", "vapor"):
        raise BadInput(
            f"wall_rate: phase {rec['phase']!r} — liquid-phase rates are "
            "refused (no solvation/Henry model stated, spec Q3)"
        )
    if rec["site_area_A2"] is None or rec["site_area_A2"] <= 0:
        raise BadInput(
            f"wall_rate: the solve behind {source!r} carries no "
            "site_area_A2 — the slab site area is required"
        )
    if rec["T_K"] is None or rec["T_K"] <= 0:
        raise BadInput(
            f"wall_rate: the solve behind {source!r} carries no T_K — "
            "the solve's stated temperature is required"
        )
    p_bar = _pressure(rec, species)
    if p_bar is None or p_bar <= 0:
        raise BadInput(
            f"wall_rate: no stated reactant partial pressure for "
            f"{species!r} behind {source!r} (expected p_bar — bar, gas "
            "phase)"
        )

    a_site = rec["site_area_A2"] * 1e-20  # Å² → m²
    c_wall = p_bar * 1e5 / (R_GAS * rec["T_K"])  # bar → Pa, ideal gas
    r = tof / (a_site * N_A)  # mol m⁻² s⁻¹
    k_s = roughness * r / c_wall  # m/s

    alpha: float | None = None
    tof_plus = _solve_tof(rec, p_bar * (1.0 + DELTA))
    tof_minus = _solve_tof(rec, p_bar * (1.0 - DELTA))
    if (
        tof_plus is not None
        and tof_minus is not None
        and tof_plus > 0
        and tof_minus > 0
    ):
        alpha = math.log(tof_plus / tof_minus) / math.log((1.0 + DELTA) / (1.0 - DELTA))

    def _k_of_tof(t: float) -> float:
        return roughness * (t / (a_site * N_A)) / c_wall

    # The stored name is '<species>/wall-rate-constant' (Q2 scope via the
    # ops' species= key). A second reactive patch for the same species
    # takes a -2/-3 suffix so an earlier patch's measure is not replaced;
    # it still binds through the shared 'wall-rate-constant' measurand.
    taken = {(m.name, m.datum) for m in tree.measures if m.block == block}
    mname = "wall-rate-constant"
    n = 2
    while (f"{species}{SCOPE_SEP}{mname}", datum) not in taken and any(
        name == f"{species}{SCOPE_SEP}{mname}" for name, _d in taken
    ):
        mname = f"wall-rate-constant-{n}"
        n += 1
    fields: dict[str, Any] = {
        "block": block,
        "name": mname,
        "species": species,
        "datum": datum,
        "measurand": _MEASURAND,
        "value": k_s,
        "origin": "proposed",
        "reason": (
            f"catpath TOF → k_s: {source}, T {rec['T_K']:g} K, "
            f"p {p_bar * 1e5:g} Pa, roughness {roughness:g}"
        ),
        "provenance": {
            "source": "solver",
            "fidelity": "full_solve",
            "solver_id": f"catpath:{source}",
            "assumptions": [
                f"quest={source}",
                f"T_K={rec['T_K']:g}",
                f"p_Pa={p_bar * 1e5:g}",
                f"roughness={roughness:g}",
                "trusted=log_tof present",
                f"alpha={alpha if alpha is not None else 'unmeasured'}",
                f"delta={DELTA}",
                f"site_area_A2={rec['site_area_A2']:g} (A2 -> m2 x1e-20)",
                "phase=gas (c_wall = p/(R T), bar -> Pa x1e5)",
            ],
        },
    }
    lo = rec["log_tof_p5"]
    hi = rec["log_tof_p95"]
    if lo is not None and hi is not None and lo <= hi:
        fields["min"] = _k_of_tof(10.0**lo)
        fields["max"] = _k_of_tof(10.0**hi)

    stored = f"{species}{SCOPE_SEP}{mname}"
    exists = (stored, datum) in taken
    opname = "set_measure" if exists else "add_measure"
    try:
        apply_ops(tree, [{"op": opname, **fields}])
    except OpError as exc:
        raise BadInput(str(exc)) from exc
    alpha_txt = f"{alpha:.4g}" if alpha is not None else "unmeasured"
    return (
        f"wall_rate: {block}.{species}{SCOPE_SEP}wall-rate-constant = {k_s:.6g} m/s "
        f"({source}, T {rec['T_K']:g} K, p {p_bar * 1e5:g} Pa, "
        f"α {alpha_txt} at δ {DELTA}, roughness {roughness:g})"
    )
