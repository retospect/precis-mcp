"""``precis_pathway.tof_sign`` + the surfaces that read it (gr559294).

A negative ``tof_mf`` no longer fails the kinetics job — the engine
classifies it (``kinetics_v2.mf_status.tof_sign``) and precis shows the
label wherever the TOF appears: the harvest keeps ``noise`` on the
Pareto field as ~0, drops every non-noise class to the provisional band
with the label as its reason, and the MCP ``view='kinetics'`` text and
the web panel's ``v2_solve`` box both print the class in plain words.
A record with no classification (``tof_mf >= 0``, or any pre-cut engine)
renders exactly as before.
"""

from __future__ import annotations

import re
from typing import Any

import pytest

from precis.dispatch import Hub
from precis.handlers.quest import QuestHandler
from precis.quest import compute as compute_mod
from precis.quest.frontier import quest_frontier
from precis_pathway._dispatch_common import _kinetics_scalars
from precis_pathway.tof_sign import (
    pareto_keeps,
    tof_sign,
    tof_sign_detail,
    tof_sign_label,
)
from precis_pathway.toon_views import _v2_status_lines


def _sign(cls: str, **over: Any) -> dict[str, Any]:
    return {"class": cls, **over}


def _v2(tof_mf: float, sign: dict[str, Any] | None, **status: Any) -> dict[str, Any]:
    """A kinetics_v2 record whose MF rung carries the ranked TOF."""
    mf_status: dict[str, Any] = {"ok": True, **status}
    if sign is not None:
        mf_status["tof_sign"] = sign
    return {"tof_mf": tof_mf, "mf_status": mf_status}


# ── the pure label mapping ────────────────────────────────────────────


def test_tof_sign_absent_and_drifted_shapes() -> None:
    assert tof_sign({}) is None
    assert tof_sign({"mf_status": {"ok": True}}) is None
    assert tof_sign({"mf_status": {"tof_sign": {}}}) is None  # no class
    assert tof_sign({"mf_status": {"tof_sign": "noise"}}) is None
    assert tof_sign(None) is None
    assert pareto_keeps(None) is True


def test_label_mapping_each_class() -> None:
    assert tof_sign_label(_sign("noise")) == "about zero (below noise floor)"
    assert tof_sign_label(_sign("reverse")) == "runs in reverse (Q/K > 1)"
    got = tof_sign_label(
        _sign("consumed_by_other", sinks={"NH3": -2.4e-7, "NO": -1e-9})
    )
    assert got == "consumed by another reaction: NH3, NO"
    got = tof_sign_label(_sign("unresolved", sinks={"NH3": -2.4e-7}))
    assert got == "consumed by another reaction (cause unknown): NH3"
    assert tof_sign_label(_sign("broken", reason="N imbalance 0.31")) == (
        "broken: N imbalance 0.31"
    )


def test_label_gas_cap_and_detail() -> None:
    sign = _sign(
        "consumed_by_other",
        sinks={"NH3": -1.0, "NO": -0.5, "H2": -0.3, "N2": -0.1},
        sources={"N2": 0.6, "H2O": 0.2},
    )
    # engine order is already most-consumed first — the cap is a slice
    assert tof_sign_label(sign) == "consumed by another reaction: NH3, NO, H2"
    assert tof_sign_detail(sign) == "sinks: NH3, NO, H2 · sources: N2, H2O"
    assert tof_sign_detail(_sign("noise")) == ""
    # a null rate is an absent measurement, never a named gas
    assert tof_sign_detail(_sign("reverse", sinks={"NH3": None})) == ""


def test_pareto_rule() -> None:
    assert pareto_keeps(_sign("noise")) is True
    for cls in ("reverse", "consumed_by_other", "unresolved", "broken"):
        assert pareto_keeps(_sign(cls)) is False


# ── the harvest: noise stays, the rest leave the front ────────────────


def test_scalars_noise_counts_as_about_zero_and_stays_trusted() -> None:
    out = _kinetics_scalars(
        {"kinetics_v2": _v2(-2.13e-31, _sign("noise"), tof_near_noise=True)}
    )
    assert out["kinetics_trusted"] is True
    assert out["tof"] == -2.13e-31  # the raw value survives for display
    assert out["log_tof"] == pytest.approx(-30.0)  # ~0 on the log axis
    assert "kinetics_note" not in out


@pytest.mark.parametrize(
    ("sign", "label"),
    [
        (_sign("reverse"), "runs in reverse (Q/K > 1)"),
        (
            _sign("consumed_by_other", sinks={"NH3": -2.45e-7}),
            "consumed by another reaction: NH3",
        ),
        (
            _sign("unresolved", sinks={"NH3": -1e-7}),
            "consumed by another reaction (cause unknown): NH3",
        ),
        (_sign("broken", reason="N imbalance 0.31"), "broken: N imbalance 0.31"),
    ],
)
def test_scalars_non_noise_negative_is_excluded_with_label(
    sign: dict[str, Any], label: str
) -> None:
    out = _kinetics_scalars({"kinetics_v2": _v2(-2.45e-7, sign)})
    assert out["kinetics_trusted"] is False
    assert out["kinetics_note"] == label
    assert out["tof"] == -2.45e-7  # surfaced raw, never under a ranked key
    assert "log_tof" not in out


def test_scalars_broken_via_ok_false_still_labels() -> None:
    """``broken`` also sets ``mf_status.ok`` false — the label, not the
    generic failure note, is still the exclusion reason."""
    out = _kinetics_scalars(
        {
            "kinetics_v2": _v2(
                -1e-3,
                _sign("broken", reason="element imbalance N: +0.4"),
                ok=False,
                stage="tof_sign",
            )
        }
    )
    assert out["kinetics_trusted"] is False
    assert out["kinetics_note"] == "broken: element imbalance N: +0.4"


def test_scalars_absent_key_keeps_todays_anomaly_note() -> None:
    for results in (
        {"kinetics_v2": _v2(-1e-3, None)},
        {"kinetics_v2": {"tof_mf": -1e-3}},  # no mf_status at all
        {"kinetics": {"tof": -1e-3}},  # v1-only record
    ):
        out = _kinetics_scalars(results)
        assert out["kinetics_trusted"] is False
        assert out["kinetics_note"] == "tof non-positive (solver anomaly)"


def test_scalars_kmc_rung_ignores_the_mf_classification() -> None:
    """The sign speaks only for the MF rung: a KMC median outranking a
    negative tof_mf keeps the solve trusted on the KMC number."""
    rec = _v2(-1e-3, _sign("consumed_by_other", sinks={"NH3": -1e-3}))
    rec["tof_kmc"] = {"median": 2.0, "p5": 1.5, "p95": 2.5}
    out = _kinetics_scalars({"kinetics_v2": rec})
    assert out["kinetics_method"] == "kmc"
    assert out["kinetics_trusted"] is True


# ── the MCP ``view='kinetics'`` text ──────────────────────────────────


def test_status_lines_label_each_class() -> None:
    lines = _v2_status_lines(_v2(-2.45e-7, _sign("reverse")), product="NH3")
    assert any("runs in reverse (Q/K > 1)" in ln for ln in lines)
    lines = _v2_status_lines(
        _v2(
            -2.45e-7,
            _sign("consumed_by_other", sinks={"NH3": -1e-7}, sources={"N2": 5e-8}),
        ),
        product="NH3",
    )
    assert any("consumed by another reaction: NH3" in ln for ln in lines)
    assert any("sinks: NH3" in ln and "sources: N2" in ln for ln in lines)
    lines = _v2_status_lines(_v2(-2.1e-31, _sign("noise")), product="NH3")
    assert any("about zero (below noise floor)" in ln for ln in lines)


def test_status_lines_absent_key_renders_as_today() -> None:
    lines = _v2_status_lines({"tof_mf": -1e-3}, product="NH3")
    assert any(
        "TOF (mf) = -1.00e-03 — no positive steady state, not a rate" in ln
        for ln in lines
    )


# ── the Pareto view ───────────────────────────────────────────────────

_SPEC = {
    "cell": {"a": 8.4, "b": 8.4, "c": 24.0, "pbc": [True, True, False]},
    "ops": [{"op": "add_atom", "element": "Fe", "frac": [0.0, 0.0, 0.5]}],
}


def _mk_quest(store: Any, text: str) -> int:
    resp = QuestHandler(hub=Hub(store=store)).put(text=text)
    m = re.search(r"\bqu(\d+)\b", resp.body)
    assert m is not None, resp.body
    return int(m.group(1))


def _measured_candidate(store: Any, qid: int, meta: dict[str, Any]) -> int:
    sid = compute_mod.ensure_candidate(store, qid, {"name": "Pd", "structure": _SPEC})
    assert sid is not None
    store.stamp_ref_meta(
        qid, {"rubric_objectives": [{"key": "log_tof", "sense": "max"}]}
    )
    store.structure_record_run(
        sid,
        fidelity="ml",
        on_version=1,
        converged=True,
        n_steps=5,
        max_disp=0.0,
        energy=-9.0,
    )
    store.stamp_ref_meta(sid, meta)
    return sid


def test_pareto_noise_candidate_stays_evaluated(store: Any) -> None:
    """``noise`` counts as TOF about 0 — the candidate stays evaluated
    (its ``log_tof`` lands at the ~0 floor), never the provisional band."""
    qid = _mk_quest(store, "A striving")
    sid = _measured_candidate(store, qid, {"kinetics_trusted": True, "log_tof": -30.0})
    fr = quest_frontier(store, qid)
    evaluated = {c.ref_id for c in (*fr.frontier, *fr.dominated)}
    assert sid in evaluated
    assert not fr.provisional


def test_pareto_non_noise_negative_is_excluded_with_the_label(
    store: Any,
) -> None:
    """A classified negative leaves the front and lists below it with the
    label as the reason — ``log_tof`` lands stashed, never ranked."""
    qid = _mk_quest(store, "A striving")
    sid = _measured_candidate(
        store,
        qid,
        {
            "kinetics_trusted": False,
            "kinetics_note": "runs in reverse (Q/K > 1)",
            "log_tof_untrusted_value": -3.0,
        },
    )
    fr = quest_frontier(store, qid)
    assert not fr.frontier and not fr.dominated
    assert [pc.candidate.ref_id for pc in fr.provisional] == [sid]
    pc = fr.provisional[0]
    assert any("runs in reverse (Q/K > 1)" in r for r in pc.reasons)


def test_pareto_consumed_and_broken_name_their_reason(store: Any) -> None:
    qid = _mk_quest(store, "A striving")
    sid = _measured_candidate(
        store,
        qid,
        {
            "kinetics_trusted": False,
            "kinetics_note": "consumed by another reaction: NH3",
            "log_tof_untrusted_value": -6.0,
        },
    )
    fr = quest_frontier(store, qid)
    assert "consumed by another reaction: NH3" in fr.provisional[0].reasons[0] or any(
        "consumed by another reaction" in r for r in fr.provisional[0].reasons
    )


def test_pareto_untrusted_kinetics_without_note_still_reasons(
    store: Any,
) -> None:
    qid = _mk_quest(store, "A striving")
    _measured_candidate(
        store,
        qid,
        {"kinetics_trusted": False, "log_tof_untrusted_value": -4.0},
    )
    fr = quest_frontier(store, qid)
    assert any("kinetics untrusted" in r for r in fr.provisional[0].reasons)


# ── the web panel's v2_solve reading ─────────────────────────────────


def test_v2_solve_carries_the_label() -> None:
    from precis_web.pathway_kinetics import kinetics_payload

    out = kinetics_payload(
        {
            "kinetics_v2": _v2(
                -2.45e-7,
                _sign(
                    "consumed_by_other",
                    sinks={"NH3": -1e-7},
                    sources={"N2": 5e-8},
                ),
            ),
            "score": {"target": "NH3"},
        }
    )
    assert out is not None
    s = next(iter(out.values()))["v2_solve"]
    assert s["tof_sign"]["class"] == "consumed_by_other"
    assert s["tof_sign"]["label"] == "consumed by another reaction: NH3"
    assert "sinks: NH3" in s["tof_sign"]["detail"]
    assert "consumed by another reaction" in s["headline"]


def test_v2_solve_no_mf_status_renders_as_today() -> None:
    from precis_web.pathway_kinetics import kinetics_payload

    out = kinetics_payload({"kinetics_v2": {"tof_mf": -1e-3, "warnings": []}})
    assert out is not None
    s = next(iter(out.values()))["v2_solve"]
    assert s["tof_sign"] is None
    assert "no positive steady state" in s["headline"]
