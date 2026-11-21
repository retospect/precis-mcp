"""``quest.environment_stamps.stamp_environment_results`` — the write
side of the slab-environment optimizer (td568641).

env_best/env_pareto/env_next are non-measure stamps; env_best_J and
env_pareto_fraction are the declared measures. The writer refuses
non-finite values, caps the Pareto set at 24, merges the pathway's
network basis under both measures, and accepts only the two footing
values.
"""

from __future__ import annotations

from typing import Any
from uuid import uuid4

import pytest

from precis.quest import compute as compute_mod
from precis.quest.environment_optimizer import (
    EnvironmentPoint,
    PointEvaluation,
    Term,
)
from precis.quest.environment_stamps import (
    DEFAULT_FOOTING_NOTE,
    ENV_PARETO_CAP,
    stamp_environment_results,
)
from precis.store import Store

_POINT = EnvironmentPoint(
    U=-0.1, pH=7.0, T=600.0, pressures={"NO": 0.5}, ion_conc_M=1e-3
)


def _evaluation(
    j: float | None = 1.5,
    *,
    u: float | None = -0.1,
    limiting: str | None = "m_kin",
    terms: dict[str, Term] | None = None,
) -> PointEvaluation:
    return PointEvaluation(
        point=EnvironmentPoint(
            U=u,
            pH=_POINT.pH,
            T=_POINT.T,
            pressures=_POINT.pressures,
            ion_conc_M=_POINT.ion_conc_M,
        ),
        terms=terms
        if terms is not None
        else {
            "m_th": Term(0.4, "staircase"),
            "m_kin": Term(j if j is not None else 0.0, "kinetics at the run U"),
        },
        mask=Term(1.0, "passes"),
        objective=Term(j, "bottleneck"),
        limiting=limiting,
        checks={"physical": Term(1.0, "ok")},
    )


def _structure(store: Store) -> int:
    return store.insert_ref(
        kind="structure", slug=f"slab-{uuid4().hex[:8]}", title="slab"
    ).id


def _meta(store: Store, sid: int) -> dict[str, Any]:
    return dict(store.fetch_refs_by_ids({sid})[sid].meta or {})


def test_stamps_all_five_keys(store: Store) -> None:
    sid = _structure(store)
    note = stamp_environment_results(
        store,
        sid,
        footing="computed",
        env_best=_evaluation(1.5),
        env_pareto=[_evaluation(1.5), _evaluation(1.2, u=-0.2)],
        env_next={
            "point": EnvironmentPoint(
                U=0.3, pH=7.0, T=None, pressures=None, ion_conc_M=None
            ),
            "reason": "outside the sweep box",
            "cart_items": ["coverage_scan"],
        },
        env_pareto_fraction=0.6,
    )
    assert note is not None and "environment stamps" in note
    meta = _meta(store, sid)
    best = meta["env_best"]
    assert best["J"] == 1.5
    assert best["point"]["U"] == -0.1
    assert best["point"]["pressures"] == {"NO": 0.5}
    assert best["limiting"] == "m_kin"
    assert best["footing"] == "computed"
    assert best["footing_note"] == DEFAULT_FOOTING_NOTE
    assert best["terms"]["m_th"] == {"value": 0.4, "reason": "staircase"}
    assert best["trust"]["mask"] == {"value": 1.0, "reason": "passes"}
    assert "physical" in best["trust"]["checks"]
    assert meta["env_best_J"] == 1.5
    assert meta["env_pareto_fraction"] == 0.6
    assert len(meta["env_pareto"]) == 2
    assert meta["env_pareto"][0]["J"] == 1.5
    # Pareto rows carry no trust block — only env_best does.
    assert "trust" not in meta["env_pareto"][0]
    assert meta["env_next"] == {
        "point": {
            "U": 0.3,
            "pH": 7.0,
            "T": None,
            "pressures": None,
            "ion_conc_M": None,
        },
        "reason": "outside the sweep box",
        "cart_items": ["coverage_scan"],
    }


def test_idempotent_restamp(store: Store) -> None:
    sid = _structure(store)
    kwargs: dict[str, Any] = {
        "footing": "computed",
        "env_best": _evaluation(1.5),
        "env_pareto_fraction": 0.6,
    }
    assert stamp_environment_results(store, sid, **kwargs) is not None
    first = _meta(store, sid)
    assert stamp_environment_results(store, sid, **kwargs) is None
    assert _meta(store, sid) == first


@pytest.mark.parametrize("bad_j", [float("-inf"), float("nan"), None])
def test_non_finite_j_writes_no_env_best(store: Store, bad_j: Any) -> None:
    """A failed mask (-inf), NaN or an unknown objective is never a
    value: env_best and env_best_J are cleared, not stamped."""
    sid = _structure(store)
    stamp_environment_results(
        store,
        sid,
        footing="computed",
        env_best=_evaluation(1.5),
        env_pareto_fraction=0.4,
    )
    assert _meta(store, sid)["env_best_J"] == 1.5
    stamp_environment_results(
        store, sid, footing="computed", env_best=_evaluation(bad_j)
    )
    meta = _meta(store, sid)
    assert meta.get("env_best") is None
    assert meta.get("env_best_J") is None
    assert meta["env_pareto_fraction"] is None  # not re-stamped → cleared


def test_non_finite_fraction_never_written(store: Store) -> None:
    sid = _structure(store)
    stamp_environment_results(
        store,
        sid,
        footing="computed",
        env_best=_evaluation(1.0),
        env_pareto_fraction=float("nan"),
    )
    meta = _meta(store, sid)
    assert meta["env_best_J"] == 1.0
    assert meta.get("env_pareto_fraction") is None


def test_pareto_capped_at_24(store: Store) -> None:
    sid = _structure(store)
    evals = [_evaluation(1.0 + i * 0.01, u=-0.1 - i * 0.01) for i in range(30)]
    stamp_environment_results(store, sid, footing="computed", env_pareto=evals)
    meta = _meta(store, sid)
    assert len(meta["env_pareto"]) == ENV_PARETO_CAP == 24


def test_env_next_cart_item_names(store: Store) -> None:
    sid = _structure(store)
    stamp_environment_results(
        store,
        sid,
        footing="computed",
        env_next={
            "point": {"U": 0.4, "pH": 7.0},
            "reason": "a decisive edge without a barrier",
            "cart_items": ["modes", "recompute_kinetics", "neb"],
        },
    )
    assert _meta(store, sid)["env_next"]["cart_items"] == [
        "modes",
        "recompute_kinetics",
        "neb",
    ]


def test_basis_merge_stamps_both_measures(store: Store) -> None:
    """The pathway's network basis merges under env_best_J and
    env_pareto_fraction so a basis move reprices them."""
    sid = _structure(store)
    pw_meta = {
        "config": {"network": "ammonia", "template": "parked"},
        "autocatpath_version": "0.24.0",
        "results": {"network_digest": "abc123"},
    }
    stamp_environment_results(
        store,
        sid,
        footing="computed",
        env_best=_evaluation(1.5),
        env_pareto_fraction=0.5,
        pathway_meta=pw_meta,
    )
    by_key = _meta(store, sid)["network_basis_by_key"]
    basis = compute_mod._network_basis(pw_meta)
    assert by_key["env_best_J"] == basis
    assert by_key["env_pareto_fraction"] == basis


def test_basis_entry_dropped_when_measure_cleared(store: Store) -> None:
    sid = _structure(store)
    pw_meta = {"config": {"network": "ammonia"}}
    stamp_environment_results(
        store,
        sid,
        footing="computed",
        env_best=_evaluation(1.5),
        pathway_meta=pw_meta,
    )
    assert "env_best_J" in _meta(store, sid)["network_basis_by_key"]
    stamp_environment_results(
        store,
        sid,
        footing="computed",
        env_best=_evaluation(float("-inf")),
        pathway_meta=pw_meta,
    )
    meta = _meta(store, sid)
    assert meta.get("env_best_J") is None
    # The last entry's removal leaves the map empty — stored as JSON null
    # (never an empty map), so the row reads absence, not a vacant dict.
    assert meta.get("network_basis_by_key") in (None, {})


def test_sixth_term_m_ea_is_stored(store: Store) -> None:
    """Terms serialise verbatim — never a fixed five."""
    sid = _structure(store)
    terms = {
        "m_th": Term(0.4, ""),
        "m_kin": Term(1.5, ""),
        "m_Ea": Term(0.7, "Ea_app resolved"),
    }
    stamp_environment_results(
        store, sid, footing="computed", env_best=_evaluation(1.5, terms=terms)
    )
    stored = _meta(store, sid)["env_best"]["terms"]
    assert set(stored) == {"m_th", "m_kin", "m_Ea"}
    assert stored["m_Ea"] == {"value": 0.7, "reason": "Ea_app resolved"}


@pytest.mark.parametrize("bad", ["run-point", "guessed", "", None, 3])
def test_footing_refused(store: Store, bad: Any) -> None:
    sid = _structure(store)
    with pytest.raises(ValueError, match="footing"):
        stamp_environment_results(store, sid, footing=bad)


def test_basis_merge_survives_a_concurrent_harvest(
    store: Store, monkeypatch: Any
) -> None:
    """The basis map is merged entry-by-entry under a row lock, never
    replaced from the writer's pre-read snapshot: a harvest that
    re-stamped another measure's basis between the snapshot and the
    write keeps its entry — previously the shallow merge reverted the
    harvest's new basis while keeping its new value (review td659951)."""
    from dataclasses import replace as _dc_replace

    sid = _structure(store)
    harvest_basis = {"digest": "d9", "template": "parked", "version": "0.25.0"}
    store.stamp_ref_meta(
        sid, {"network_basis_by_key": {"barrier": dict(harvest_basis)}}
    )
    real_fetch = store.fetch_refs_by_ids

    def _stale_view(ref_ids: Any, **kwargs: Any) -> Any:
        # The snapshot the writer would have read before the harvest
        # committed: the basis map exists but holds no entries yet.
        refs = real_fetch(ref_ids, **kwargs)
        return {
            rid: _dc_replace(
                ref, meta={**dict(ref.meta or {}), "network_basis_by_key": {}}
            )
            for rid, ref in refs.items()
        }

    monkeypatch.setattr(store, "fetch_refs_by_ids", _stale_view)
    pw_meta = {"config": {"network": "ammonia"}, "autocatpath_version": "0.24.0"}
    note = stamp_environment_results(
        store,
        sid,
        footing="computed",
        env_best=_evaluation(1.5),
        pathway_meta=pw_meta,
    )
    assert note is not None
    by_key = dict(real_fetch({sid})[sid].meta or {})["network_basis_by_key"]
    assert by_key["env_best_J"] == compute_mod._network_basis(pw_meta)
    assert by_key["barrier"] == harvest_basis
