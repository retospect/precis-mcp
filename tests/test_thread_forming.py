"""Rung 3c's numbers: what a screw threads into when the member is soft
(``se-off-the-shelf-fabrication.md`` engine 2b).

Two kinds of test, and the split matters. The **data guards** check the
transcribed half against arithmetic nobody can fudge (a metric minor
diameter is d − P, full stop); the **API tests** check that a rule of
thumb is applied where it is claimed and refused where it isn't. Neither
asserts that a house factor *is right* — 0.8 × D is a judgement, and a
test that pinned it would only be asserting that the file says what the
file says.
"""

from __future__ import annotations

import pytest

from precis import thread_forming as tf


class TestCoreTable:
    def test_every_metric_minor_is_the_pitch_subtracted(self) -> None:
        """d − P, checked against the pitches the series registry carries
        — two independently transcribed files agreeing is the guard."""
        from precis import component_series as cs

        series = cs.find_series("iso-4762")
        assert series is not None
        pitches = {
            str(s.specs["thread_size"]): float(s.specs["thread_pitch"])
            for s in series.sizes
        }
        for size in tf.thread_sizes():
            if not size.startswith("M") or size not in pitches:
                continue
            core = tf.core_hole(size, material_class="thermoplastic-rigid")
            tapped = tf.tapping_drill(size, pitches[size])
            assert tapped is not None and core is not None
            major = float(size[1:])
            assert tapped.diameter_mm == pytest.approx(major - pitches[size], abs=1e-9)

    def test_tapping_screw_sizes_are_carried_as_st_keys(self) -> None:
        sizes = tf.thread_sizes()
        assert "ST4.2" in sizes and "ST2.9" in sizes
        assert sizes.index("M2") < sizes.index("M8")  # ordered by major Ø

    def test_a_size_nobody_tabulated_returns_none_rather_than_a_guess(self) -> None:
        assert tf.core_hole("M42") is None
        assert tf.tapping_drill("M42", 4.5) is None


class TestCoreHole:
    def test_the_factor_is_applied_to_the_major_diameter(self) -> None:
        core = tf.core_hole("ST6.3", material_class="thermoplastic-rigid")
        assert core is not None
        rule = tf.material("thermoplastic-rigid")
        assert rule is not None and rule.core_hole_factor is not None
        assert core.diameter_mm == pytest.approx(rule.core_hole_factor * 6.25, abs=0.01)

    def test_the_minor_diameter_is_a_floor_not_a_suggestion(self) -> None:
        """A tough-plastic factor on a fine metric thread lands *below*
        the thread's own minor Ø, and a hole that small does not take the
        screw — it splits the boss."""
        core = tf.core_hole("M3", material_class="thermoplastic-tough")
        assert core is not None
        assert core.diameter_mm == pytest.approx(2.5)  # M3 minor, not 0.75×3
        assert "floored at the minor" in core.source

    def test_metal_has_no_forming_hole_at_all(self) -> None:
        """A cut thread's drill is d − P; there is no factor to apply, and
        inventing one would be inventing a process."""
        assert tf.core_hole("M4", material_class="metal") is None

    def test_an_uncharacterized_material_is_none_not_the_default(self) -> None:
        assert tf.material("unobtainium") is None
        assert tf.core_hole("M4", material_class="unobtainium") is None

    def test_every_source_says_which_half_is_a_rule(self) -> None:
        core = tf.core_hole("M4")
        assert core is not None
        assert "NOT a standard" in core.source
        assert "ISO" in core.source  # …and which half is transcribed


class TestPockets:
    def test_an_insert_pocket_is_deeper_than_the_insert(self) -> None:
        pocket = tf.insert_pocket("M3", insert_length_mm=5.74)
        assert pocket is not None
        assert pocket.depth_mm is not None
        assert pocket.depth_mm > 5.74
        assert pocket.chamfer_mm and pocket.chamfer_mm > 0

    def test_an_insert_size_nobody_stocks_is_none(self) -> None:
        assert tf.insert_pocket("M12", insert_length_mm=10.0) is None

    def test_a_nut_pocket_carries_its_across_flats(self) -> None:
        """A hex pocket described by a diameter alone is a hole the nut
        spins in."""
        pocket = tf.nut_pocket(across_flats_mm=5.5, nut_height_mm=2.4)
        assert pocket.across_flats_mm is not None
        assert pocket.across_flats_mm > 5.5  # the fit
        assert pocket.diameter_mm > pocket.across_flats_mm  # circumscribed

    def test_the_insert_series_is_named_by_the_data_not_the_caller(self) -> None:
        assert tf.insert_series_id() == "insert-brass-heatset"


class TestMaterials:
    def test_plastic_wants_more_engagement_than_steel(self) -> None:
        metal = tf.material("metal")
        plastic = tf.material("thermoplastic-rigid")
        assert metal is not None and plastic is not None
        assert plastic.min_engagement_d > metal.min_engagement_d

    def test_a_boss_is_at_least_two_diameters_across(self) -> None:
        boss = tf.boss("M3", material_class="thermoplastic-rigid")
        assert boss is not None
        assert boss.diameter_mm >= 6.0

    def test_strategies_are_documented_prose_not_bare_ids(self) -> None:
        strategies = tf.strategies()
        assert set(tf.STRATEGIES) <= set(strategies)
        for entry in strategies.values():
            assert entry["title"] and entry["note"]
