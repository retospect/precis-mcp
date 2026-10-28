"""Closed-axis registry reads ``data/axes/*.yaml`` (ontology-v2 slice 0d,
gr504808). The categorizer namespaces register at import: ref-level axes
join their kinds' allowed sets, marker/orphaned namespaces are
``DECLARED_AXES`` (registered, value-free), and an unknown value on a
registered axis refuses with close matches.
"""

from __future__ import annotations

from typing import Any

import pytest

from precis.errors import BadInput
from precis.store.types import (
    _CLOSED_VOCAB,
    _KIND_ALLOWED_AXES,
    Tag,
)


def test_every_yaml_axis_is_registered() -> None:
    """Every data/axes/*.yaml with id+values lands in the registry —
    the table can never drift behind a new axis file again."""
    from precis.store._axes_vocab import load_categorizer_axes

    axes = load_categorizer_axes()
    assert axes, "no categorizer axes found — data/axes moved?"
    for ns, ax in axes.items():
        assert _CLOSED_VOCAB[ns] >= ax.values


def test_material_mof_parses_and_is_paper_writable() -> None:
    """gr504808: MATERIAL:mof was refused as unknown closed-prefix axis."""
    t = Tag.parse_strict("MATERIAL:mof")
    assert (t.prefix, t.value) == ("MATERIAL", "mof")
    assert str(Tag.parse_strict("MATERIAL:mof", kind="paper")) == "MATERIAL:mof"


def test_unknown_axis_value_refuses_with_close_matches() -> None:
    with pytest.raises(BadInput, match="invalid MATERIAL value.*mof") as ei:
        Tag.parse_strict("MATERIAL:moff")
    assert ei.value.options is not None and "mof" in ei.value.options


def test_unregistered_axis_still_refused() -> None:
    with pytest.raises(BadInput, match="unknown closed-prefix axis"):
        Tag.parse_strict("CONFIDENCE:moderate")


def test_declared_marker_axes_accept_dynamic_values() -> None:
    """Pass bookkeeping namespaces (version markers, leases, orphaned
    review axes) parse any value — the axis name is what is protected."""
    for tag in (
        "TOPICCASCADE:3-15ff5638",
        "MATERIALCASCADE:1",
        "MATERIALCASCADEATTEMPT:1",
        "CHASETRIG:2",
        "BIBMARK:1",
        "TAPROOT_DUE:1",
        "SEV:major",
        "NOTE:review",
    ):
        t = Tag.parse_strict(tag, kind="paper")
        assert t.namespace == "closed", tag


def test_ref_level_axes_join_their_kinds_allowed_sets() -> None:
    """A ref-level yaml axis is writable on every kind its pass targets —
    MATERIAL/DOMAIN/… on paper, MOVE on memory."""
    for axis in (
        "DOMAIN",
        "MATERIAL",
        "PROPERTY",
        "DIM",
        "SCALE",
        "STUDYTYPE",
        "TRANSPORT",
    ):
        assert axis in _KIND_ALLOWED_AXES["paper"], axis
    assert "MOVE" in _KIND_ALLOWED_AXES["memory"]


def test_junk_axis_ref_kinds_whitelists_paper() -> None:
    """gr520915: ``ref_kinds:`` in junk.yaml whitelists the chunk-level
    JUNK namespace as a ref tag on paper — the furniture-only verdict a
    human stamps. A chunk-level axis without ``ref_kinds`` stays
    filter-only."""
    assert "JUNK" in _KIND_ALLOWED_AXES["paper"]
    assert str(Tag.parse_strict("JUNK:junk", kind="paper")) == "JUNK:junk"
    with pytest.raises(BadInput, match="axis not allowed on kind 'paper'"):
        Tag.parse_strict("ROLE:motivation", kind="paper")


def test_chunk_level_axes_stay_ref_read_only_on_paper() -> None:
    """ROLE3/OPEN-QUESTION are chunk tags — registered for filters,
    never ref-tag writable on paper. (JUNK is the one ``ref_kinds``
    exception — covered above.)"""
    assert "JUNK" in _CLOSED_VOCAB
    with pytest.raises(BadInput, match="axis not allowed on kind 'paper'"):
        Tag.parse_strict("ROLE3:own", kind="paper")
    # but the filter form (kind=None) still validates it
    assert str(Tag.parse_strict("ROLE3:own")) == "ROLE3:own"


def test_taproot_static_entry_unioned_not_replaced() -> None:
    assert _CLOSED_VOCAB["TAPROOT"] == frozenset({"claim", "review"})


class TestSearchEndToEnd:
    def test_paper_search_accepts_material_filter(self, store: Any) -> None:
        """gr504808 regression: search(kind='paper', tags=['MATERIAL:mof'])
        ran into 'unknown closed-prefix axis' at normalize_filter."""
        from precis.dispatch import Hub
        from precis.handlers.paper import PaperHandler

        h = PaperHandler(hub=Hub(store=store))
        body = h.search(q="anything", tags=["MATERIAL:mof"]).body
        assert "unknown closed-prefix axis" not in body

    def test_tag_verb_writes_a_categorizer_axis_on_paper(self, store: Any) -> None:
        from precis.dispatch import Hub
        from precis.handlers.paper import PaperHandler
        from tests.workers._helpers import seed_ref

        h = PaperHandler(hub=Hub(store=store))
        ref_id = seed_ref(store, title="a mof paper", kind="paper")
        resp = h.tag(id=ref_id, add=["MATERIAL:mof"])
        assert "error" not in resp.body.lower(), resp.body
        rows = store.ref_tag_rows([ref_id], namespaces=["MATERIAL"])
        assert rows == [(ref_id, "MATERIAL", "mof")]
