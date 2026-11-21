"""Environment-optimizer stamps on a candidate — the write side of the
slab-environment-optimizer spec (sections me568509/me568512/me568514).

The producers (layers 1–3, td568639/td568640) call
:func:`stamp_environment_results` once per evaluated candidate; the
frontier then ranks an environment-space quest's slabs at their own
``env_best`` instead of at the run point
(:func:`precis.quest.frontier.quest_frontier`), with the run-point
fallback for a slab the optimizer has not produced ``env_best`` for.

Stored shapes (candidate ``meta`` — the contract td568640 and td568642
build to):

- ``env_best`` — the rubric's pick, a dict, never a measure::

    {"point": {"U": …, "pH": …, "T": …, "pressures": {…}, "ion_conc_M": …},
     "J": <float>, "terms": {<name>: {"value": …, "reason": …}, …},
     "limiting": "<term name>",
     "trust": {"mask": {"value": …, "reason": …},
               "checks": {<name>: {"value": …, "reason": …}, …}},
     "footing": "computed" | "interpolated",
     "footing_note": "kinetics at the run U"}

  Every term the :class:`~precis.quest.environment_optimizer.PointEvaluation`
  carries is stored — a sixth term ``m_Ea``
  (:data:`precis.quest.environment_space.ENVIRONMENT_TERMS`) rides along;
  never a fixed five. A failed mask gives J of ``-inf`` and an unknown
  one ``None``: there is then no ``env_best`` — both ``env_best`` and
  ``env_best_J`` are cleared, never a ``-inf``/NaN/fake-zero write.
- ``env_pareto`` — at most :data:`ENV_PARETO_CAP` entries of
  ``{"point", "J", "terms", "limiting", "footing"}``: the Pareto set
  over the terms, harvested from every evaluated point and capped at the
  ruled size (ruling me568266).
- ``env_next`` — ``{"point", "reason", "cart_items"}``: the point to
  compute next and why, with the matching cart item names
  (``coverage_scan``, ``modes``, ``recompute_kinetics``, ``neb``). The
  request door is not built — item names only, never dispatches.
- ``env_best_J`` / ``env_pareto_fraction`` — the two declared measures
  (the frontier's env-cohort objectives, sense ``max`` both). The
  fraction comes from the producer — the share of the box that is viable
  and selective; this function never derives one.

``footing`` is a required keyword and is refused unless ``"computed"``
(the kinetic terms came from a stored solve or sweep row at exactly that
point — all the evaluator yields today:
:func:`precis.quest.environment_optimizer.kinetic_record` matches exactly
and never interpolates) or ``"interpolated"`` (a producer evaluated
between adjacent stored grid points — the only interpolation allowed,
``precis_pathway.environment._lookup_sweep``). ``footing_note`` is the
standing "kinetics at the run U": while kinetics at a given U is not a
sweep axis, ``env_best`` says so and never reports a joint optimum
(section me568514).

Both measures ride the network basis: the writer merges
``meta.network_basis_by_key`` for them
(:func:`precis.quest.compute._network_basis_by_key`) with
:func:`precis.quest.compute._network_basis` of the pathway the evaluator
read, so :func:`precis.quest.frontier.apply_network_demotion` drops a
mismatch and the slab falls back to the run-point cohort. The merge is
entry-by-entry under a row lock (``Store.stamp_ref_meta_entries``),
never a replace of the whole map from a pre-read snapshot — a concurrent
harvest re-stamping another measure's basis between the read and the
write would otherwise keep its new value while losing its new basis
(review td659951). A call without ``pathway_meta`` (or one whose meta
projects no basis) leaves the measures unstamped — the frontier demotes
them as unprovenanced, so such a value never ranks.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from typing import TYPE_CHECKING, Any

from precis.quest.environment_optimizer import EnvironmentPoint, PointEvaluation

if TYPE_CHECKING:
    from precis.store import Store

#: The env_pareto cap — ruling me568266 (Reto 2026-11-05).
ENV_PARETO_CAP = 24

#: The only footing values the writer accepts: ``"computed"`` (a stored
#: solve or sweep row at exactly that point) and ``"interpolated"``
#: (between adjacent stored grid points — the only interpolation
#: allowed). Anything else is refused.
FOOTINGS = frozenset({"computed", "interpolated"})

#: Kinetics at a given U is not a sweep axis — the standing note every
#: env_best carries so it never reads as a joint optimum.
DEFAULT_FOOTING_NOTE = "kinetics at the run U"


def _finite(value: Any) -> float | None:
    """A stored finite number; booleans, null and non-finite are not
    measurements (never write -inf, NaN or a fake zero)."""
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    return float(value) if math.isfinite(value) else None


def _point_dict(point: Any) -> dict[str, Any] | None:
    """``{U, pH, T, pressures, ion_conc_M}`` from an
    :class:`~precis.quest.environment_optimizer.EnvironmentPoint` (or a
    dict in that shape, passed through sanitised); ``None`` when there is
    no point at all. Non-finite coordinates serialise as ``None`` —
    absent, never a bogus number."""
    if point is None:
        return None
    if isinstance(point, EnvironmentPoint):
        raw = {
            "U": point.U,
            "pH": point.pH,
            "T": point.T,
            "pressures": point.pressures,
            "ion_conc_M": point.ion_conc_M,
        }
    elif isinstance(point, Mapping):
        raw = {k: point.get(k) for k in ("U", "pH", "T", "pressures", "ion_conc_M")}
    else:
        return None
    pressures = raw.get("pressures")
    return {
        "U": _finite(raw.get("U")),
        "pH": _finite(raw.get("pH")),
        "T": _finite(raw.get("T")),
        "pressures": (
            {str(g): v for g, p in pressures.items() if (v := _finite(p)) is not None}
            if isinstance(pressures, Mapping)
            else None
        ),
        "ion_conc_M": _finite(raw.get("ion_conc_M")),
    }


def _term_dict(term: Any) -> dict[str, Any]:
    """``{"value", "reason"}`` for one evaluator Term — the value is
    ``None`` when non-finite (the reason then carries what happened)."""
    value = getattr(term, "value", None)
    reason = getattr(term, "reason", None)
    return {
        "value": _finite(value),
        "reason": str(reason) if reason is not None else "",
    }


def _terms_dict(terms: Any) -> dict[str, Any]:
    """Every term the evaluation carries — never a fixed five."""
    if not isinstance(terms, Mapping):
        return {}
    return {str(k): _term_dict(t) for k, t in terms.items()}


def _evaluation_dict(
    evaluation: PointEvaluation,
    footing: str,
    footing_note: str,
    *,
    trust: bool,
) -> dict[str, Any] | None:
    """Serialise one :class:`PointEvaluation` — or ``None`` when its J is
    not a finite number: a failed mask (``-inf``) or an unknown one means
    there is no optimum to report, so nothing is written."""
    j = _finite(getattr(evaluation.objective, "value", None))
    if j is None:
        return None
    out: dict[str, Any] = {
        "point": _point_dict(evaluation.point),
        "J": j,
        "terms": _terms_dict(evaluation.terms),
        "limiting": evaluation.limiting
        if isinstance(evaluation.limiting, str)
        else None,
        "footing": footing,
    }
    if trust:
        out["trust"] = {
            "mask": _term_dict(evaluation.mask),
            "checks": _terms_dict(evaluation.checks),
        }
        out["footing_note"] = footing_note
    return out


def _next_dict(env_next: Any) -> dict[str, Any] | None:
    """``{"point", "reason", "cart_items"}`` — the point to compute next
    and why; cart item names only (the request door is not built)."""
    if not isinstance(env_next, Mapping):
        return None
    items = env_next.get("cart_items")
    if isinstance(items, str):
        cart_items = [items]
    elif isinstance(items, Sequence):
        cart_items = [str(i) for i in items]
    else:
        cart_items = []
    reason = env_next.get("reason")
    return {
        "point": _point_dict(env_next.get("point")),
        "reason": str(reason) if reason is not None else "",
        "cart_items": cart_items,
    }


def stamp_environment_results(
    store: Store,
    structure_ref_id: int,
    *,
    footing: str,
    env_best: PointEvaluation | None = None,
    env_pareto: Sequence[PointEvaluation] = (),
    env_next: Mapping[str, Any] | None = None,
    env_pareto_fraction: float | None = None,
    pathway_meta: Mapping[str, Any] | None = None,
    footing_note: str = DEFAULT_FOOTING_NOTE,
) -> str | None:
    """Stamp the five environment-optimizer keys on one candidate —
    :func:`stamp_window_measures`-style: ``store.stamp_ref_meta`` of the
    changed keys only, a one-line note on change, ``None`` on a no-op.

    ``footing`` is required and must be one of :data:`FOOTINGS` — a
    ``ValueError`` otherwise. ``env_best`` is the evaluation the producer
    picked; a non-finite J (failed or unknown mask) means no env_best:
    ``env_best`` and ``env_best_J`` are cleared instead of writing a
    bogus value. ``env_pareto`` is the Pareto set of evaluations, capped
    at :data:`ENV_PARETO_CAP`; ``env_pareto_fraction`` is the producer's
    own share-of-the-box figure (never derived here). ``pathway_meta`` is
    the pathway ref's meta the evaluator read — its network basis merges
    into ``network_basis_by_key`` for both measures.
    """
    if footing not in FOOTINGS:
        raise ValueError(
            f"footing must be one of {sorted(FOOTINGS)}, got {footing!r} — "
            "'computed' for a stored solve/sweep row at exactly that point, "
            "'interpolated' between adjacent stored grid points"
        )
    from precis.quest import compute as compute_mod

    ref = store.fetch_refs_by_ids({int(structure_ref_id)}).get(int(structure_ref_id))
    existing = dict((ref.meta if ref is not None else None) or {})

    best = (
        _evaluation_dict(env_best, footing, footing_note, trust=True)
        if env_best is not None
        else None
    )
    pareto = [
        d
        for d in (
            _evaluation_dict(ev, footing, footing_note, trust=False)
            for ev in env_pareto
        )
        if d is not None
    ][:ENV_PARETO_CAP]
    fraction = _finite(env_pareto_fraction)
    desired: dict[str, Any] = {
        "env_best": best,
        "env_best_J": best["J"] if best is not None else None,
        "env_pareto": pareto or None,
        "env_next": _next_dict(env_next),
        "env_pareto_fraction": fraction,
    }

    basis = compute_mod._network_basis(dict(pathway_meta) if pathway_meta else None)
    env_keys = ("env_best_J", "env_pareto_fraction")
    # The basis map is merged entry-by-entry under a row lock at write
    # time — never replaced from this pre-read snapshot, which a
    # concurrent harvest may already have made stale for the OTHER
    # measures' entries (review td659951).
    set_entries = {
        k: basis for k in env_keys if basis is not None and desired[k] is not None
    }
    # A cleared measure keeps no basis entry — its stale stamp must
    # not mark the candidate network_stale on a value it no longer
    # carries; the same when this write carries no basis at all (the
    # previous evaluation's provenance must not carry over to the new
    # value).
    drop_entries = tuple(k for k in env_keys if desired[k] is None or basis is None)
    snapshot_map = existing.get("network_basis_by_key")
    snapshot_dict = dict(snapshot_map) if isinstance(snapshot_map, dict) else {}
    projected = dict(snapshot_dict)
    for k in drop_entries:
        projected.pop(k, None)
    projected.update(set_entries)
    basis_changed = projected != snapshot_dict

    changed = {
        k: v
        for k, v in desired.items()
        if existing.get(k) != v and (v is not None or existing.get(k) is not None)
    }
    if not changed and not basis_changed:
        return None
    store.stamp_ref_meta_entries(
        int(structure_ref_id),
        changed,
        map_key="network_basis_by_key",
        set_entries=set_entries,
        drop_entries=drop_entries,
    )
    bits = []
    if best is not None:
        bits.append(f"env_best J={best['J']:g} ({footing})")
    else:
        bits.append("no env_best")
    if pareto:
        bits.append(f"{len(pareto)} pareto")
    if fraction is not None:
        bits.append(f"fraction {fraction:g}")
    if desired["env_next"] is not None:
        bits.append("env_next")
    return f"environment stamps for structure {int(structure_ref_id)}: " + ", ".join(
        bits
    )


__all__ = [
    "DEFAULT_FOOTING_NOTE",
    "ENV_PARETO_CAP",
    "FOOTINGS",
    "stamp_environment_results",
]
