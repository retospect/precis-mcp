"""Parity: the web-side verdict must SAY THE SAME THING as the engine's.

``precis_web.pathway_kinetics.kinetics_verdict`` is a hand-port of
``autocatpath.report._kinetics_verdict`` — two copies of the same thresholds
and sentence templates, in two repos, kept in step until now by a docstring
asking whoever edits one to re-sync the other. A docstring does not fail a
gate; each copy also has its own detailed suite, so a threshold changed on
one side leaves BOTH suites green and the divergence invisible. This file is
the executable version of that docstring, and the characterization baseline
for folding the pair into a shared module — a refactor that keeps it green
changed nothing.

**Two tiers, because ``autocatpath`` is not installed everywhere.** The
GitHub gate subtracts the ``catalyst`` extra (a private git source an
unauthenticated runner cannot fetch, ``.github/workflows/check.yml``), so a
test that simply imported the engine would SKIP there — green, and proving
nothing, which is the failure mode this file exists to close. So:

* :func:`test_verdict_matches_golden` compares against committed expected
  output (``tests/fixtures/pathway/kinetics_verdict_golden.json``, generated
  FROM the engine). No ``autocatpath`` needed — it gates everywhere, and it
  is what catches drift on the precis side.
* :func:`test_golden_matches_live_engine` re-derives the golden from the
  installed engine, so drift on the CATPATH side reddens too. This one needs
  ``autocatpath`` and therefore runs in the ship gate's dev image
  (``uv sync --all-extras``) and locally under ``scripts/test``, not on the
  GitHub shards.

Regenerate the golden (after an INTENDED engine change, in an environment
that has ``autocatpath``) with::

    PRECIS_UPDATE_KINETICS_GOLDEN=1 \\
        scripts/test tests/precis_web/test_pathway_kinetics_parity.py -n0

and read the resulting diff: every changed sentence is a change the web
panel is about to start showing for stored records.

Scope is the VERDICT only. The payload trims are not comparable by
construction: the engine's ``_kinetics_payload`` reads ``kinetics.json`` /
``kinetics.dft.json`` off an output directory, the web one trims an
already-stored ``meta.results.kinetics`` dict.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import pytest

from precis_web.pathway_kinetics import kinetics_verdict

GOLDEN = (
    Path(__file__).parent.parent
    / "fixtures"
    / "pathway"
    / "kinetics_verdict_golden.json"
)


def _rec(**over: Any) -> dict[str, Any]:
    """A healthy record; each case overrides the keys its branch reads."""
    rec: dict[str, Any] = {
        "tof": 3.2e-4,
        "product": "NH3",
        "coverages": {},
        "production": {},
        "warnings": [],
        "drc": {"X_RC": {}},
        "thermodynamic_drc": {"X_TRC": {}},
    }
    rec.update(over)
    return rec


#: One case per branch of the verdict, named for the branch it pins. The
#: engine's own suite covers whether each sentence is RIGHT; this corpus only
#: has to reach every branch, so a threshold moved on either side lands on at
#: least one case.
CASES: dict[str, dict[str, Any]] = {
    # 1. trust gate — an un-agreeing bracket outranks the number
    "bracket_disagrees": _rec(
        tof=6.6e-4,
        tof_bracket={
            "agree": False,
            "tof_slow": 6.6e-4,
            "tof_fast": 30.5,
            "load_bearing": ["NO@N->NO@O"],
        },
    ),
    "bracket_empty_load_bearing": _rec(
        tof=6.6e-4,
        tof_bracket={
            "agree": False,
            "tof_slow": 6.6e-4,
            "tof_fast": 30.5,
            "load_bearing": [],
            "bounded_steps": ["NO@N->NO@O", "NO@O->N@O"],
        },
    ),
    "bracket_agrees_is_not_a_gate": _rec(
        tof_bracket={"agree": True, "tof_slow": 1e-4, "tof_fast": 4e-4},
    ),
    # 2. magnitude, then direction
    "tof_missing": _rec(tof=None),
    "tof_nonfinite": _rec(tof=float("inf")),
    "tof_below_noise": _rec(tof=1e-12),
    "tof_negative": _rec(tof=-4.4e-3),
    "band_inactive": _rec(tof=1e-7),
    "band_slow_but_finite": _rec(tof=1e-4),
    "band_active": _rec(tof=5.0),
    "band_fast": _rec(tof=1e4),
    # Threshold brackets. Each branch constant gets a case just under and just
    # over it, so moving it on ONE side by more than ~10% flips a sentence and
    # reddens this file. A corpus that only sampled the middle of each band
    # would pass while pinning nothing. Sub-10% tweaks are outside what this
    # catches — the constants tests below are the backstop for those.
    "edge_under_noise": _rec(tof=9e-9),
    "edge_over_noise": _rec(tof=1.1e-8),
    "edge_under_inactive": _rec(tof=9e-7),
    "edge_over_inactive": _rec(tof=1.1e-6),
    "edge_under_slow": _rec(tof=9e-3),
    "edge_over_slow": _rec(tof=1.1e-2),
    "edge_under_active": _rec(tof=9e1),
    "edge_over_active": _rec(tof=1.1e2),
    "edge_under_dominant_coverage": _rec(coverages={"NO@fcc": 0.49, "*": 0.51}),
    "edge_over_dominant_coverage": _rec(coverages={"NO@fcc": 0.51, "*": 0.49}),
    "edge_under_single_drc": _rec(drc={"X_RC": {"a->b": 0.49, "c->d": 0.2}}),
    "edge_over_single_drc": _rec(drc={"X_RC": {"a->b": 0.51, "c->d": 0.2}}),
    "edge_under_unusable_drc": _rec(drc={"X_RC": {"a->b": 1.9}}),
    "edge_over_unusable_drc": _rec(drc={"X_RC": {"a->b": 2.1}}),
    "edge_under_brake": _rec(thermodynamic_drc={"X_TRC": {"NO@fcc": -0.49}}),
    "edge_over_brake": _rec(thermodynamic_drc={"X_TRC": {"NO@fcc": -0.51}}),
    # 3. coverage
    "coverage_product_inhibited": _rec(coverages={"NH3@fcc": 0.92, "*": 0.08}),
    "coverage_saturated_other": _rec(coverages={"NO@fcc": 0.71, "*": 0.29}),
    "coverage_no_dominant": _rec(coverages={"NO@fcc": 0.3, "H@fcc": 0.25}),
    "coverage_null_is_absent": _rec(coverages={"NO@fcc": None, "H@fcc": 0.97}),
    # 3b. selectivity
    "side_products_one": _rec(production={"NH3": 3.2e-4, "N2": 1.1e-5}),
    "side_products_many": _rec(
        production={"NH3": 3.2e-4, "N2": 1.1e-5, "N2O": 4e-6},
        selectivity=0.94,
    ),
    # 4. rate control
    "drc_unusable": _rec(drc={"X_RC": {"H2NO+H->NH2OH": -74.67}}),
    "drc_single_step": _rec(drc={"X_RC": {"NO*->NOH*": 0.8, "NOH*->NH3*": 0.15}}),
    "drc_shared": _rec(drc={"X_RC": {"a->b": 0.3, "c->d": 0.25, "e->f": 0.2}}),
    "trc_brake": _rec(thermodynamic_drc={"X_TRC": {"NO@fcc": -0.9}}),
    # 5. caveats
    "nullity_gt_one": _rec(steady_state={"nullity": 2, "t_end": 1.0e6}),
    "nullity_null_t_end": _rec(steady_state={"nullity": 3, "t_end": None}),
    "pressure_defaulted_new_wording": _rec(
        warnings=["no pressure stated for NH3; its gas exchange uses 1 bar"],
    ),
    # The engine and the port both carry a shim for this: pre-0.18 records say
    # "for the product X" where newer ones warn per gas. Pinned so neither
    # copy drops it while records of that vintage are still in the DB.
    "pressure_defaulted_pre_0_18_wording": _rec(
        warnings=["no pressure stated for the product NH3, defaulting to 1 bar"],
    ),
    # CHARACTERIZES A WART, does not endorse it. A "no pressure stated"
    # warning whose wording matches neither shim pattern produces NO caveat
    # at all — the panel silently loses the disclaimer instead of saying it
    # could not read it. Both copies behave this way, so fixing it is a
    # both-sides change; this case makes the day someone does so visible in
    # the golden diff rather than silent.
    "pressure_defaulted_unparsed_wording": _rec(
        warnings=["pressure for NH3 was not supplied — falling back to 1 bar"],
    ),
    "pressure_defaulted_two_gases": _rec(
        warnings=[
            "no pressure stated for NH3; its gas exchange uses 1 bar",
            "no pressure stated for N2; its gas exchange uses 1 bar",
        ],
    ),
    "endpoint_mismatch_one": _rec(
        warnings=["step NO*->NOH* aggregated ΔE differs from its endpoints"],
    ),
    "endpoint_mismatch_many": _rec(
        warnings=[
            "step NO*->NOH* aggregated ΔE differs from its endpoints",
            "step NOH*->NH3* aggregated ΔE differs from its endpoints",
        ],
    ),
    # everything at once — the ORDER of lines/caveats is part of the contract
    "compound": _rec(
        tof=-2.0e-3,
        coverages={"NH3@fcc": 0.88},
        production={"NH3": 3.2e-4, "N2": 1.1e-5},
        selectivity=0.9,
        drc={"X_RC": {"NO*->NOH*": 0.77}},
        thermodynamic_drc={"X_TRC": {"NO@fcc": -0.61}},
        steady_state={"nullity": 2, "t_end": 1.0e6},
        warnings=[
            "no pressure stated for NH3; its gas exchange uses 1 bar",
            "step NO*->NOH* aggregated ΔE differs from its endpoints",
        ],
    ),
}


def _golden() -> dict[str, Any]:
    return json.loads(GOLDEN.read_text(encoding="utf-8"))


@pytest.mark.skipif(
    not os.environ.get("PRECIS_UPDATE_KINETICS_GOLDEN"),
    reason="regeneration is opt-in: set PRECIS_UPDATE_KINETICS_GOLDEN=1",
)
def test_regenerate_golden() -> None:
    """Rewrite the golden from the installed engine. Not a check — it is the
    documented way to record an intended engine change, and it fails loudly
    rather than writing a half-corpus if the engine is missing."""
    report = pytest.importorskip("autocatpath.report")
    out = {
        name: report._kinetics_verdict(dict(rec)) for name, rec in sorted(CASES.items())
    }
    GOLDEN.parent.mkdir(parents=True, exist_ok=True)
    GOLDEN.write_text(
        json.dumps(out, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )


def test_golden_covers_exactly_the_corpus() -> None:
    """A case added without regenerating the golden must not pass by absence."""
    assert sorted(_golden()) == sorted(CASES)


@pytest.mark.parametrize("name", sorted(CASES))
def test_verdict_matches_golden(name: str) -> None:
    """Drift on the PRECIS side. Runs everywhere — no engine needed."""
    assert kinetics_verdict(dict(CASES[name])) == _golden()[name]


@pytest.mark.parametrize("name", sorted(CASES))
def test_golden_matches_live_engine(name: str) -> None:
    """Drift on the CATPATH side. Needs the engine, so it gates in the dev
    image and locally, not on the GitHub shards (see the module docstring)."""
    report = pytest.importorskip("autocatpath.report")
    assert report._kinetics_verdict(dict(CASES[name])) == _golden()[name]


def test_noise_threshold_is_the_same_number() -> None:
    """The one bare constant both copies branch on, pinned across the repos.
    Backstop for a threshold nudge too small to flip any bracketed case."""
    report = pytest.importorskip("autocatpath.report")
    from precis_web import pathway_kinetics

    assert pathway_kinetics._TOF_NOISE == report._TOF_NOISE


def test_corpus_reaches_every_tone() -> None:
    """A corpus that never produced a 'dead' or 'warn' verdict would pass the
    parity assertions while exercising only the happy path."""
    tones = {kinetics_verdict(dict(r))["tone"] for r in CASES.values()}
    assert tones == {"ok", "warn", "dead"}
