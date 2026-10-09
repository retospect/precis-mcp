"""`view='catalogue'` — each join side's consulted catalogue row and its
resolution label, read back from the join record without recomputation
(docs/backlog/se-join-observability.md slice 2).

The label is the one `hexfold.join.compose` carried on
`seam["catalogue"]`; the view adds only which withheld (measured) row the
gate kept from that side, so "the pinned constant governed" is shown next
to what it displaced.
"""

from __future__ import annotations

import dataclasses

import pytest

from hexfold import catalogue as hx
from precis.dispatch import Hub
from precis.errors import BadInput, NotFound
from precis.store import Store
from precis_se.atomic.catalogue import DbCatalogueStore
from precis_se.handler import SeHandler

_SPEC = "hexfold 0.2\na: tube(8,0, len=3)\n"


def _measured_z8(seam_radius: int) -> hx.EdgeMotif:
    pinned = next(
        r
        for r in hx.seed_rows()
        if r.key.rim_type == "z" and r.key.rung == "stick" and r.key.N is None
    )
    return dataclasses.replace(
        pinned,
        key=dataclasses.replace(pinned.key, N=8),
        seam_radius=seam_radius,
        source="measured",
        coverage="full",
    )


@pytest.fixture
def joined(store: Store) -> SeHandler:
    handler = SeHandler(hub=Hub(store=store))
    handler.put(
        id="cat-view",
        args={
            "ops": [
                {
                    "op": "generate",
                    "generator": "hexfold",
                    "params": {"spec": _SPEC},
                    "name": "a",
                },
                {
                    "op": "generate",
                    "generator": "hexfold",
                    "params": {"spec": _SPEC},
                    "name": "b",
                },
            ]
        },
    )
    handler.edit(
        id="cat-view",
        ops=[
            {
                "op": "join",
                "name": "joined",
                "a": "a.out",
                "b": "b.in",
                "seam_radius": {"b": 2},
            }
        ],
    )
    return handler


def test_pinned_row_governs_and_is_shown_per_side(joined: SeHandler) -> None:
    body = joined.get(id="cat-view", view="catalogue").body
    assert "## catalogue rows (6 preferred, 0 withheld)" in body
    assert "## block 'joined' (join, rung=stick)" in body
    assert "- a: a.out  rim z8" in body
    assert "- b: b.in  rim z8" in body
    a_part, b_part = body.split("- b: b.in")
    assert "consulted: pinned z — key" in a_part and "source=pinned-" in a_part
    assert "in force: radius 8 (pinned z); leak 0.0001 Å / 0.025° (pinned z)" in a_part
    assert "in force: radius 2 (explicit); leak 0.0001 Å / 0.025° (pinned z)" in b_part
    assert "consulted: pinned z" in b_part
    assert body.count("withheld: none for this environment") == 2
    assert "block 'a'" not in body


def test_withheld_measured_row_is_listed_and_named_per_side(
    joined: SeHandler, store: Store
) -> None:
    row = _measured_z8(3)
    DbCatalogueStore(store).put(row)
    body = joined.get(id="cat-view", view="catalogue", args={"block": "joined"}).body
    assert "(6 preferred, 1 withheld)" in body
    assert (
        body.count(
            f"withheld: exact z8 — key {row.key.hash()[:12]} radius 3 leak "
            "0.0001 Å / 0.025° (measured-row gate)"
        )
        == 2
    )
    # the recorded resolution is untouched by the later row
    assert body.count("consulted: pinned z") == 2


def test_non_join_block_and_bad_selectors(joined: SeHandler) -> None:
    body = joined.get(id="cat-view", view="catalogue", args={"block": "a"}).body
    assert "## block 'a'" in body
    assert "Not a join" in body and "generator=hexfold" in body
    with pytest.raises(NotFound):
        joined.get(id="cat-view", view="catalogue", args={"block": "nope"})
    with pytest.raises(BadInput):
        joined.get(id="cat-view", view="catalogue", args={"block": " "})
    with pytest.raises(BadInput):
        joined.get(id="cat-view", view="catalogue", args={"state": {"a": "x"}})


def test_design_without_joins_says_so(store: Store) -> None:
    handler = SeHandler(hub=Hub(store=store))
    handler.put(
        id="no-joins",
        args={
            "ops": [
                {
                    "op": "generate",
                    "generator": "hexfold",
                    "params": {"spec": _SPEC},
                    "name": "a",
                },
            ]
        },
    )
    body = handler.get(id="no-joins", view="catalogue").body
    assert "No join blocks in this design." in body
