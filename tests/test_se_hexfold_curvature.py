"""H1 authored-input policy and stored diagnostics: no live geometry/relaxation."""

from __future__ import annotations

import copy
import json
import math
from types import SimpleNamespace
from typing import Any, cast

import pytest

from hexfold.lattice import tube_radius
from hexfold.report import Severity
from precis.dispatch import Hub
from precis.errors import BadInput
from precis.store import Store
from precis_se import persist
from precis_se.atomic import apply as atomic_apply
from precis_se.atomic.generators import GeneratorError
from precis_se.atomic.generators import authored_foot as af
from precis_se.atomic.generators import hexfold_scene as hs
from precis_se.handler import SeHandler, _generated_section
from precis_se.ops import SeBlock

SPHERE: dict[str, Any] = {
    "name": "q",
    "at": [30, 30],
    "n": 12,
    "radius": 5,
    "tube_len": 3,
    "top": "sphere",
}


def forbidden(*_args: Any, **_kwargs: Any) -> Any:
    pytest.fail("H1 reached a held geometry, persistence, or job boundary")


@pytest.fixture
def no_compute(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(hs, "plan_scene", forbidden)
    monkeypatch.setattr(hs, "build", forbidden)
    monkeypatch.setattr(af, "_top_grid", forbidden)
    monkeypatch.setattr(af, "stick_info", forbidden)
    monkeypatch.setattr(atomic_apply, "finish_generate", forbidden)
    monkeypatch.setattr(SeHandler, "_run_pending", forbidden)


class RefStore:
    """Only a ref read is available; any unplanned access/write fails loudly."""

    def __init__(self, ref: Any = None) -> None:
        self.ref = ref
        self.reads = 0

    def get_ref(self, **_kwargs: Any) -> Any:
        self.reads += 1
        return self.ref

    def __getattr__(self, _name: str) -> Any:
        return forbidden


@pytest.mark.parametrize(
    ("features", "match"),
    [
        (
            [{**SPHERE, "top_fillet": 2.0}],
            "below the conservative authored-fillet policy bound",
        ),
        ([{**SPHERE, "n": 24, "top_fillet": 2.0}], "at most 1 top: 'sphere'.*n <= 12"),
        (
            [SPHERE, {**SPHERE, "name": "r", "top_fillet": 2.0}],
            "at most 1 top: 'sphere'",
        ),
    ],
)
def test_h1_handler_refuses_before_geometry_store_or_job(
    no_compute: None,
    monkeypatch: pytest.MonkeyPatch,
    features: list[dict[str, Any]],
    match: str,
) -> None:
    monkeypatch.setattr(hs, "top_tabled", forbidden)
    store = RefStore()
    hub = cast(Hub, SimpleNamespace(store=store, embedder=None, sibling=forbidden))
    handler = SeHandler(hub=hub)
    with pytest.raises(BadInput, match=match) as caught:
        handler.put(
            id="hexfold-h1-synthetic-01a108d5",
            text=json.dumps(
                {
                    "ops": [
                        {
                            "op": "generate",
                            "generator": "hexfold_scene",
                            "name": "below_bound",
                            "params": {"sheet": [60, 60], "features": features},
                        }
                    ]
                }
            ),
        )
    assert store.reads == 1 and store.ref is None
    if len(features) == 1 and features[0]["n"] == 12:
        message = str(caught.value)
        minimum = af.r_min_fillet(tube_radius(12, 0))
        assert f"R_min={minimum!r} A" in message
        assert f">= {minimum!r} A" in message
        assert "top_fillet 2 A" in message and "theta-p limit 12 deg" in message
        assert "not a physical stability verdict" in message


@pytest.mark.parametrize("value", [True, "3", 0.0, -1.0, math.nan, math.inf, -math.inf])
def test_h1_invalid_authored_input_has_input_error_before_lookup(
    no_compute: None,
    monkeypatch: pytest.MonkeyPatch,
    value: Any,
) -> None:
    monkeypatch.setattr(af, "_tabled_rows", forbidden)
    for key in ("top_fillet", "top_R"):
        with pytest.raises(ValueError, match=f"{key} must be"):
            af.plan_top(12, "sphere", **{key: value})
        with pytest.raises(GeneratorError, match=f"{key} must be"):
            hs.build_hexfold_scene(
                {"sheet": [60, 60], "features": [{**SPHERE, key: value}]}
            )


@pytest.mark.parametrize(
    "features",
    [
        [{**SPHERE, "top_fillet": 2.0}],
        [{**SPHERE, "n": 24}],
        [SPHERE, {**SPHERE, "name": "r"}],
    ],
)
def test_h1_test_db_absence_survives_refusal(
    no_compute: None,
    monkeypatch: pytest.MonkeyPatch,
    store: Store,
    hub_no_embedder: Hub,
    features: list[dict[str, Any]],
) -> None:
    """Real test-DB reads plus forbidden materialization/persistence boundaries."""
    slug = "hexfold-h1-synthetic-01a108d5"
    monkeypatch.setattr(type(store), "insert_ref", forbidden)
    monkeypatch.setattr(persist, "save_tree", forbidden)
    monkeypatch.setattr(hub_no_embedder, "sibling", forbidden)
    assert store.get_ref(kind="se", id=slug) is None
    assert store.get_ref(kind="structure", id=f"{slug}-below_bound") is None
    with pytest.raises(BadInput):
        SeHandler(hub=hub_no_embedder).put(
            id=slug,
            args={
                "ops": [
                    {
                        "op": "generate",
                        "generator": "hexfold_scene",
                        "name": "below_bound",
                        "params": {"sheet": [60, 60], "features": features},
                    }
                ]
            },
        )
    assert store.get_ref(kind="se", id=slug) is None
    assert store.get_ref(kind="structure", id=f"{slug}-below_bound") is None


@pytest.mark.parametrize("n", [True, 12.0, "12", 6, 13, 0, -12])
def test_h1_invalid_sphere_n_before_policy_arithmetic(
    no_compute: None,
    monkeypatch: pytest.MonkeyPatch,
    n: Any,
) -> None:
    monkeypatch.setattr(af, "r_min_fillet", forbidden)
    with pytest.raises(ValueError, match="sphere top needs n"):
        af.plan_top(n, "sphere", top_fillet=3.0)


def test_h1_exact_boundary_and_defaults_and_lids_preserve_continuation(
    no_compute: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    minimum = af.r_min_fillet(tube_radius(12, 0))
    with pytest.raises(
        ValueError, match="below the conservative authored-fillet policy bound"
    ):
        af.check_authored_sphere_fillet(12, math.nextafter(minimum, 0))
    for value in (minimum, math.nextafter(minimum, math.inf)):
        hs._normalize(
            {"sheet": [60, 60], "features": [{**SPHERE, "top_fillet": value}]}
        )

        class Continued(Exception):
            pass

        def lookup(*_args: Any, **_kwargs: Any) -> Any:
            raise Continued

        with monkeypatch.context() as patch:
            patch.setattr(af, "_tabled_rows", lookup)
            with pytest.raises(Continued):
                af.plan_top(12, "sphere", top_fillet=value)
    assert af.plan_top(12, "sphere").source == "table"
    assert af.plan_top(12, "lid", top_fillet=4.69).source == "table"
    features = [SPHERE, {**SPHERE, "name": "lid", "top": "lid", "top_fillet": 4.69}]
    assert (
        hs._normalize({"sheet": [60, 60], "features": features})[1][0].top_fillet
        is None
    )
    for lid_fillet in (None, 2.0):
        hs._normalize(
            {
                "sheet": [60, 60],
                "features": [{**SPHERE, "top": "lid", "top_fillet": lid_fillet}]
                if lid_fillet is not None
                else [{**SPHERE, "top": "lid"}],
            }
        )
    assert (
        af.default_fillet(tube_radius(12, 0), tube_radius(12, 0) + 0.5) == 2.0 < minimum
    )
    af.check_authored_sphere_fillet(12, None)


def test_h1_nonfinite_policy_bound_does_not_recommend_infinity(
    no_compute: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(af, "r_min_fillet", lambda *_args: math.inf)
    with pytest.raises(ValueError, match="no finite radius satisfies") as caught:
        af.plan_top(12, "sphere", top_fillet=3.0)
    assert "revise the sphere tube n" in str(caught.value)
    assert ">=" not in str(caught.value)
    af.check_authored_sphere_fillet(12, None)


def stored_record() -> dict[str, Any]:
    return {
        "generator": "hexfold_scene",
        "spec": "original spec",
        "n_atoms": 2406,
        "scene": {"features": [SPHERE]},
        "plan": {
            "top_plans": {
                "q": {
                    "kind": "sphere",
                    "fillet": 2.0,
                    "authored_fillet": None,
                    "scene": {"theta_p_max": 12.0, "error": "", "misses": []},
                    "theta_p_max": 0.0,
                    "bars_met": True,
                    "trial": {"theta_p_max": 0.0},
                    "grid": [{"theta_p_max": 0.0}],
                }
            }
        },
        "report": {
            "ok": True,
            "findings": [
                {"code": "old.finding", "severity": "WARN", "message": "retain me"}
            ],
        },
    }


@pytest.mark.parametrize(
    ("actual", "status"),
    [
        (12.0, "pass"),
        (13.2, "miss"),
        (None, "unknown"),
        (math.nan, "unknown"),
        (math.inf, "unknown"),
        (True, "unknown"),
        (-1.0, "unknown"),
    ],
)
def test_h1_block_report_reads_only_scene_measurement_and_retains_old_report(
    no_compute: None,
    actual: Any,
    status: str,
) -> None:
    record = stored_record()
    record["plan"]["top_plans"]["q"]["scene"]["theta_p_max"] = actual
    original = copy.deepcopy(record)
    store = RefStore(SimpleNamespace(meta={"generated": record}))
    node = SeBlock(name="ball12", bound_kind="structure", bound="stored-r4")
    text = "\n".join(_generated_section(store, node))
    assert f"; {status} (tethered scene measurement" in text
    assert "limit <= 12 deg" in text and "recorded tethered scene provenance" in text
    assert (
        "analytic conservative R_min" in text
        and "not a necessary physical stability bound" in text
    )
    assert "stored applied fillet 2 A, below policy bound" in text
    assert "omitted/default" in text
    assert "original spec" in text and "old.finding" in text and "retain me" in text
    assert "report: ok — 1 finding(s)" in text
    # NaNs don't compare equal; compare the untouched serialized record instead.
    assert json.dumps(record, sort_keys=True) == json.dumps(original, sort_keys=True)
    assert store.reads == 1


@pytest.mark.parametrize("failure", [{"error": "cannot build"}, {"misses": ["build"]}])
def test_h1_failed_candidate_zero_is_unknown(
    no_compute: None, failure: dict[str, Any]
) -> None:
    record = stored_record()
    record["plan"]["top_plans"]["q"]["scene"] = {"theta_p_max": 0.0, **failure}
    assert "measured theta-p unknown" in "\n".join(hs.stored_top_diagnostics(record))


def test_h1_dedicated_finding_keeps_exact_measured_boundary_and_severity() -> None:
    above = math.nextafter(af.THETA_P_MAX_DEG, math.inf)
    passed = hs._theta_p_band_finding("q", af.THETA_P_MAX_DEG)
    missed = hs._theta_p_band_finding("q", above)
    unknown = hs._theta_p_band_finding("q", 0.0, misses=("build",))
    assert passed.severity == Severity.INFO and "; pass " in passed.message
    assert missed.severity == Severity.WARN and f"{above!r} deg" in missed.message
    assert unknown.severity == Severity.INFO and "; unknown " in unknown.message


def test_h1_stored_policy_details_do_not_certify_authored_input(
    no_compute: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    record = stored_record()
    top = record["plan"]["top_plans"]["q"]
    top.update(fillet=3.0, authored_fillet=3.0)
    text = "\n".join(hs.stored_top_diagnostics(record))
    assert "authored 3 A" in text and "at-or-above policy bound" in text
    assert "not a necessary physical stability bound" in text
    top["fillet"] = None
    assert "stored applied fillet unknown, unknown" in "\n".join(
        hs.stored_top_diagnostics(record)
    )
    monkeypatch.setattr(hs, "r_min_fillet", lambda *_args: math.inf)
    assert "nonfinite (no finite policy minimum)" in "\n".join(
        hs.stored_top_diagnostics(record)
    )


def test_h1_missing_and_malformed_provenance_and_lid_are_truthful(
    no_compute: None,
) -> None:
    record = stored_record()
    top = record["plan"]["top_plans"]["q"]
    del top["scene"]
    assert "measured theta-p unknown" in "\n".join(hs.stored_top_diagnostics(record))
    record["scene"]["features"][0] = {**SPHERE, "n": True}
    assert "R_min: unknown" in "\n".join(hs.stored_top_diagnostics(record))
    record["scene"] = None
    assert "R_min: unknown" in "\n".join(hs.stored_top_diagnostics(record))
    record["plan"]["top_plans"]["q"] = None
    assert "measured theta-p unknown" in "\n".join(hs.stored_top_diagnostics(record))
    record["plan"] = None
    assert "measured theta-p: unknown" in "\n".join(hs.stored_top_diagnostics(record))
    record = stored_record()
    record["scene"]["features"][0] = {**SPHERE, "top": "lid"}
    record["plan"]["top_plans"]["q"]["kind"] = "lid"
    assert "policy not applied to lids" in "\n".join(hs.stored_top_diagnostics(record))
