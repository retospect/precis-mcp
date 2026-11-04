"""Outlet totals use the same normalised-entropy score as crossing bins."""

import numpy as np
import pytest

from precis_flow import nonuniformity


def test_equal_outlets():
    masks = [np.array([[index == outlet for index in range(3)]]) for outlet in range(3)]
    points = np.array([[0.5, 0.5], [1.5, 0.5], [2.5, 0.5]])
    score, kept = nonuniformity(points, (0, 3), (0, 1), outlet_masks=masks)
    assert score == pytest.approx(0, abs=1e-15)
    np.testing.assert_array_equal(kept, points)


def test_uneven_split_has_hand_computed_entropy():
    masks = [np.array([[True, False]]), np.array([[False, True]])]
    points = np.array([[0.25, 0.5], [0.75, 0.5], [0.75, 0.5], [0.75, 0.5]])
    score, _ = nonuniformity(points, (0, 1), (0, 1), outlet_masks=masks)
    # 1 + (0.25 ln(0.25) + 0.75 ln(0.75)) / ln(2).
    assert score == pytest.approx(0.18872187554086717)


def test_outlet_totals_are_not_normalised_by_area():
    masks = [np.array([[True, False, False]]), np.array([[False, True, True]])]
    points = np.array([[0.25, 0.5], [0.75, 0.5], [1.5, 0.5], [2.5, 0.5]])
    score, _ = nonuniformity(points, (0, 3), (0, 1), outlet_masks=masks)
    assert score == pytest.approx(0, abs=1e-15)


def test_nonsquare_masks_keep_union_in_input_order_and_include_upper_edges():
    masks = [
        np.array([[False, False, True], [False, False, False]]),
        np.array([[False, False, False], [True, False, False]]),
    ]
    points = np.array([[0.5, 2], [1.5, 0.5], [3, 0.5], [-0.1, 1.5], [3.1, 0.5]])
    score, kept = nonuniformity(points, (0, 3), (0, 2), outlet_masks=masks)
    assert score == pytest.approx(0, abs=1e-15)
    np.testing.assert_array_equal(kept, points[[0, 2]])


def test_interior_edges_belong_to_higher_cell_and_single_outlet_is_even():
    mask = np.array([[False, False], [False, True]])
    points = np.array([[1, 1], [2, 2], [0, 0]])
    score, kept = nonuniformity(points, (0, 2), (0, 2), outlet_masks=[mask])
    assert score == pytest.approx(0, abs=1e-15)
    np.testing.assert_array_equal(kept, points[:2])


def test_empty_outlet_list_refuses():
    with pytest.raises(ValueError, match="at least one outlet"):
        nonuniformity(np.empty((0, 2)), (0, 1), (0, 1), outlet_masks=[])


@pytest.mark.parametrize("empty_mask", [False, True])
def test_zero_flow_outlet_refuses(empty_mask):
    masks = [np.array([[True, False]]), np.array([[False, not empty_mask]])]
    with pytest.raises(ValueError, match="outlet mask 1 has zero flow"):
        nonuniformity(np.array([[0.25, 0.5]]), (0, 1), (0, 1), outlet_masks=masks)


def test_no_crossings_refuses_in_outlet_mode():
    with pytest.raises(ValueError, match="outlet mask 0 has zero flow"):
        nonuniformity(
            np.empty((0, 2)), (0, 1), (0, 1), outlet_masks=[np.ones((1, 1), dtype=bool)]
        )


@pytest.mark.parametrize(
    ("masks", "message"),
    [
        ([np.array([True])], "nonempty 2-D"),
        ([np.empty((0, 2), dtype=bool)], "nonempty 2-D"),
        ([np.array([[1]])], "Boolean grids"),
        ([np.ones((1, 1), dtype=bool), np.ones((1, 2), dtype=bool)], "same shape"),
        ([np.array([[True]]), np.array([[True]])], "overlaps"),
    ],
)
def test_invalid_outlet_masks_refuse(masks, message):
    with pytest.raises(ValueError, match=message):
        nonuniformity(np.array([[0.5, 0.5]]), (0, 1), (0, 1), outlet_masks=masks)


@pytest.mark.parametrize("x_range", [(1, 0), (0, 0), (0, np.inf), (np.nan, 1)])
def test_invalid_outlet_ranges_refuse(x_range):
    with pytest.raises(ValueError, match="finite and strictly increasing"):
        nonuniformity(
            np.array([[0.5, 0.5]]), x_range, (0, 1), outlet_masks=[np.array([[True]])]
        )


@pytest.mark.parametrize("coordinate", [np.nan, np.inf, -np.inf])
def test_nonfinite_outlet_points_refuse(coordinate):
    with pytest.raises(ValueError, match="points must be finite"):
        nonuniformity(
            np.array([[coordinate, 0.5]]),
            (0, 1),
            (0, 1),
            outlet_masks=[np.array([[True]])],
        )


def test_bins_and_outlet_masks_are_mutually_exclusive():
    with pytest.raises(ValueError, match="not both"):
        nonuniformity(
            np.array([[0.5, 0.5]]),
            (0, 1),
            (0, 1),
            (1, 1),
            outlet_masks=[np.array([[True]])],
        )


def test_missing_bins_and_masks_refuses():
    with pytest.raises(ValueError, match="supply bins or outlet_masks"):
        nonuniformity(np.array([[0.5, 0.5]]), (0, 1), (0, 1))


def test_legacy_histogram_behavior():
    points = np.array([[0.25, 0.5], [0.75, 0.5], [2, 2]])
    score, kept = nonuniformity(points, (0, 1), (0, 1), (2, 1))
    assert score == pytest.approx(0, abs=1e-15)
    np.testing.assert_array_equal(kept, points[:2])
    for points in (np.empty((0, 2)), np.array([[2, 2]])):
        score, kept = nonuniformity(points, (0, 1), (0, 1), (2, 1))
        assert score == pytest.approx(1)
        assert kept.shape == (0, 2)
