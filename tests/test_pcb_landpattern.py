"""Synthesized land patterns must give distinct pins distinct coordinates.

That is the whole point: every geometric consumer in the pcb engine reads
the INSTANCE centroid for every pin, so two nets leaving one part start
coincident and generate an exact-0.000mm clearance violation that no
router can fix. See :mod:`precis.pcb.landpattern`'s module docstring.
"""

from __future__ import annotations

import math

import pytest

from precis.pcb.landpattern import (
    HEADER_PITCH_MM,
    offsets_for,
    rotate_offset,
    sizes_for,
)


@pytest.mark.parametrize("n", [2, 3, 4, 8, 14, 16, 24, 48])
def test_every_pin_gets_a_distinct_coordinate(n: int) -> None:
    """The defect this module exists to fix, asserted directly.

    Coincident pads are not a quality problem here — they are the
    structural cause of unroutable copper, because two tracks that start
    at the same point violate clearance before routing begins.
    """
    offsets, synthesized = offsets_for(n)
    assert synthesized is True
    assert len(offsets) == n
    assert len(set(offsets)) == n, f"{n}-pin pattern has coincident pads: {offsets}"


@pytest.mark.parametrize("n", [2, 3, 8, 14, 24, 48])
def test_adjacent_pads_are_far_enough_apart_to_not_manufacture_drc_errors(
    n: int,
) -> None:
    """Distinct is not enough — they must clear the fab minimum.

    JLC's minimum copper clearance is 0.090mm. A pattern that separated
    pads by less would trade one wall of false clearance errors for
    another, which would look like progress in the total and be none.
    """
    offsets, _ = offsets_for(n)
    closest = min(
        math.dist(a, b) for i, a in enumerate(offsets) for b in offsets[i + 1 :]
    )
    assert closest >= 0.3, f"{n}-pin pattern's closest pad pair is {closest:.3f}mm"


def test_two_pin_passive_straddles_the_origin() -> None:
    """The centroid must stay the rotation pivot.

    ``padplace`` rotates about the instance centroid; offsets that are not
    centred would make a rotation translate the part as a side effect.
    """
    offsets, _ = offsets_for(2)
    cx = sum(x for x, _ in offsets) / len(offsets)
    cy = sum(y for _, y in offsets) / len(offsets)
    assert cx == pytest.approx(0.0, abs=1e-9)
    assert cy == pytest.approx(0.0, abs=1e-9)


def test_header_label_hint_selects_the_standard_pitch() -> None:
    """2.54mm is a real standard, not a guess — use it when recognisable."""
    offsets, _ = offsets_for(4, label="J1 2.54mm pin header")
    xs = sorted(x for x, _ in offsets)
    assert xs[1] - xs[0] == pytest.approx(HEADER_PITCH_MM)


# ── pad SIZE synthesis ──────────────────────────────────────────────────
@pytest.mark.parametrize("n", [2, 3, 8, 14, 24, 48])
def test_sizes_for_returns_one_positive_nonvanishing_size_per_pin(n: int) -> None:
    sizes, synthesized = sizes_for(n)
    assert synthesized is True
    assert len(sizes) == n
    for w, h, shape in sizes:
        assert w > 0.0
        assert h > 0.0
        assert shape in ("circle", "rect")


def test_sizes_for_is_not_one_flat_disc_regardless_of_package() -> None:
    """The defect this module exists to close: a 2-pin passive, a 4-pin
    row part and a 48-pin QFN must NOT all reserve the same pad, the way
    every pad in the engine used to before ``maze.PAD_RADIUS_MM`` stopped
    being the only figure anyone read."""
    passive, _ = sizes_for(2)
    row, _ = sizes_for(4)
    quad, _ = sizes_for(48)
    assert passive[0][:2] != row[0][:2]
    assert row[0][:2] != quad[0][:2]
    assert passive[0][:2] != quad[0][:2]


def test_sizes_for_single_and_header_are_round_everything_else_is_not() -> None:
    single, _ = sizes_for(1)
    header, _ = sizes_for(4, label="J1 2.54mm pin header")
    passive, _ = sizes_for(2)
    quad, _ = sizes_for(48)
    for sizes in (single, header):
        for w, h, shape in sizes:
            assert shape == "circle"
            assert w == h
    for sizes in (passive, quad):
        for w, h, shape in sizes:
            assert shape == "rect"


@pytest.mark.parametrize("n", [2, 3, 4, 8, 14, 16, 24, 48])
def test_synthesized_pads_of_one_package_do_not_collide_with_their_own_offsets(
    n: int,
) -> None:
    """Distinct offsets are not enough (the test above this one already
    covers that) — a pad big enough to reach its own neighbour's pad
    would trade one wall of false clearance errors for another, which is
    exactly the failure :mod:`precis.pcb.maze`'s own module docstring
    describes for a claim that is too generous. The enclosing-circle
    radius the router actually claims (``0.5 * hypot(w, h)``, see
    :func:`precis.pcb.realize._realize_maze`) must leave real clearance
    between any two of this package's own pads."""
    offsets, _ = offsets_for(n)
    sizes, _ = sizes_for(n)
    radii = [math.hypot(w, h) / 2.0 for w, h, _shape in sizes]
    for i, a in enumerate(offsets):
        for j, b in enumerate(offsets):
            if i >= j:
                continue
            gap = math.dist(a, b) - radii[i] - radii[j]
            assert gap > 0.0, f"{n}-pin pattern pads {i}/{j} overlap by {-gap:.3f}mm"


def test_rotation_is_clockwise_from_north() -> None:
    """The board frame's convention, which is NOT the mathematical one.

    A pad at +X ("east") rotated 90 degrees clockwise lands at -Y
    ("south"). Getting this backwards yields a mirrored board that renders
    plausibly, which is why it is pinned rather than assumed.
    """
    x, y = rotate_offset(1.0, 0.0, 90.0)
    assert x == pytest.approx(0.0, abs=1e-9)
    assert y == pytest.approx(-1.0, abs=1e-9)


def test_mirror_happens_before_rotate() -> None:
    """``padplace`` fixed this order and pinned it with a test; match it.

    Mirror-then-rotate and rotate-then-mirror differ for any angle that is
    not a multiple of 180 degrees. Both look reasonable in isolation.
    """
    got = rotate_offset(1.0, 0.0, 90.0, mirrored=True)
    # mirror: (1,0) -> (-1,0); then rotate 90 CW: (-1,0) -> (0,1)
    assert got[0] == pytest.approx(0.0, abs=1e-9)
    assert got[1] == pytest.approx(1.0, abs=1e-9)


def test_rotation_preserves_distance_from_the_pivot() -> None:
    offsets, _ = offsets_for(14)
    for dx, dy in offsets:
        for angle in (0.0, 37.0, 90.0, 180.0, 271.5):
            rx, ry = rotate_offset(dx, dy, angle)
            assert math.hypot(rx, ry) == pytest.approx(math.hypot(dx, dy), abs=1e-9)
