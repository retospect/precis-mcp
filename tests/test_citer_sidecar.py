"""``handlers/_citer_sidecar.py`` — the Part 3 capped citer-verdict render.

Covers the citation-chunk-grounding "sidecar render" (docs/design/
citation-chunk-grounding.md Part 3): a chunk with resolved outbound
``cites`` links (from ``workers/inbound_chase.py``) gets a capped,
best-first, expand-on-request sidecar of the surfaceable (yes/partial)
verdicts. Also covers the symmetric inbound render
(``render_cited_by_sidecar``, filtered on ``dst_pos``), buildable now
that ``inbound_chase``'s second locate pass populates ``dst_pos``.
"""

from __future__ import annotations

import pytest

from precis.dispatch import Hub
from precis.handlers._citer_sidecar import render_cited_by_sidecar, render_citer_sidecar
from precis.handlers.paper import PaperHandler
from precis.store import BlockInsert, Store


def _paper(store: Store, *, slug: str, blocks: list[str]) -> int:
    ref = store.insert_ref(kind="paper", slug=slug, title=f"Paper {slug}")
    store.insert_blocks(
        ref.id, [BlockInsert(pos=i, text=t, meta={}) for i, t in enumerate(blocks)]
    )
    return ref.id


class TestRenderCiterSidecar:
    def test_no_links_is_empty_string(self, store: Store) -> None:
        x = _paper(store, slug="x2020", blocks=["chunk zero"])
        ref = store.get_ref(kind="paper", id=x)
        assert ref is not None
        assert render_citer_sidecar(store, ref, 0) == ""

    def test_no_verdict_supports_key_is_dropped(self, store: Store) -> None:
        """A chunk-scoped link with no ``supports`` in meta at all
        (deterministic with_llm=False resolution) doesn't surface —
        only verified yes/partial rows do."""
        x = _paper(store, slug="x2020", blocks=["chunk zero"])
        y = _paper(store, slug="y2020", blocks=["cited paper"])
        store.add_link(src_ref_id=x, dst_ref_id=y, src_pos=0, relation="cites", meta={})
        ref = store.get_ref(kind="paper", id=x)
        assert ref is not None
        assert render_citer_sidecar(store, ref, 0) == ""

    def test_no_verdict_is_dropped(self, store: Store) -> None:
        x = _paper(store, slug="x2020", blocks=["chunk zero"])
        y = _paper(store, slug="y2020", blocks=["cited paper"])
        store.add_link(
            src_ref_id=x,
            dst_ref_id=y,
            src_pos=0,
            relation="cites",
            meta={"supports": "no"},
        )
        ref = store.get_ref(kind="paper", id=x)
        assert ref is not None
        assert render_citer_sidecar(store, ref, 0) == ""

    def test_yes_and_partial_surface_sorted_best_first(self, store: Store) -> None:
        x = _paper(store, slug="x2020", blocks=["chunk zero"])
        partial_target = _paper(store, slug="partial2020", blocks=["p"])
        yes_target = _paper(store, slug="yes2020", blocks=["y"])
        # Insert partial FIRST so a naive insertion-order render would
        # get this backwards — the sort must put "yes" ahead of
        # "partial" regardless of link insertion order.
        store.add_link(
            src_ref_id=x,
            dst_ref_id=partial_target,
            src_pos=0,
            relation="cites",
            meta={"supports": "partial", "caveats": ["only at low T"]},
        )
        store.add_link(
            src_ref_id=x,
            dst_ref_id=yes_target,
            src_pos=0,
            relation="cites",
            meta={"supports": "yes"},
        )
        ref = store.get_ref(kind="paper", id=x)
        assert ref is not None
        section = render_citer_sidecar(store, ref, 0)
        assert "Cites (verified):" in section
        yes_idx = section.index("Paper yes2020")
        partial_idx = section.index("Paper partial2020")
        assert yes_idx < partial_idx
        assert "only at low T" in section  # caveat surfaced in the verdict text

    def test_only_the_requested_chunk_pos_is_shown(self, store: Store) -> None:
        x = _paper(store, slug="x2020", blocks=["chunk zero", "chunk one"])
        y = _paper(store, slug="y2020", blocks=["cited"])
        store.add_link(
            src_ref_id=x,
            dst_ref_id=y,
            src_pos=1,
            relation="cites",
            meta={"supports": "yes"},
        )
        ref = store.get_ref(kind="paper", id=x)
        assert ref is not None
        assert render_citer_sidecar(store, ref, 0) == ""
        assert "Cites (verified):" in render_citer_sidecar(store, ref, 1)

    def test_caps_at_five_and_notes_the_rest(self, store: Store) -> None:
        x = _paper(store, slug="x2020", blocks=["chunk zero"])
        for i in range(7):
            target = _paper(store, slug=f"target{i}2020", blocks=["t"])
            store.add_link(
                src_ref_id=x,
                dst_ref_id=target,
                src_pos=0,
                relation="cites",
                meta={"supports": "yes"},
            )
        ref = store.get_ref(kind="paper", id=x)
        assert ref is not None
        section = render_citer_sidecar(store, ref, 0)
        assert "(2 more)" in section

    def test_identity_falls_back_to_bare_id_when_ref_missing(
        self, store: Store
    ) -> None:
        """``_identity`` degrades to a bare id when the target ref isn't
        in the endpoint map — belt-and-braces (a real hard-delete
        cascades the link away too via FK, so this can't happen through
        ``render_citer_sidecar`` itself; exercised at the unit level)."""
        from precis.handlers._citer_sidecar import _identity

        assert _identity(None, 999) == "<ref 999>"


class TestRenderCitedBySidecar:
    """Inbound direction: rendering chunk D of the *cited* paper Y shows
    who specifically cites that chunk (``dst_pos``-filtered)."""

    def test_no_links_is_empty_string(self, store: Store) -> None:
        y = _paper(store, slug="y2020", blocks=["chunk zero"])
        ref = store.get_ref(kind="paper", id=y)
        assert ref is not None
        assert render_cited_by_sidecar(store, ref, 0) == ""

    def test_src_pos_only_link_does_not_surface_here(self, store: Store) -> None:
        """A link with only ``src_pos`` set (no second locate ran, or it
        found no confident match) doesn't show up in the inbound render
        — that's exactly the "graceful no match" case."""
        x = _paper(store, slug="x2020", blocks=["chunk zero"])
        y = _paper(store, slug="y2020", blocks=["cited paragraph"])
        store.add_link(
            src_ref_id=x,
            dst_ref_id=y,
            src_pos=0,
            relation="cites",
            meta={"supports": "yes"},
        )
        ref = store.get_ref(kind="paper", id=y)
        assert ref is not None
        assert render_cited_by_sidecar(store, ref, 0) == ""

    def test_dst_pos_link_surfaces_citer_identity(self, store: Store) -> None:
        x = _paper(store, slug="x2020", blocks=["chunk zero"])
        y = _paper(store, slug="y2020", blocks=["cited paragraph"])
        store.add_link(
            src_ref_id=x,
            dst_ref_id=y,
            src_pos=0,
            dst_pos=0,
            relation="cites",
            meta={"supports": "yes"},
        )
        ref = store.get_ref(kind="paper", id=y)
        assert ref is not None
        section = render_cited_by_sidecar(store, ref, 0)
        assert "Cited by (verified):" in section
        assert "Paper x2020" in section

    def test_no_verdict_is_dropped(self, store: Store) -> None:
        x = _paper(store, slug="x2020", blocks=["chunk zero"])
        y = _paper(store, slug="y2020", blocks=["cited paragraph"])
        store.add_link(
            src_ref_id=x,
            dst_ref_id=y,
            src_pos=0,
            dst_pos=0,
            relation="cites",
            meta={"supports": "no"},
        )
        ref = store.get_ref(kind="paper", id=y)
        assert ref is not None
        assert render_cited_by_sidecar(store, ref, 0) == ""

    def test_only_the_requested_dst_pos_is_shown(self, store: Store) -> None:
        x = _paper(store, slug="x2020", blocks=["chunk zero"])
        y = _paper(store, slug="y2020", blocks=["chunk zero", "chunk one"])
        store.add_link(
            src_ref_id=x,
            dst_ref_id=y,
            src_pos=0,
            dst_pos=1,
            relation="cites",
            meta={"supports": "yes"},
        )
        ref = store.get_ref(kind="paper", id=y)
        assert ref is not None
        assert render_cited_by_sidecar(store, ref, 0) == ""
        assert "Cited by (verified):" in render_cited_by_sidecar(store, ref, 1)

    def test_yes_and_partial_surface_sorted_best_first(self, store: Store) -> None:
        y = _paper(store, slug="y2020", blocks=["chunk zero"])
        partial_citer = _paper(store, slug="partial2020", blocks=["p"])
        yes_citer = _paper(store, slug="yes2020", blocks=["y"])
        store.add_link(
            src_ref_id=partial_citer,
            dst_ref_id=y,
            src_pos=0,
            dst_pos=0,
            relation="cites",
            meta={"supports": "partial"},
        )
        store.add_link(
            src_ref_id=yes_citer,
            dst_ref_id=y,
            src_pos=0,
            dst_pos=0,
            relation="cites",
            meta={"supports": "yes"},
        )
        ref = store.get_ref(kind="paper", id=y)
        assert ref is not None
        section = render_cited_by_sidecar(store, ref, 0)
        yes_idx = section.index("Paper yes2020")
        partial_idx = section.index("Paper partial2020")
        assert yes_idx < partial_idx


# ---------------------------------------------------------------------------
# PaperHandler.get() wiring — gated behind PRECIS_INBOUND_CHASE_ENABLED
# ---------------------------------------------------------------------------


class TestPaperHandlerSidecarWiring:
    def test_sidecar_hidden_when_flag_off(
        self, hub: Hub, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("PRECIS_INBOUND_CHASE_ENABLED", raising=False)
        store = hub.store
        x = _paper(store, slug="x2020", blocks=["chunk zero"])
        y = _paper(store, slug="y2020", blocks=["cited"])
        store.add_link(
            src_ref_id=x,
            dst_ref_id=y,
            src_pos=0,
            relation="cites",
            meta={"supports": "yes"},
        )
        resp = PaperHandler(hub=hub).get(id="x2020~0")
        assert "Cites (verified):" not in resp.body

    def test_sidecar_shown_when_flag_on(
        self, hub: Hub, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("PRECIS_INBOUND_CHASE_ENABLED", "1")
        store = hub.store
        x = _paper(store, slug="x2021", blocks=["chunk zero"])
        y = _paper(store, slug="y2021", blocks=["cited"])
        store.add_link(
            src_ref_id=x,
            dst_ref_id=y,
            src_pos=0,
            relation="cites",
            meta={"supports": "yes"},
        )
        resp = PaperHandler(hub=hub).get(id="x2021~0")
        assert "Cites (verified):" in resp.body
        assert "Paper y2021" in resp.body

    def test_both_sidecars_render_as_separate_sections(
        self, hub: Hub, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A chunk can both cite something (outbound) and be cited by
        something else (inbound) — two small sections, not one table."""
        monkeypatch.setenv("PRECIS_INBOUND_CHASE_ENABLED", "1")
        store = hub.store
        w = _paper(store, slug="w2021", blocks=["chunk zero"])
        x = _paper(store, slug="x2021", blocks=["chunk zero"])
        y = _paper(store, slug="y2021", blocks=["cited"])
        # x's chunk 0 cites y (outbound from x's perspective).
        store.add_link(
            src_ref_id=x,
            dst_ref_id=y,
            src_pos=0,
            relation="cites",
            meta={"supports": "yes"},
        )
        # w's chunk 0 cites x's chunk 0 (inbound from x's perspective).
        store.add_link(
            src_ref_id=w,
            dst_ref_id=x,
            src_pos=0,
            dst_pos=0,
            relation="cites",
            meta={"supports": "partial"},
        )
        resp = PaperHandler(hub=hub).get(id="x2021~0")
        assert "Cites (verified):" in resp.body
        assert "Paper y2021" in resp.body
        assert "Cited by (verified):" in resp.body
        assert "Paper w2021" in resp.body
