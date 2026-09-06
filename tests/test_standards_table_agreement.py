"""Two standards data files, and the invariants that keep them honest.

**History matters for reading this file.** It was written on 2026-09-05
to guard *three* transcriptions of the same ISO fastener dimensions —
``precis/cad/catalog.py``'s private dicts, ``component_series.json`` and
``fit_classes.json``. On 2026-09-06 the duplication was removed instead:
``cad.catalog`` now reads the series file (its no-DB rule was never a
no-*data-file* rule), so the cad↔series assertions became tautologies and
are gone. Asserting that a value equals itself is worse than no test — it
reports green for work it is not doing.

What is left is what is still genuinely two things:

- **ISO 273 (clearance holes) vs ISO 7089 (washers)** — separate
  standards, separately transcribed, written to agree where they
  overlap. A typo in either still shows up here.
- **The series file's internal consistency** — three size tables in one
  file that must agree about the coarse pitch of a given thread.
- **Completeness for `cad.catalog`'s consumers** — the series file is now
  load-bearing for the design language, so a size it advertises must
  carry every dimension the generators read. This replaces the deleted
  agreement tests: same failure caught, one layer earlier, without the
  tautology.
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


class TestClearanceHolesVsWashers:
    """ISO 273's *fine* column and the ISO 7089 washer bore are the same
    number at every shared size — the standards were written to agree, so
    this catches a transcription slip in either."""

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


class TestSeriesInternalConsistency:
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

    def test_a_nut_and_a_bolt_of_one_size_share_across_flats(self) -> None:
        """They are turned by the same spanner. Stated independently in
        ISO 4017 and ISO 4032, so this is a real check, not a tautology —
        and `cad.catalog._nut` reads the *nut's* row, which is only
        correct because of this."""
        bolts = _series_specs("iso-4017")
        nuts = _series_specs("iso-4032")
        shared = sorted(set(bolts) & set(nuts))
        assert len(shared) >= 7
        for thread_size in shared:
            assert bolts[thread_size]["across_flats"] == pytest.approx(
                nuts[thread_size]["across_flats"]
            ), thread_size


class TestCadCatalogCompleteness:
    """`cad.catalog` reads the series file for its ISO fastener families
    (consolidated 2026-09-06), so the file is load-bearing for the design
    language. A size it advertises must fully resolve."""

    @pytest.mark.parametrize("family", ["bolt", "nut", "washer"])
    def test_every_advertised_size_resolves_to_a_part(self, family: str) -> None:
        sizes = cad_catalog._fastener_sizes(family)
        assert sizes, f"{family} has no sizes"
        for m_size in sizes:
            code = (
                f"{family}:m{m_size}x20" if family == "bolt" else f"{family}:m{m_size}"
            )
            info = cad_catalog.resolve_part(code)
            assert info.source.strip(), code
            # Every generated envelope must carry real dimensions — a
            # missing spec would otherwise render as a zero-radius solid.
            assert "r0h" not in info.source and "h0\n" not in info.source, code

    def test_the_families_cover_the_sizes_a_designer_expects(self) -> None:
        """M3 through M12 is the working range; it regressed to M5 once
        when the series file was the only source and lacked the small hex
        heads."""
        for family in ("bolt", "nut", "washer"):
            covered = set(cad_catalog._fastener_sizes(family))
            assert {3, 4, 5, 6, 8, 10, 12} <= covered, f"{family}: {sorted(covered)}"

    def test_an_unknown_size_still_refuses_by_name(self) -> None:
        with pytest.raises(ValueError, match="unknown nut size"):
            cad_catalog.resolve_part("nut:m7")
        with pytest.raises(ValueError, match="unknown bolt code"):
            cad_catalog.resolve_part("bolt:m7x20")
