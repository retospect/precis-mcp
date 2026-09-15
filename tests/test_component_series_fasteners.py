"""Guards on the 2026-09-15 fastener batch — the hex/Torx head set, the
Torx tapping (pointy) screws, locking nuts, set screws and heat-set
inserts (``se-off-the-shelf-fabrication.md`` rung 2c).

These are transcription guards, which is the only kind of test curated
standards data can usefully have: a table cannot be unit-tested against
itself, so every assertion here is either **cross-file** (two independent
transcriptions of the same standard must agree) or **geometric** (a head
is wider than its shank, a countersink's depth follows from its
diameters). A typo that survives all of them is one that happens to be
self-consistent in three places at once.

`test_component_series.py` owns the registry's *mechanics* (resolution,
minting, units); this file owns the new rows' *content*.
"""

from __future__ import annotations

import pytest

from precis import component_series as cs

#: ISO 261 coarse pitch — the single table every metric screw series here
#: must agree with. Transcribed once, on purpose: it is the cross-check.
COARSE_PITCH = {
    "M2": 0.4,
    "M2.5": 0.45,
    "M3": 0.5,
    "M3.5": 0.6,
    "M4": 0.7,
    "M5": 0.8,
    "M6": 1.0,
    "M8": 1.25,
    "M10": 1.5,
    "M12": 1.75,
    "M16": 2.0,
    "M20": 2.5,
}

NEW_SCREW_SERIES = (
    "iso-10642",
    "iso-7380",
    "iso-14579",
    "iso-14581",
    "iso-14583",
    "iso-4026",
)
TAPPING_SERIES = ("iso-14585", "iso-14586")


def _series(series_id: str) -> cs.Series:
    series = cs.find_series(series_id)
    assert series is not None, f"{series_id} missing from the registry"
    return series


class TestPitchAgreement:
    @pytest.mark.parametrize("series_id", NEW_SCREW_SERIES)
    def test_every_metric_screw_agrees_with_iso_261(self, series_id: str) -> None:
        for size in _series(series_id).sizes:
            expected = COARSE_PITCH.get(size.key)
            if expected is None:
                continue
            assert size.specs["thread_pitch"] == pytest.approx(expected), (
                f"{series_id} {size.key}"
            )

    def test_the_torx_cap_screw_shares_the_socket_cap_head(self) -> None:
        """ISO 14579 is ISO 4762 with a different recess. If the two rows
        ever disagree about a head diameter, one of them was retyped."""
        cap = {s.key: s.specs for s in _series("iso-4762").sizes}
        for size in _series("iso-14579").sizes:
            if size.key not in cap:
                continue
            assert size.specs["head_diameter"] == cap[size.key]["head_diameter"]
            assert size.specs["head_height"] == cap[size.key]["head_height"]


class TestHeadGeometry:
    @pytest.mark.parametrize("series_id", (*NEW_SCREW_SERIES, *TAPPING_SERIES))
    def test_a_head_is_wider_than_its_shank(self, series_id: str) -> None:
        for size in _series(series_id).sizes:
            head = size.specs.get("head_diameter")
            if head is None:  # a set screw has no head at all
                continue
            assert float(head) > float(size.specs["outer_diameter"]), size.key

    @pytest.mark.parametrize("series_id", (*NEW_SCREW_SERIES, *TAPPING_SERIES))
    def test_a_drive_fits_inside_its_own_head(self, series_id: str) -> None:
        for size in _series(series_id).sizes:
            head = size.specs.get("head_diameter")
            drive = size.specs.get("drive_size")
            if head is None or drive is None:
                continue
            assert float(drive) < float(head), f"{series_id} {size.key}"

    @pytest.mark.parametrize("series_id", ("iso-10642", "iso-14581", "iso-14586"))
    def test_countersunk_rows_carry_an_angle_and_no_head_height(
        self, series_id: str
    ) -> None:
        """The sink depth is (head Ø − shank Ø)/2 by geometry, so the file
        deliberately does not carry it — a second copy could disagree with
        the cone it describes."""
        series = _series(series_id)
        assert series.specs["head_angle"] == 90.0
        assert series.specs["head_form"] == "countersunk"
        for size in series.sizes:
            assert "head_height" not in size.specs

    def test_the_derived_sink_depth_matches_the_published_head_height(self) -> None:
        """ISO 14586 publishes k_max, and it equals the cone depth at every
        size — the check that the derivation is the standard's own."""
        published = {"ST2.9": 1.7, "ST3.5": 2.35, "ST4.2": 2.6, "ST6.3": 3.15}
        for size in _series("iso-14586").sizes:
            want = published.get(size.key)
            if want is None:
                continue
            derived = (
                float(size.specs["head_diameter"]) - float(size.specs["outer_diameter"])
            ) / 2.0
            assert derived == pytest.approx(want, abs=0.06), size.key


class TestDrives:
    def test_every_new_screw_series_is_hex_socket_or_torx(self) -> None:
        """The house drive policy, enforced at the data: nothing we add
        should need a cross-recess screwdriver."""
        for series_id in (*NEW_SCREW_SERIES, *TAPPING_SERIES):
            series = _series(series_id)
            assert series.specs["drive_type"] in ("socket", "torx"), series_id

    def test_torx_rows_carry_the_t_number_as_well_as_the_millimetres(self) -> None:
        for size in _series("iso-14583").sizes:
            assert str(size.specs["drive_code"]).startswith("T")
            assert float(size.specs["drive_size"]) > 0


class TestTappingScrews:
    def test_pointy_screws_are_st_sizes_with_a_tapping_point(self) -> None:
        for series_id in TAPPING_SERIES:
            series = _series(series_id)
            assert series.specs["point_type"] == "tapping-c"
            assert all(s.key.startswith("ST") for s in series.sizes)

    def test_their_thread_sizes_reach_the_thread_forming_table(self) -> None:
        """The screw and the hole it wants must be keyed the same way, or
        the fastening pass silently stamps nothing."""
        from precis import thread_forming as tf

        known = set(tf.thread_sizes())
        for series_id in TAPPING_SERIES:
            for size in _series(series_id).sizes:
                assert size.key in known, f"{series_id} {size.key}"

    def test_a_pointy_screw_resolves_from_the_words_a_person_uses(self) -> None:
        hits = cs.resolve("pointy screw ST4.2x16")
        assert hits and hits[0].series.series_id in TAPPING_SERIES
        assert hits[0].length == 16.0


class TestStocking:
    def test_every_tier_is_one_of_the_three(self) -> None:
        for series in cs.load_series():
            for size in series.sizes:
                assert size.stocking in (None, *cs.STOCKING_TIERS)

    def test_an_unrecognized_tier_is_dropped_rather_than_carried(self) -> None:
        """A tier nobody ranks is worse than no tier: it reads as judged
        when it isn't, and the resolver has no weight for it."""
        assert cs._stocking("universal") == "universal"
        assert cs._stocking("UNIVERSAL ") == "universal"  # case/space folded
        assert cs._stocking("plentiful") is None
        assert cs._stocking(None) is None

    def test_the_everyday_sizes_are_marked_universal(self) -> None:
        cap = {s.key: s.stocking for s in _series("iso-4762").sizes}
        assert cap.get("M4") == "universal"
        assert cap.get("M20") == "specialty"

    def test_availability_breaks_a_tie_but_never_beats_fit(self) -> None:
        """A universal M4 outranks a specialty M4 of the same head form;
        a *wrong size* never outranks a right one however stocked."""
        hits = cs.resolve("M4x12 countersunk")
        assert hits[0].size.key == "M4"
        universal = [h for h in hits if h.size.stocking == "universal"]
        specialty = [h for h in hits if h.size.stocking == "specialty"]
        if universal and specialty:
            assert universal[0].score > specialty[0].score


class TestInserts:
    def test_the_insert_series_admits_it_has_no_standard(self) -> None:
        series = _series("insert-brass-heatset")
        assert series.designation is None
        assert "NO ISO STANDARD" in series.source

    def test_an_insert_carries_the_diameter_a_pocket_must_accept(self) -> None:
        series = _series("insert-brass-heatset")
        m3 = series.size("M3")
        assert m3 is not None
        assert float(m3.specs["outer_diameter"]) > 3.0
        assert float(m3.specs["height"]) > 0
