"""The same published standards are transcribed in three places. These
tests are the drift guard.

The tree has grown three independent encodings of ISO fastener dimensions,
each for a good reason and none of them wrong:

- ``precis/cad/catalog.py`` — envelopes for the design language's ``part``
  line. Lives in ``precis.cad``, which imports nothing from the DB and
  must stay that way, so it cannot read a data file that a store consumer
  owns.
- ``precis/data/component_series.json`` — the series/size tables the
  `component` mint turns into spec rows.
- ``precis/data/fit_classes.json`` — ISO 273 clearance holes.

Three transcriptions of one standard is a drift generator: the numbers
agree **today** (verified 2026-09-05, when the third arrived), and the
cheapest way to keep that true is to assert it. Consolidating them is a
real option and is recorded in ``docs/backlog/se-off-the-shelf-
fabrication.md``; until someone does, a divergence should redden a gate
rather than surface as a bolt that fits in one view and not another.

These import the private family tables from ``cad.catalog`` on purpose:
the public :func:`resolve_part` returns *rendered cad source*, and
recovering a head height by parsing that string would make the guard
weaker than the thing it guards.
"""

from __future__ import annotations

import pytest

from precis import component_series, fit_classes
from precis.cad import catalog as cad_catalog


def _series_specs(series_id: str) -> dict[str, dict]:
    """``{'M6': {...specs...}}`` for one series, keyed by thread size."""
    series = component_series.find_series(series_id)
    assert series is not None, f"{series_id} missing from component_series.json"
    return {str(s.specs["thread_size"]): dict(s.specs) for s in series.sizes}


class TestBoltHeads:
    def test_iso_4017_head_dimensions_agree(self) -> None:
        rows = _series_specs("iso-4017")
        checked = 0
        for m_size, (head_h, across_flats) in cad_catalog._BOLT_HEADS.items():
            row = rows.get(f"M{m_size}")
            if row is None:
                continue
            assert row["head_height"] == pytest.approx(head_h), f"M{m_size} head height"
            assert row["across_flats"] == pytest.approx(across_flats), (
                f"M{m_size} across flats"
            )
            checked += 1
        assert checked >= 5


class TestNuts:
    def test_iso_4032_heights_and_across_flats_agree(self) -> None:
        rows = _series_specs("iso-4032")
        checked = 0
        for m_size, height in cad_catalog._NUT_HEIGHTS.items():
            row = rows.get(f"M{m_size}")
            if row is None:
                continue
            assert row["height"] == pytest.approx(height), f"M{m_size} nut height"
            # cad.catalog shares the bolt's across-flats with the nut; the
            # series file states it separately, so this is a real check.
            head = cad_catalog._BOLT_HEADS.get(m_size)
            if head is not None:
                assert row["across_flats"] == pytest.approx(head[1]), (
                    f"M{m_size} nut across flats"
                )
            checked += 1
        assert checked >= 5


class TestWashers:
    def test_iso_7089_dimensions_agree(self) -> None:
        rows = _series_specs("iso-7089")
        checked = 0
        for m_size, (bore, outer, thickness) in cad_catalog._WASHERS.items():
            row = rows.get(f"M{m_size}")
            if row is None:
                continue
            assert row["inner_diameter"] == pytest.approx(bore), f"M{m_size} bore"
            assert row["outer_diameter"] == pytest.approx(outer), f"M{m_size} OD"
            assert row["thickness"] == pytest.approx(thickness), f"M{m_size} thickness"
            checked += 1
        assert checked >= 5


class TestClearanceHoles:
    """ISO 273's *fine* column and the ISO 7089 washer bore are the same
    number at every shared size — the two standards were written to
    agree, so a typo in either shows up here."""

    def test_the_fine_column_is_the_washer_bore(self) -> None:
        rows = _series_specs("iso-7089")
        checked = 0
        for thread_size, specs in rows.items():
            fit = fit_classes.clearance_hole(thread_size, "fine")
            if fit is None:
                continue
            assert fit.hole_mm == pytest.approx(float(specs["inner_diameter"])), (
                f"ISO 273 fine vs ISO 7089 bore at {thread_size}"
            )
            checked += 1
        assert checked >= 8

    def test_the_cad_washer_bore_is_the_fine_column_too(self) -> None:
        """Closes the triangle: cad.catalog ↔ fit_classes directly, so a
        change to either that keeps the JSON pair consistent still fails
        if it broke the third corner."""
        for m_size, (bore, _od, _t) in cad_catalog._WASHERS.items():
            fit = fit_classes.clearance_hole(f"M{m_size}", "fine")
            if fit is None:
                continue
            assert fit.hole_mm == pytest.approx(bore), f"M{m_size}"

    def test_every_clearance_hole_clears_its_own_thread(self) -> None:
        """The invariant that makes the table usable at all: a hole a bolt
        cannot pass through is worse than no row."""
        for size in fit_classes.sizes():
            for cls in fit_classes.classes():
                fit = fit_classes.clearance_hole(size, cls)
                assert fit is not None and fit.hole_mm > fit.nominal_mm, f"{size}/{cls}"

    def test_the_classes_are_ordered_fine_medium_coarse(self) -> None:
        for size in fit_classes.sizes():
            fine = fit_classes.clearance_hole(size, "fine")
            medium = fit_classes.clearance_hole(size, "medium")
            coarse = fit_classes.clearance_hole(size, "coarse")
            assert fine is not None and medium is not None and coarse is not None
            assert fine.hole_mm <= medium.hole_mm <= coarse.hole_mm, size


class TestThreadPitch:
    def test_the_series_files_agree_on_coarse_pitch(self) -> None:
        """Every series that states a pitch states the same one — the ISO
        coarse pitch is a property of the thread, not of the part."""
        by_size: dict[str, set[float]] = {}
        for series_id in ("iso-4762", "iso-4017", "iso-4032"):
            for thread_size, specs in _series_specs(series_id).items():
                pitch = specs.get("thread_pitch")
                if pitch is not None:
                    by_size.setdefault(thread_size, set()).add(float(pitch))
        assert by_size, "no series states a thread pitch"
        for thread_size, pitches in sorted(by_size.items()):
            assert len(pitches) == 1, f"{thread_size} has pitches {sorted(pitches)}"
