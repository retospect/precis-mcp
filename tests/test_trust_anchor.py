"""The named trust anchor + per-family next action (gr543165 ruling,
2026-10-31): ``trust_gate.trust_anchor`` (pathway id + gates that
passed + seed set — a barrier is credited only through it),
``second_basin`` (reproducible vs not), ``family_action`` (one action
per ruled fault family), ``struct_neb_anchor`` (the same rule binds
struct_neb runs), the ``anchor:`` line in ``view='trust'``, and
``put(kind='pathway', mode='verify')`` (multi-seed at dispatch through
the rerun machinery).
"""

from __future__ import annotations

from typing import Any

import pytest

from precis.errors import BadInput
from precis.utils import trust_gate


@pytest.fixture(autouse=True)
def _pin_engine(monkeypatch: Any) -> None:
    from precis.quest import compute as compute_mod

    monkeypatch.setenv(compute_mod._AUTOCATPATH_VERSION_ENV, "0.39.0")


def _rec(
    check: str,
    verdict: str,
    severity: str = "fatal",
    *,
    seed: int = 0,
    step: str | None = None,
    state: str | None = None,
    evidence: dict[str, Any] | None = None,
) -> dict[str, Any]:
    rec: dict[str, Any] = {
        "check": check,
        "verdict": verdict,
        "severity": severity,
        "seed": seed,
        "id": f"{state or step}#s{seed}#{check}",
        "evidence": dict(evidence or {}),
    }
    if step is not None:
        rec["step"] = step
    if state is not None:
        rec["state"] = state
    return rec


_SCOPE = trust_gate.route_scope({"route_steps": ["R->A", "A->P"]})
_FP_LO, _FP_HI = "aaaabbbbcccc", "dddd00001111"


def _basin_records(hi_seeds: list[int]) -> list[dict[str, Any]]:
    """endpoint_identity + endpoint_agreement over states R/A/P: the
    ``hi_seeds`` all land in the SAME higher basin on state A."""
    recs: list[dict[str, Any]] = []
    for st in ("R", "A", "P"):
        for s in (0, 1, 2):
            hi = st == "A" and s in hi_seeds
            recs.append(
                _rec(
                    "endpoint_identity",
                    "pass",
                    "warn",
                    seed=s,
                    state=st,
                    evidence={
                        "e_eV": -10.0 + (0.87 if hi else 0.0),
                        "fingerprint": _FP_HI if hi else _FP_LO,
                    },
                )
            )
        for s in (0, 1, 2):
            bad = st == "A" and s in hi_seeds
            recs.append(
                _rec(
                    "endpoint_agreement",
                    "fail" if bad else "pass",
                    seed=s,
                    state=st,
                    evidence={
                        "delta_meV": 866.9 if bad else 0.0,
                        "tol_eV": 0.05,
                    },
                )
            )
    return recs


def _rows(*, ea_fail: bool = False) -> list[dict[str, Any]]:
    rows = [
        {"gate": g, "verdict": "pass", "input": "…", "threshold": "…", "blocking": 0}
        for g in ("endpoint_agreement", "detachment", "neb_convergence")
    ]
    if ea_fail:
        rows[0] = {
            "gate": "endpoint_agreement",
            "verdict": "fail",
            "input": "…",
            "threshold": "…",
            "blocking": 1,
        }
    rows.append(
        {
            "gate": "spread (info, never gates)",
            "verdict": "warn",
            "input": "…",
            "threshold": "…",
            "blocking": "—",
        }
    )
    return rows


def test_anchor_names_pathway_gates_and_seeds() -> None:
    a = trust_gate.trust_anchor(_rows(), pathway="pw7", seeds={0, 1, 2})
    assert a is not None
    assert a["pathway"] == "pw7"
    assert a["seeds"] == [0, 1, 2]
    assert "endpoint_agreement" in a["gates"]
    # the info spread row is neither a gate nor a blocker
    assert all("spread" not in g for g in a["gates"])
    assert a["over"] == []


def test_anchor_none_on_uncreditable_fail_and_unprovable_seeds() -> None:
    assert (
        trust_gate.trust_anchor(_rows(ea_fail=True), pathway="pw7", seeds={0}) is None
    )
    assert trust_gate.trust_anchor(_rows(), pathway="pw7", seeds=None) is None
    # nothing passed → nothing to anchor
    rows = [dict(r, verdict="not-run") for r in _rows()]
    assert trust_gate.trust_anchor(rows, pathway="pw7", seeds={0, 1}) is None


def test_anchor_creditable_reproducible_basin() -> None:
    a = trust_gate.trust_anchor(
        _rows(ea_fail=True),
        pathway="pw7",
        seeds={0, 1, 2},
        creditable={"endpoint_agreement"},
    )
    assert a is not None and a["over"] == ["endpoint_agreement"]
    # 'endpoint_agreement' failed — it is NOT among the passing gates
    assert "endpoint_agreement" not in a["gates"]


def test_second_basin_reproducible_vs_not() -> None:
    repro, detail, lower = trust_gate.second_basin(_basin_records([1, 2]), _SCOPE)
    assert repro and "higher basin" in detail
    assert lower == {0}
    repro2, detail2, lower2 = trust_gate.second_basin(_basin_records([1]), _SCOPE)
    assert not repro2 and "no reproducible second basin" in detail2
    assert lower2 == set()


@pytest.mark.parametrize(
    ("family", "action"),
    [
        ("saddle_verified", "ci-neb-rerun"),
        ("detachment", "stop"),
        ("seed_registration", "file-gripe"),
    ],
)
def test_family_action_ruled_families(family: str, action: str) -> None:
    act = trust_gate.family_action(family, [], _SCOPE)
    assert act is not None and act["action"] == action
    assert trust_gate.family_action("wrong_binder", [], _SCOPE) is None


def test_family_action_endpoint_basin_split() -> None:
    act = trust_gate.family_action("endpoint_agreement", _basin_records([1, 2]), _SCOPE)
    assert act is not None and act["action"] == "credit-lower-basin"
    act = trust_gate.family_action("endpoint_agreement", _basin_records([1]), _SCOPE)
    assert act is not None and act["action"] == "re-place-endpoints"


def test_failed_families_maps_seed_gate() -> None:
    rows = _rows(ea_fail=True) + [
        {
            "gate": "seed independence (precis)",
            "verdict": "fail",
            "input": "…",
            "threshold": "…",
            "blocking": 1,
        }
    ]
    assert trust_gate.failed_families(rows) == [
        "endpoint_agreement",
        "seed_registration",
    ]


def test_struct_neb_anchor_same_rule() -> None:
    run: dict[str, Any] = {
        "id": 42,
        "engine": "0.39.0",
        "trust": [
            _rec("saddle_verified", "pass", step=None, seed=0),
            _rec("detachment", "pass", step=None, seed=0),
            _rec("multistart", "pass", step=None, seed=0),
        ],
        "multistart": {
            "starts": [
                {"init_seed": 0, "valid": True},
                {"init_seed": 1, "valid": True},
            ]
        },
    }
    a = trust_gate.struct_neb_anchor(run)
    assert a is not None and a["pathway"] == "struct_neb:42"
    assert a["seeds"] == [0, 1]
    # a missing multistart seed set is unprovable → no anchor, no credit
    assert trust_gate.struct_neb_anchor({**run, "multistart": None}) is None
    # a failed gate outside creditable refuses
    bad = dict(run)
    bad["trust"] = [*run["trust"], _rec("detachment", "fail", seed=1)]
    assert trust_gate.struct_neb_anchor(bad) is None


def _toon_records(hi_seeds: list[int]) -> list[dict[str, Any]]:
    recs = _basin_records(hi_seeds)
    for step in ("R->A", "A->P"):
        for s in (0, 1, 2):
            recs.append(_rec("neb_convergence", "pass", seed=s, step=step))
            recs.append(
                _rec(
                    "saddle_verified",
                    "pass",
                    seed=s,
                    step=step,
                    evidence={"barrier_eV": 0.5 + 0.1 * s},
                )
            )
    return recs


def _toon_meta(records: list[dict[str, Any]]) -> dict[str, Any]:
    scope = trust_gate.route_scope({"route_steps": ["R->A", "A->P"]})
    blocked = trust_gate.replay_barrier_gate(records, scope)
    return {
        "status": "ready",
        "autocatpath_version": "0.39.0",
        "config": {},
        "results": {
            "trust_schema": 2,
            "trust": records,
            "route_steps": ["R->A", "A->P"],
            "seeds": [0, 1, 2],
            "trust_summary": {
                "barrier": {"available": not blocked, "blocked_by": blocked}
            },
        },
    }


def test_trust_view_renders_anchor_line() -> None:
    from precis_pathway.toon_views import trust_toon

    body = trust_toon(_toon_meta(_toon_records([])), pw_handle="pw9")
    line = next(ln for ln in body.splitlines() if ln.startswith("anchor:"))
    assert "pw9" in line and "seeds [0, 1, 2]" in line
    assert "verdict: PASS" in body


def test_trust_view_anchor_none_and_family_actions() -> None:
    from precis_pathway.toon_views import trust_toon

    records = _toon_records([1]) + [
        _rec("detachment", "fail", seed=1, step="R->A", evidence={})
    ]
    body = trust_toon(_toon_meta(records), pw_handle="pw9")
    assert "anchor: none — no credit" in body
    actions = [ln for ln in body.splitlines() if ln.startswith("next action")]
    assert any("endpoint_agreement" in ln and "re-place" in ln for ln in actions)
    assert any("detachment" in ln and "no rerun" in ln for ln in actions)


def test_trust_view_creditable_basin_anchor() -> None:
    from precis_pathway.toon_views import trust_toon

    body = trust_toon(_toon_meta(_toon_records([1, 2])), pw_handle="pw9")
    line = next(ln for ln in body.splitlines() if ln.startswith("anchor:"))
    assert "credited over endpoint_agreement" in line
    assert "lower basin" in line


class TestVerifyMode:
    """``put(kind='pathway', id=N, mode='verify')`` — the multi-seed-at-
    dispatch door (gr543165): N fresh seeds through ``request_rerun``."""

    @pytest.fixture(autouse=True)
    def _engine(self) -> None:
        from tests._autocatpath import needs_engine

        needs_engine()

    def _handler(self, store: Any) -> Any:
        from precis.dispatch import Hub
        from precis_pathway.handler import PathwayHandler

        hub = Hub(store=store)
        h = PathwayHandler(hub=hub)
        h._register_with(hub)
        return h

    def _seed_pathway(self, store: Any, *, candidate_ref: Any = 1) -> None:
        with store.tx() as conn:
            store.insert_ref(
                kind="pathway",
                slug="pw-verify",
                title="verify me",
                meta={
                    "status": "ready",
                    "candidate_ref": candidate_ref,
                    "results": {"seeds": [0, 1, 2], "trust": []},
                    "requested_seeds": [0, 1, 2],
                },
                conn=conn,
            )

    def test_verify_refuses_seeds_below_two(self, store: Any) -> None:
        self._seed_pathway(store)
        with pytest.raises(BadInput, match="at least 2"):
            self._handler(store).put(id="pw-verify", mode="verify", args={"seeds": 1})

    def test_verify_refuses_no_neb_inputs(self, store: Any) -> None:
        self._seed_pathway(store, candidate_ref=None)
        with pytest.raises(BadInput, match="no NEB inputs"):
            self._handler(store).put(id="pw-verify", mode="verify", args={"seeds": 3})

    def test_verify_dispatches_fresh_seeds(self, store: Any, monkeypatch: Any) -> None:
        """A 3-seed verify on a pathway whose run registered [0,1,2]
        mints seeds [3,4,5] — one call, N jobs through request_rerun."""
        import precis.quest.compute as compute_mod

        calls: list[dict[str, Any]] = []

        def _fake_rerun(store: Any, sid: int, **kw: Any) -> str:
            calls.append(kw)
            return "autocatpath[emt] dispatched 3 seed(s) (3 new) + aggregate"

        monkeypatch.setattr(compute_mod, "request_rerun", _fake_rerun)
        self._seed_pathway(store, candidate_ref=99)
        resp = self._handler(store).put(
            id="pw-verify", mode="verify", args={"seeds": 3}
        )
        assert calls and calls[0]["seeds"] == [3, 4, 5]
        assert "3 seed(s) [3, 4, 5]" in resp.body
