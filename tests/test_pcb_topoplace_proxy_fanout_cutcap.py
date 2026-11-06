"""Slice-5 placer proxy terms (td569573): fanoutability (a per-pad legal
via-site-within-reach test) and cut capacity (a per-channel demand-vs-
capacity test) — exact math on tiny hand-built designs, and the
``ProxyState`` incremental delta's parity with the full recompute through
a commit, including the cross-part case where the term on a pad/channel
NOT owned by the moved part still changes."""

from __future__ import annotations

import dataclasses

import numpy as np
import pytest

from precis.pcb.topoplace import ObjectiveShares, ProxyState, TopoDesign
from precis.pcb.topoplace.proxy import cutcap_total, fanout_total
from tests.test_pcb_topoplace_route_proxies import _design

_SQUARE = np.array(
    [[-5.0, -3.0], [5.0, -3.0], [5.0, 3.0], [-5.0, 3.0], [-5.0, -3.0]],
    dtype=np.float64,
)
_NO_BODY = np.zeros((0, 2), dtype=np.float64)


def _fanout_design() -> TopoDesign:
    """A's escaping pin (net0 spans sides 0/1, so both ends need a via)
    sits far from every other body at the seed pose; B carries a 10x6
    courtyard it can park on top of A's pin."""
    return _design(
        ["A", "B", "C"],
        [(0, 0.0, 0.0, 0), (2, 0.0, 0.0, 0)],
        [0, 1],
        [
            (5.0, 5.0, 0.0, 0.0),
            (35.0, 5.0, 0.0, 0.0),
            (35.0, 35.0, 0.0, 0.0),
        ],
        part_rings=True,
    )


def test_fanout_zero_when_every_needing_pad_has_room() -> None:
    d = _fanout_design()
    assert fanout_total(d, d.pose0) == 0.0


def test_fanout_walls_a_pad_when_a_body_parks_on_it() -> None:
    """B's courtyard, moved onto A's pin, blocks every one of the 8
    candidate via-site directions — the pin that needed an escape via
    now has none."""
    d = _fanout_design()
    pose = d.pose0.copy()
    pose[1, 0], pose[1, 1] = 5.0, 5.5
    assert fanout_total(d, pose) == 1.0


def test_fanout_proxy_state_tracks_a_different_parts_pad_through_commit() -> None:
    """A never moves — B does. The walled pad belongs to A, not the part
    ``try_move`` is staging, exactly the cross-part site test the
    ``_fanout_has_site`` obstacle scan (every OTHER body) requires."""
    d = _fanout_design()
    w = ObjectiveShares(crossings=0.5, ratsnest=0.2, overlap=0.2, fanout=0.1)
    proxy = ProxyState(d, d.pose0, w)
    assert proxy.fanout == 0.0

    row = proxy.pose[1].copy()
    row[0], row[1] = 5.0, 5.5
    before = proxy.fanout
    proxy.try_move(1, row)
    assert proxy.fanout > before
    proxy.commit()
    assert proxy.fanout == 1.0
    assert proxy.fanout == pytest.approx(fanout_total(d, proxy.pose))


def test_fanout_revert_drops_the_staged_delta() -> None:
    d = _fanout_design()
    w = ObjectiveShares(crossings=0.5, ratsnest=0.2, overlap=0.2, fanout=0.1)
    proxy = ProxyState(d, d.pose0, w)
    row = proxy.pose[1].copy()
    row[0], row[1] = 5.0, 5.5
    proxy.try_move(1, row)
    assert proxy.fanout == 1.0
    proxy.revert()
    assert proxy.fanout == 0.0
    assert proxy.pose[1, 0] == pytest.approx(35.0)


def _cutcap_design() -> TopoDesign:
    """A/B's courtyards leave a 0.1 mm gap (capacity 1, ``_channel_cap``'s
    floor-plus-one over the finest trace/clearance rule); C-D's net
    always crosses the gate, E-F's does not until E moves onto C's spot."""
    d = _design(
        ["A", "B", "C", "D", "E", "F"],
        [(2, 0.0, 0.0, 0), (3, 0.0, 0.0, 0), (4, 0.0, 0.0, 1), (5, 0.0, 0.0, 1)],
        [0, 0, 0, 0],
        [
            (10.0, 10.0, 0.0, 0.0),  # A
            (20.1, 10.0, 0.0, 0.0),  # B — 0.1 mm gap from A
            (15.05, 3.0, 0.0, 0.0),  # C
            (15.05, 23.0, 0.0, 0.0),  # D
            (0.0, 0.0, 0.0, 0.0),  # E — far from the gate at the seed pose
            (15.05, 23.0, 0.0, 0.0),  # F
        ],
    )
    return dataclasses.replace(
        d, part_outline=(_SQUARE, _SQUARE, _NO_BODY, _NO_BODY, _NO_BODY, _NO_BODY)
    )


def test_cutcap_zero_under_capacity() -> None:
    d = _cutcap_design()
    assert cutcap_total(d, d.pose0) == 0.0


def test_cutcap_overflow_when_a_second_foreign_net_crosses_the_gate() -> None:
    d = _cutcap_design()
    pose = d.pose0.copy()
    pose[4, 0], pose[4, 1] = 15.05, 3.0
    assert cutcap_total(d, pose) == 1.0


def test_cutcap_proxy_state_tracks_an_unrelated_parts_channel_through_commit() -> None:
    """The channel is between A and B; the part that moves (E) owns
    neither body — the channel's own geometry never changes, only the
    demand crossing it, exactly the 'every OTHER channel' delta path."""
    d = _cutcap_design()
    w = ObjectiveShares(crossings=0.5, ratsnest=0.2, overlap=0.2, cutcap=0.1)
    proxy = ProxyState(d, d.pose0, w)
    assert proxy.cutcap == 0.0

    row = proxy.pose[4].copy()
    row[0], row[1] = 15.05, 3.0
    before = proxy.cutcap
    proxy.try_move(4, row)
    assert proxy.cutcap > before
    proxy.commit()
    assert proxy.cutcap == 1.0
    assert proxy.cutcap == pytest.approx(cutcap_total(d, proxy.pose))


def test_cutcap_revert_drops_the_staged_delta() -> None:
    d = _cutcap_design()
    w = ObjectiveShares(crossings=0.5, ratsnest=0.2, overlap=0.2, cutcap=0.1)
    proxy = ProxyState(d, d.pose0, w)
    row = proxy.pose[4].copy()
    row[0], row[1] = 15.05, 3.0
    proxy.try_move(4, row)
    assert proxy.cutcap == 1.0
    proxy.revert()
    assert proxy.cutcap == 0.0


def test_fanout_cutcap_shares_normalise_and_the_compact_preset_carries_both() -> None:
    """``__post_init__`` folds the two new terms into the same total
    every earlier share normalised against (design step 1)."""
    from precis.pcb.topoplace.design import PRESETS

    w = ObjectiveShares(crossings=0.5, ratsnest=0.0, fanout=0.25, cutcap=0.25)
    assert w.crossings + w.fanout + w.cutcap == pytest.approx(1.0)
    compact = PRESETS["compact"]
    assert compact.fanout > 0.0
    assert compact.cutcap > 0.0
