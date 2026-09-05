"""Catalog → geometry for bound components (:mod:`precis_se.catalog`,
``se-off-the-shelf-fabrication.md`` rung 2b).

The module is pure arithmetic over a spec dict, so these are unit tests
with no store. Two things they pin beyond the numbers:

- every envelope this module emits **parses with the cad DSL** — the
  string is not merely plausible, it round-trips;
- an absent spec yields a *named* gap, never a fallback shape.
"""

from __future__ import annotations

import math

import pytest

from precis.cad import dsl as cad_dsl
from precis_se import catalog

# ISO 4762 M6x30 and ISO 4032 M6, in METRES (what the caller converts to).
M6_SCREW = {
    "thread_size": "M6",
    "outer_diameter": 0.006,
    "head_diameter": 0.010,
    "head_height": 0.006,
    "drive_size": 0.005,
    "length": 0.030,
}
M6_NUT = {
    "thread_size": "M6",
    "inner_diameter": 0.006,
    "across_flats": 0.010,
    "height": 0.0052,
}
M6_WASHER = {
    "inner_diameter": 0.0064,
    "outer_diameter": 0.012,
    "thickness": 0.0016,
}
DN25_TUBE = {
    "outer_diameter": 0.0337,
    "inner_diameter": 0.0272,
    "wall_thickness": 0.00325,
    "length_overall": 2.0,
}
BEARING_608 = {
    "inner_diameter": 0.008,
    "outer_diameter": 0.022,
    "width": 0.007,
}


class TestScrew:
    def test_envelope_bounds_head_and_shank(self) -> None:
        d = catalog.derive("fastener", M6_SCREW)
        assert d.ok
        spec = cad_dsl.parse(d.envelope or "")
        assert spec.alias == "cyl"
        # head Ø10 is wider than the Ø6 shank, so the bound is the head
        assert spec.params["r"] == pytest.approx(0.005)
        # bounding height is head + shank, not either alone
        assert spec.params["h"] == pytest.approx(0.036)

    def test_ports_are_head_shank_thread(self) -> None:
        d = catalog.derive("fastener", M6_SCREW)
        assert d.ports is not None
        assert set(d.ports) == {"head", "shank", "thread"}
        assert d.ports["head"].roles == ["bearing-face"]
        assert d.ports["thread"].roles == ["thread"]

    def test_head_faces_back_along_the_axis(self) -> None:
        d = catalog.derive("fastener", M6_SCREW)
        assert d.ports is not None
        assert d.ports["head"].direction == [0.0, 0.0, -1.0]
        assert d.ports["thread"].direction == [0.0, 0.0, 1.0]

    def test_shank_port_sits_under_the_head(self) -> None:
        d = catalog.derive("fastener", M6_SCREW)
        assert d.ports is not None
        assert d.ports["shank"].annotations["axial_offset_m"] == pytest.approx(0.006)
        assert d.ports["shank"].annotations["diameter_m"] == pytest.approx(0.006)

    def test_a_screw_with_no_length_names_the_missing_spec(self) -> None:
        d = catalog.derive(
            "fastener", {k: v for k, v in M6_SCREW.items() if k != "length"}
        )
        assert not d.ok
        assert d.envelope is None
        assert d.why_not is not None and "length" in d.why_not


class TestNut:
    def test_envelope_is_a_hex_prism_at_the_circumradius(self) -> None:
        d = catalog.derive("fastener", M6_NUT)
        assert d.ok
        spec = cad_dsl.parse(d.envelope or "")
        assert spec.alias == "hex"
        # across-flats 10mm -> circumradius = s/sqrt(3)
        assert spec.params["r"] == pytest.approx(0.010 / math.sqrt(3.0))
        assert spec.params["h"] == pytest.approx(0.0052)

    def test_the_bore_is_a_thread_port(self) -> None:
        d = catalog.derive("fastener", M6_NUT)
        assert d.ports is not None
        assert set(d.ports) == {"face_a", "face_b", "thread"}
        assert "thread" in d.ports["thread"].roles
        assert d.ports["thread"].annotations["diameter_m"] == pytest.approx(0.006)

    def test_a_nut_is_not_mistaken_for_a_washer(self) -> None:
        """Both carry an inner diameter; across_flats is what decides."""
        d = catalog.derive("fastener", M6_NUT)
        assert cad_dsl.parse(d.envelope or "").alias == "hex"


class TestWasher:
    def test_envelope_is_a_disc_at_the_outer_diameter(self) -> None:
        d = catalog.derive("fastener", M6_WASHER)
        assert d.ok
        spec = cad_dsl.parse(d.envelope or "")
        assert spec.alias == "cyl"
        assert spec.params["r"] == pytest.approx(0.006)
        assert spec.params["h"] == pytest.approx(0.0016)

    def test_ports_are_two_faces_and_a_bore(self) -> None:
        d = catalog.derive("fastener", M6_WASHER)
        assert d.ports is not None
        assert set(d.ports) == {"face_a", "face_b", "bore"}
        assert d.ports["bore"].annotations["diameter_m"] == pytest.approx(0.0064)


class TestFastenerFormDispatch:
    def test_a_hex_head_screw_takes_the_screw_rule_not_the_nut_rule(self) -> None:
        """A hex-head screw carries BOTH head_height and across_flats. The
        head rule must win — its envelope is head-over-shank, and a hex
        prism at the head's across-flats would omit the entire shank."""
        hex_bolt = {
            "outer_diameter": 0.006,
            "across_flats": 0.010,
            "head_height": 0.004,
            "head_diameter": 0.011,
            "length": 0.020,
        }
        d = catalog.derive("fastener", hex_bolt)
        spec = cad_dsl.parse(d.envelope or "")
        assert spec.alias == "cyl"
        assert spec.params["h"] == pytest.approx(0.024)

    def test_a_fastener_with_no_discriminator_says_which_specs_would_help(
        self,
    ) -> None:
        d = catalog.derive("fastener", {"thread_size": "M6", "grade": "8.8"})
        assert not d.ok
        assert d.why_not is not None
        for hint in ("head_diameter", "across_flats", "thickness"):
            assert hint in d.why_not


class TestTube:
    @pytest.mark.parametrize("category", ["pipe", "profile"])
    def test_envelope_spans_the_cut_length(self, category: str) -> None:
        d = catalog.derive(category, DN25_TUBE)
        assert d.ok
        spec = cad_dsl.parse(d.envelope or "")
        assert spec.params["r"] == pytest.approx(0.01685)
        assert spec.params["h"] == pytest.approx(2.0)

    def test_ports_are_two_ends_plus_the_bore(self) -> None:
        d = catalog.derive("pipe", DN25_TUBE)
        assert d.ports is not None
        assert set(d.ports) == {"end_a", "end_b", "bore"}
        assert d.ports["end_b"].annotations["axial_offset_m"] == pytest.approx(2.0)

    def test_a_solid_profile_gets_no_bore_port(self) -> None:
        solid = {k: v for k, v in DN25_TUBE.items() if k != "inner_diameter"}
        d = catalog.derive("profile", solid)
        assert d.ports is not None
        assert "bore" not in d.ports

    def test_a_tube_with_no_length_is_a_named_gap(self) -> None:
        stock = {k: v for k, v in DN25_TUBE.items() if k != "length_overall"}
        d = catalog.derive("pipe", stock)
        assert not d.ok
        assert d.why_not is not None and "length_overall" in d.why_not


class TestBearing:
    def test_envelope_is_the_outer_race(self) -> None:
        d = catalog.derive("bearing", BEARING_608)
        assert d.ok
        spec = cad_dsl.parse(d.envelope or "")
        assert spec.params["r"] == pytest.approx(0.011)
        assert spec.params["h"] == pytest.approx(0.007)

    def test_bore_and_od_are_both_seats(self) -> None:
        d = catalog.derive("bearing", BEARING_608)
        assert d.ports is not None
        assert set(d.ports) == {"face_a", "face_b", "od", "bore"}
        assert "seat" in d.ports["od"].roles
        assert "seat" in d.ports["bore"].roles

    def test_the_bearing_specific_bore_spec_wins_over_the_generic_one(self) -> None:
        specs = dict(BEARING_608, bore_diameter_bearing=0.0079)
        d = catalog.derive("bearing", specs)
        assert d.ports is not None
        assert d.ports["bore"].annotations["diameter_m"] == pytest.approx(0.0079)


class TestSheet:
    def test_a_stock_thickness_alone_is_an_honest_gap_not_a_square(self) -> None:
        """The NORMAL case for sheet: the catalog knows the thickness, the
        design owns the outline. Inventing a plan size would put a made-up
        dimension into a clearance check."""
        d = catalog.derive("laminate", {"thickness": 0.003})
        assert not d.ok
        assert d.why_not is not None
        assert "plan size" in d.why_not
        assert "width" in d.why_not and "height" in d.why_not

    def test_with_a_plan_size_it_is_a_box_at_stock_thickness(self) -> None:
        d = catalog.derive(
            "laminate", {"thickness": 0.003, "width": 0.2, "height": 0.1}
        )
        assert d.ok
        spec = cad_dsl.parse(d.envelope or "")
        assert spec.alias == "box"
        assert spec.params["w"] == pytest.approx(0.2)
        assert spec.params["d"] == pytest.approx(0.1)
        assert spec.params["h"] == pytest.approx(0.003)


class TestDegradation:
    def test_an_unknown_category_names_the_ones_we_have(self) -> None:
        d = catalog.derive("adhesive", {"mass": 0.1})
        assert not d.ok
        assert d.why_not is not None
        assert "adhesive" in d.why_not
        assert "fastener" in d.why_not

    @pytest.mark.parametrize("category", [None, ""])
    def test_no_category_is_reported_not_crashed(self, category: str | None) -> None:
        d = catalog.derive(category, M6_SCREW)
        assert not d.ok
        assert d.why_not is not None and "category" in d.why_not

    @pytest.mark.parametrize("bad", [0.0, -1.0, None, "wide", float("nan")])
    def test_a_nonpositive_or_unparseable_dimension_counts_as_missing(
        self, bad: object
    ) -> None:
        """A zero-radius cylinder is not a degraded envelope, it's a wrong
        one — so a bad value must fail the same way an absent one does."""
        d = catalog.derive("fastener", dict(M6_WASHER, outer_diameter=bad))
        assert not d.ok
        assert d.why_not is not None and "outer_diameter" in d.why_not


class TestDslRoundTrip:
    @pytest.mark.parametrize(
        ("category", "specs"),
        [
            ("fastener", M6_SCREW),
            ("fastener", M6_NUT),
            ("fastener", M6_WASHER),
            ("pipe", DN25_TUBE),
            ("bearing", BEARING_608),
            ("laminate", {"thickness": 0.003, "width": 0.2, "height": 0.1}),
        ],
    )
    def test_every_generated_envelope_parses(self, category: str, specs: dict) -> None:
        d = catalog.derive(category, specs)
        assert d.ok
        cad_dsl.parse(d.envelope or "")  # raises DslError if malformed

    def test_millimetre_scale_metres_do_not_become_exponents(self) -> None:
        """`%g` would render 0.0016 as 1.6e-03, which the DSL's token
        regex cannot parse — the exact reason _fmt is fixed-point."""
        d = catalog.derive("fastener", M6_WASHER)
        assert "e-" not in (d.envelope or "")
        assert cad_dsl.parse(d.envelope or "").params["h"] == pytest.approx(0.0016)


# ── the wiring: bound component → derived envelope/ports ─────────────


class TestToMetres:
    def test_millimetres_convert(self) -> None:
        assert catalog.to_metres(30.0, "mm") == pytest.approx(0.030)

    def test_metres_pass_through(self) -> None:
        assert catalog.to_metres(2.0, "m") == pytest.approx(2.0)

    @pytest.mark.parametrize("unit", [None, "USD", "kg", "N", "K"])
    def test_a_non_length_unit_yields_none_not_the_raw_number(
        self, unit: str | None
    ) -> None:
        """Passing the raw number through is how a 2000mm tube becomes a
        2000m one — an envelope is the last place a silently-wrong
        magnitude should reach."""
        assert catalog.to_metres(2000.0, unit) is None
