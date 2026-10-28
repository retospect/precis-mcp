"""``options(handle, wish)`` (SPEC 25.3): the fit families driven by a wish
-- a target with a band, or a don't-care -- ranked by distance first, with
the in-band values that do not build reported as rejections."""

from __future__ import annotations

import json

import pytest

from hexfold.cli import main
from hexfold.options import Wish, options

_LEN = """hexfold 0.1
origin t
t: tube(10,10,len=fit) - hexagon@(7,0,A):0
"""

_ARM = """hexfold 0.2
a: tube(5,5, len=3)
b: tube(5,5, len=3)
a.out --fuse k=0--> b.in
"""

_ZIG = """hexfold 0.2
a: tube(5,0, len=3)
b: tube(5,0, len=3)
a.out --fuse k=0--> b.in
"""


# ---------- len ----------


def test_len_wish_ranks_by_distance_then_lattice_index() -> None:
    r = options(_LEN, "t.len", Wish(target=9, band=2))
    assert r.kind == "len" and r.unit == "periods"
    assert [o.value for o in r.options] == [9, 8, 10, 7, 11]
    assert [o.distance for o in r.options] == [0, 1, 1, 2, 2]
    assert r.rejected == ()
    assert r.applied == 7  # what a plain build picks: the smallest clean len


def test_len_wish_in_angstrom_converts_through_the_tube_period() -> None:
    r = options(_LEN, "t.len", Wish(target_A=22.0))
    # (10,10) armchair: period a = 2.46 A -> 22 A is 8.94 periods -> 9
    assert r.period_A == pytest.approx(2.46, abs=0.01)
    assert [o.value for o in r.options] == [9]
    assert r.options[0].value_A == pytest.approx(9 * 2.46, abs=0.05)


def test_len_wish_below_feasibility_returns_only_rejections() -> None:
    r = options(_LEN, "t.len", Wish(target=3, band=3))
    assert r.options == ()
    assert [o.value for o in r.rejected] == [1, 2, 3, 4, 5, 6]
    codes = {c for o in r.rejected for c in o.errors}
    assert "cut.overlap" in codes  # the hole clips the rim on a short tube
    assert all(not o.clean and o.errors for o in r.rejected)


def test_len_dont_care_is_the_capped_fit_family() -> None:
    r = options(_LEN, "t.len")
    assert r.wish.dont_care
    assert [o.value for o in r.options] == [7, 8, 9, 10, 11, 12, 13, 14]
    assert all(o.distance == 0 for o in r.options)
    assert r.applied == 7


def test_len_on_an_authored_length_keeps_applied_as_authored() -> None:
    r = options(_ARM, "a.len", Wish(target=5, band=1))
    assert r.applied == 3
    assert [o.value for o in r.options] == [5, 4, 6]


# ---------- k ----------


def test_k_dont_care_ranks_all_hexagon_phases_first() -> None:
    r = options(_ARM, "a.out.k")
    assert r.kind == "k" and r.unit == "steps" and r.period_steps == 10
    assert r.applied == 0
    assert [o.value for o in r.options] == [0, 2, 4, 6, 8, 1, 3, 5, 7, 9]
    # even phases: seam of ten hexagons; odd: five squares + five octagons
    assert {o.cost[0] for o in r.options if o.value % 2 == 0} == {6.0}
    assert {o.cost[0] for o in r.options if o.value % 2 == 1} == {8.0}
    assert r.rejected == ()


def test_k_wish_distance_is_modular() -> None:
    r = options(_ARM, "a.out.k", Wish(target=9, band=1))
    assert [(o.value, o.distance) for o in r.options] == [(9, 0), (0, 1), (8, 1)]


def test_k_zigzag_all_phases_equivalent() -> None:
    r = options(_ZIG, "a.out.k")
    assert r.period_steps == 5
    assert [o.value for o in r.options] == [0, 1, 2, 3, 4]
    assert {o.cost[:2] for o in r.options} == {(6.0, 0.0)}


# ---------- handles ----------


@pytest.mark.parametrize(
    "handle",
    ["nope", "zz.len", "a.k", "a.out.len", "a.out.width"],
)
def test_bad_handles_raise_with_the_handle_named(handle: str) -> None:
    with pytest.raises(ValueError, match="options"):
        options(_ARM, handle)


def test_k_handle_rejects_angstrom_wishes() -> None:
    with pytest.raises(ValueError, match="steps"):
        options(_ARM, "a.out.k", Wish(target_A=3.0))


# ---------- cli ----------


def test_cli_options_json(tmp_path, capsys) -> None:
    f = tmp_path / "t.hx"
    f.write_text(_LEN, encoding="utf-8")
    rc = main(["options", str(f), "t.len", "--target", "9", "--band", "2", "--json"])
    assert rc == 0
    doc = json.loads(capsys.readouterr().out)
    assert doc["handle"] == "t.len"
    assert [o["value"] for o in doc["options"]] == [9, 8, 10, 7, 11]
    assert doc["applied"] == 7


def test_cli_options_text_lists_options_and_rejections(tmp_path, capsys) -> None:
    f = tmp_path / "t.hx"
    f.write_text(_LEN, encoding="utf-8")
    rc = main(["options", str(f), "t.len", "--target", "6", "--band", "1"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "7" in out and "cut.overlap" in out
