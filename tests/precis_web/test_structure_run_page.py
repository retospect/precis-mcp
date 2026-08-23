"""The per-run page — ``GET /structure/{slug}/run/{run_id}``.

The run-cube card is a glance (rung, numbers, settled or not); this page is
the "…and what does that mean?" behind it. What is guarded here: a run is
looked up *under its design* (a foreign run id must 404, never render under
the wrong slug), the numbers arrive with their glosses, the convergence curve
is drawn from ``struct_frames``, and the identity hashes are off the card and
folded away on the page.

Real Postgres (the ``store`` fixture) — the helpers are raw SQL, which the web
``FakeStore`` does not parse. See ``test_structure_sql.py``.
"""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from precis.dispatch import Hub
from precis.handlers._slug_ref_shared import resolve_live_slug_ref
from precis.handlers.structure import StructureHandler
from precis_web.app import create_app
from precis_web.config import WebConfig
from precis_web.routes.structure import (
    RUNG_INFO,
    _curve_svg,
    _run_detail,
    _run_origin,
    _run_rows,
    _viewer,
    rung_info,
)

from .conftest import FakeRuntime

_SI2 = json.dumps(
    {
        "cell": {"a": 5.43, "b": 5.43, "c": 5.43, "pbc": [True, True, True]},
        "ops": [
            {"op": "add_atom", "element": "Si", "frac": [0.0, 0.0, 0.0]},
            {"op": "add_atom", "element": "Si", "frac": [0.25, 0.25, 0.25]},
        ],
    }
)


def _seed(store, slug: str, **kw):
    """A Si2 design + one injected ml run carrying a convergence curve and a
    relaxed geometry (the shape the page is written for)."""
    StructureHandler(hub=Hub(store=store)).put(id=slug, text=_SI2)
    ref = resolve_live_slug_ref(store, kind="structure", id=slug)
    args = {
        "fidelity": "ml",
        "on_version": 1,
        "converged": True,
        "n_steps": 3,
        "max_disp": 0.011,
        "energy": -191.5154,
        "max_force": 0.0481,
        "model": "mace_mp",
        "status": "succeeded",
        "curve": [0.94, 0.31, 0.0481],
        "cache_key": "f952cfb4b248af35" + "0" * 48,
        "structure_sha": "3da4024fe1" + "0" * 54,
        "final_geometry": {
            "frac": [[0.004, 0.004, 0.004], [0.256, 0.256, 0.256]],
            "lattice": [[5.43, 0, 0], [0, 5.43, 0], [0, 0, 5.43]],
        },
    }
    args.update(kw)
    run_id = store.structure_record_run(ref.id, **args)
    return ref, run_id


def _client(store) -> TestClient:
    app = create_app(runtime=FakeRuntime(store), web_config=WebConfig(corpus_dir=None))
    return TestClient(app)


# ── the rung glossary ────────────────────────────────────────────────────


def test_rung_info_covers_the_pickable_ladder() -> None:
    """Every rung the Relax picker offers has a gloss — the picker renders
    ``rung_info.<rung>.blurb`` directly, so a missing entry is a blank
    tooltip on the button a human is about to press."""
    for rung in ("clean", "ml", "dft"):
        assert RUNG_INFO[rung]["blurb"]
        assert RUNG_INFO[rung]["label"]


def test_rung_info_widens_a_dft_variant_and_falls_back() -> None:
    # A recorded ``dft-fast`` / ``dft-tight`` inherits the DFT wording…
    assert rung_info("dft-fast") is RUNG_INFO["dft"]
    assert rung_info("dft-tight") is RUNG_INFO["dft"]
    # …and a rung we have never heard of says so rather than rendering blank.
    assert rung_info("warpdrive")["blurb"]
    assert rung_info(None)["label"] == "unrecognised rung"


def test_run_origin_prefers_import_then_cache_then_job() -> None:
    assert "outside dataset" in _run_origin("external", cached=True, has_job=True)
    assert "reused" in _run_origin("computed", cached=True, has_job=False)
    assert "job" in _run_origin("computed", cached=False, has_job=True)
    assert "locally" in _run_origin("computed", cached=False, has_job=False)


def test_curve_svg_needs_two_points() -> None:
    assert _curve_svg([]) == ""
    assert _curve_svg([0.5]) == ""  # one point is a dot, not a trend
    pts = _curve_svg([1.0, 0.5, 0.0]).split()
    assert len(pts) == 3
    # Falling force ⇒ the polyline descends: y grows down in SVG space.
    ys = [float(p.split(",")[1]) for p in pts]
    assert ys[0] < ys[-1]


# ── _run_detail ──────────────────────────────────────────────────────────


def test_run_detail_carries_numbers_curve_and_gloss(store) -> None:
    ref, run_id = _seed(store, "runpage_si2")
    d = _run_detail(store, ref.id, run_id)
    assert d is not None
    assert d["fidelity"] == "ml"
    assert d["model"] == "mace_mp"
    assert d["energy"] == pytest.approx(-191.5154, abs=1e-4)
    assert d["max_force"] == pytest.approx(0.0481, abs=1e-6)
    assert d["curve"] == [
        pytest.approx(0.94),
        pytest.approx(0.31),
        pytest.approx(0.0481),
    ]
    assert d["curve_svg"]
    assert d["has_geometry"] is True
    assert d["rung_help"] == RUNG_INFO["ml"]["blurb"]
    # Nothing dispatched it and it was not a cache hit → computed locally.
    assert d["job_id"] is None
    assert "locally" in d["origin"]


def test_run_detail_flags_a_cache_reuse(store) -> None:
    """A cache hit still records a per-design row; ``params.cached`` is what
    tells a reader no compute was spent on it."""
    ref, run_id = _seed(store, "runpage_cached", params={"cached": True})
    d = _run_detail(store, ref.id, run_id)
    assert d is not None
    assert d["cached"] is True
    assert "reused" in d["origin"]


def test_run_detail_is_scoped_to_its_design(store) -> None:
    """A run id that belongs to another design is not this design's run."""
    ref_a, run_a = _seed(store, "runpage_a")
    ref_b, _run_b = _seed(store, "runpage_b")
    assert _run_detail(store, ref_b.id, run_a) is None
    assert _run_detail(store, ref_a.id, run_a) is not None
    assert _run_detail(store, ref_a.id, 10**9) is None


# ── the routes ───────────────────────────────────────────────────────────


def test_run_page_explains_the_rung_and_the_numbers(store) -> None:
    ref, run_id = _seed(store, "runpage_render")
    resp = _client(store).get(f"/structure/{ref.slug}/run/{run_id}")
    assert resp.status_code == 200
    body = resp.text
    assert "ml relax" in body
    assert "machine-learned interatomic potential" in body  # the rung, in words
    assert "-191.5154" in body and "0.0481" in body
    assert "Settled." in body
    # The convergence curve is drawn, not just tabulated.
    assert "<polyline" in body
    # A relax is not a barrier — the page says so once, plainly.
    assert "computes no barrier" in body
    # The hashes are here (folded), and the way back to the atoms is offered.
    assert "Technical details" in body
    assert f"/structure/{ref.slug}?run={run_id}" in body


def test_run_page_404s_for_a_foreign_run(store) -> None:
    ref_a, run_a = _seed(store, "runpage_foreign_a")
    ref_b, _ = _seed(store, "runpage_foreign_b")
    resp = _client(store).get(f"/structure/{ref_b.slug}/run/{run_a}")
    assert resp.status_code == 404
    assert f"r{run_a}" in resp.text


def test_detail_card_links_to_the_run_and_drops_the_hashes(store) -> None:
    """The card keeps the glance and loses the machine identity: the full
    cache key / structure sha now live on the run page's folded details."""
    ref, run_id = _seed(store, "runpage_card")
    body = _client(store).get(f"/structure/{ref.slug}").text
    assert f"/structure/{ref.slug}/run/{run_id}" in body
    assert "f952cfb4b248af35" not in body
    assert "3da4024fe1" not in body
    # …and the rung chip explains itself in place.
    assert RUNG_INFO["ml"]["blurb"][:40] in body


def test_detail_page_has_a_help_panel(store) -> None:
    ref, _run_id = _seed(store, "runpage_help")
    body = _client(store).get(f"/structure/{ref.slug}").text
    assert 'aria-label="What is on this page?"' in body
    assert "Compute runs" in body and "Eyes &amp; measures" in body


def test_detail_run_param_pins_that_runs_geometry(store) -> None:
    """``?run=`` shows a specific run's atoms (the run page's "show these
    atoms" link); a junk value degrades to the default view, never a 500."""
    ref, run_id = _seed(store, "runpage_pin")
    runs = _run_rows(store, ref.id)
    assert _viewer(store, ref, runs, pin_run_id=run_id)["relaxed_run_id"] == run_id
    # A run with no stored geometry pins to "input only" rather than to some
    # other run's atoms.
    assert _viewer(store, ref, runs, pin_run_id=10**9)["relaxed"] is None

    client = _client(store)
    assert client.get(f"/structure/{ref.slug}?run={run_id}").status_code == 200
    assert client.get(f"/structure/{ref.slug}?run=banana").status_code == 200
