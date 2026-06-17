"""Cross-linking on read-only kinds (paper + Perplexity caches).

Phase-8 follow-up: ``PaperHandler`` and ``_PerplexityBase`` gained
``put`` surfaces that accept ``link/unlink/tags/untags/rel`` while
keeping their bodies immutable. The user's motivating case was
"paper-A cites paper-B", but the same surface lets a research
report link back to the paper that prompted it, and CACHE: tags
land where they belong (on the cache row, not on a memory hop).

These tests pin:

* The paper put surface accepts link/tag ops, rejects body-mutation
  kwargs, and resolves slugs through the same parser ``get`` uses.
* The Perplexity put surface routes ``mode='import'`` to the
  cache-import path and link/tag kwargs (mode unset) to the new
  ops path. Mixing the two raises BadInput up front.
* Per-kind axis enforcement still fires — ``STATUS:`` on a paper
  is rejected, ``CACHE:`` and ``SRC:`` are accepted on paper,
  ``CACHE:`` is accepted on the cache kinds.
* The shared ``_link_tag_ops`` helpers reject obviously-wrong
  combinations (link= and unlink= mutually exclusive, bare rel=).

Test seeding pattern: papers are inserted directly via the store
(no need to spin up the bundle ingest for these unit tests); the
research kind uses the existing ``import`` path to land a slug
that link/tag ops can then operate on.
"""

from __future__ import annotations

import pytest

from precis.dispatch import Hub
from precis.errors import BadInput, NotFound
from precis.handlers.paper import PaperHandler
from precis.store import Store

# ── PaperHandler.put — cross-linking surface ───────────────────────


@pytest.fixture
def paper(hub: Hub) -> PaperHandler:
    return PaperHandler(hub=hub)


def _seed_paper(store: Store, slug: str, title: str = "Test Paper") -> int:
    """Insert a bare paper ref. Returns the ref id."""
    ref = store.insert_ref(kind="paper", slug=slug, title=title)
    return ref.id


class TestPaperPutAcceptedOps:
    def test_link_paper_to_paper(self, store: Store, paper: PaperHandler) -> None:
        """Paper-A `cites` paper-B is the headline use case."""
        a_id = _seed_paper(store, "paper-a", "A")
        b_id = _seed_paper(store, "paper-b", "B")
        out = paper.link(id="paper-a", target="paper:paper-b", rel="cites")
        assert "+1 link" in out.body
        assert "paper-a" in out.body
        # Verify the row landed.
        out_links = store.links_for(a_id, relation="cites", direction="out")
        assert len(out_links) == 1
        assert out_links[0].dst_ref_id == b_id

    def test_link_default_relation(self, store: Store, paper: PaperHandler) -> None:
        """Omitting rel= picks ``related-to``."""
        _seed_paper(store, "paper-a")
        _seed_paper(store, "paper-b")
        paper.link(id="paper-a", target="paper:paper-b")
        # Read it back from B's side via the inverse-aware filter.
        b_links = store.links_for(_seed_id_of(store, "paper-b"), direction="in")
        assert any(link.relation == "related-to" for link in b_links)

    def test_tags_added(self, store: Store, paper: PaperHandler) -> None:
        ref_id = _seed_paper(store, "paper-a")
        out = paper.tag(id="paper-a", add=["SRC:primary", "topic-co2"])
        assert "+2 tag" in out.body
        # Verify both rows landed.
        rows = store.tags_for(ref_id)
        values = {(t.namespace, t.prefix, t.value) for t in rows}
        assert ("closed", "SRC", "primary") in values
        assert ("open", None, "topic-co2") in values

    def test_unlink_removes(self, store: Store, paper: PaperHandler) -> None:
        a_id = _seed_paper(store, "paper-a")
        _seed_paper(store, "paper-b")
        store.add_link(
            src_ref_id=a_id,
            dst_ref_id=_seed_id_of(store, "paper-b"),
            relation="cites",
        )
        out = paper.link(
            id="paper-a", target="paper:paper-b", mode="remove", rel="cites"
        )
        assert "-1 link" in out.body
        assert store.links_for(a_id, relation="cites", direction="out") == []

    def test_untags_removes(self, store: Store, paper: PaperHandler) -> None:
        ref_id = _seed_paper(store, "paper-a")
        paper.tag(id="paper-a", add=["topic-co2"])
        out = paper.tag(id="paper-a", remove=["topic-co2"])
        assert "-1 tag" in out.body
        rows = store.tags_for(ref_id)
        assert all(t.value != "topic-co2" for t in rows)


class TestPaperPutRejected:
    """Paper bodies are import-only after the seven-verb cutover. The
    ``put`` verb is no longer wired on this kind; classification +
    cross-citation move to the dedicated ``tag`` / ``link`` verbs.

    These tests pin the new failure modes — both the wholesale
    ``put`` rejection and the per-axis validation that survives on
    the new verbs (e.g. ``STATUS:`` is still not on paper's allowed
    closed-axis list).
    """

    def test_put_unsupported(self, paper: PaperHandler, store: Store) -> None:
        """``put`` is unwired on paper; bodies arrive via .acatome
        bundle ingest, not the agent surface."""
        _seed_paper(store, "paper-a")
        from precis.errors import Unsupported

        with pytest.raises(Unsupported, match="paper does not support put"):
            paper.put(id="paper-a", text="rewrite me")

    def test_unknown_paper_on_link(self, paper: PaperHandler) -> None:
        with pytest.raises(NotFound, match="paper slug 'no-such' not found"):
            paper.link(id="no-such", target="paper:other")

    def test_chunk_selector_rejected(self, paper: PaperHandler, store: Store) -> None:
        _seed_paper(store, "paper-a")
        with pytest.raises(BadInput, match="paper ops operate at ref level"):
            paper.link(id="paper-a~46", target="paper:other")

    def test_path_view_rejected(self, paper: PaperHandler, store: Store) -> None:
        _seed_paper(store, "paper-a")
        with pytest.raises(BadInput, match="paper ops operate at ref level"):
            paper.link(id="paper-a/cite/bib", target="paper:other")

    def test_status_axis_rejected_on_paper(
        self, paper: PaperHandler, store: Store
    ) -> None:
        """Per-kind axis enforcement still fires — papers don't carry STATUS."""
        _seed_paper(store, "paper-a")
        with pytest.raises(BadInput, match="axis not allowed on kind 'paper'"):
            paper.tag(id="paper-a", add=["STATUS:open"])

    def test_tag_no_op_rejected(self, paper: PaperHandler, store: Store) -> None:
        """``tag()`` with neither add= nor remove= is a misuse."""
        _seed_paper(store, "paper-a")
        with pytest.raises(BadInput, match="requires add= or remove="):
            paper.tag(id="paper-a")

    def test_link_target_required(self, paper: PaperHandler, store: Store) -> None:
        """``link()`` requires a target= so a typo can't silently no-op."""
        _seed_paper(store, "paper-a")
        with pytest.raises(BadInput, match="requires target="):
            paper.link(id="paper-a")


class TestPaperBidirectionalGraph:
    """Verify the inverse-relation read-side rewrite still works after
    the put surface lands. Paper-A ``cites`` paper-B should be findable
    from B as ``cited-by`` without auto-mirror."""

    def test_who_cites_me(self, store: Store, paper: PaperHandler) -> None:
        a_id = _seed_paper(store, "paper-a")
        _seed_paper(store, "paper-b")
        b_id = _seed_id_of(store, "paper-b")
        paper.link(id="paper-a", target="paper:paper-b", rel="cites")
        # From B's side, query via the inverse name.
        cited_by = store.links_for(b_id, relation="cited-by", direction="out")
        assert len(cited_by) == 1
        assert cited_by[0].src_ref_id == a_id
        # The stored row's relation is still 'cites'.
        assert cited_by[0].relation == "cites"

    def test_inverse_rel_unlink(self, store: Store, paper: PaperHandler) -> None:
        """Removing via the literal direction works, regardless of
        which name was used to discover it."""
        a_id = _seed_paper(store, "paper-a")
        _seed_paper(store, "paper-b")
        paper.link(id="paper-a", target="paper:paper-b", rel="cites")
        paper.link(id="paper-a", target="paper:paper-b", mode="remove", rel="cites")
        b_id = _seed_id_of(store, "paper-b")
        assert store.links_for(b_id, relation="cited-by", direction="out") == []


# ── PerplexityBase.put — link/tag ops on cache slugs ───────────────


class TestPerplexityLinkTagOps:
    def test_import_then_link_to_paper(self, store: Store) -> None:
        """The motivating workflow: import a research report, then
        link it to the paper that prompted it."""
        from precis.handlers.perplexity import ResearchHandler

        research = ResearchHandler(hub=Hub(store=store))
        # Import a tiny report so a slug exists to link to.
        ack = research.put(
            id="why is the sky blue",
            text="# Answer\n\nRayleigh scattering.",
            mode="import",
        )
        # Pull the slug out of the ack body — format is "ref '<slug>'"
        slug = ack.body.split("ref '", 1)[1].split("'", 1)[0]
        # Seed a paper to link to.
        _seed_paper(store, "rayleigh1899")
        out = research.link(id=slug, target="paper:rayleigh1899", rel="derived-from")
        assert "+1 link" in out.body

    def test_tag_cache_pinned(self, store: Store) -> None:
        from precis.handlers.perplexity import ResearchHandler

        research = ResearchHandler(hub=Hub(store=store))
        ack = research.put(id="q", text="body", mode="import")
        slug = ack.body.split("ref '", 1)[1].split("'", 1)[0]
        out = research.tag(id=slug, add=["CACHE:pinned"])
        assert "+1 tag" in out.body

    def test_status_axis_rejected_on_research(self, store: Store) -> None:
        """Cache kinds only allow CACHE: — STATUS: must reject."""
        from precis.handlers.perplexity import ResearchHandler

        research = ResearchHandler(hub=Hub(store=store))
        ack = research.put(id="q", text="body", mode="import")
        slug = ack.body.split("ref '", 1)[1].split("'", 1)[0]
        with pytest.raises(
            BadInput, match="axis not allowed on kind 'perplexity-research'"
        ):
            research.tag(id=slug, add=["STATUS:open"])

    def test_import_with_link_kwarg_rejected(self, store: Store) -> None:
        """link/tag kwargs are no longer accepted on put — the error
        points the caller at the dedicated link verb."""
        from precis.handlers.perplexity import ResearchHandler

        research = ResearchHandler(hub=Hub(store=store))
        with pytest.raises(
            BadInput, match=r"link=/unlink=/rel= are not accepted on put"
        ):
            research.put(
                id="q",
                text="body",
                mode="import",
                link="paper:something",
            )

    def test_tags_kwarg_rejected_on_put(self, store: Store) -> None:
        """tags=/untags= are no longer accepted on put either."""
        from precis.handlers.perplexity import ResearchHandler

        research = ResearchHandler(hub=Hub(store=store))
        with pytest.raises(BadInput, match=r"tags=/untags= are not accepted on put"):
            research.put(id="q", text="body", mode="import", tags=["CACHE:pinned"])

    def test_link_unknown_slug(self, store: Store) -> None:
        """link verb on an unknown cache slug raises NotFound."""
        from precis.handlers.perplexity import ResearchHandler

        research = ResearchHandler(hub=Hub(store=store))
        with pytest.raises(NotFound, match="research slug 'no-such' not found"):
            research.link(id="no-such", target="paper:other")

    def test_put_without_mode_rejected(self, store: Store) -> None:
        """put on a perplexity kind requires mode='import'. Any other
        invocation rejects with the supported-modes hint."""
        from precis.handlers.perplexity import ResearchHandler

        research = ResearchHandler(hub=Hub(store=store))
        ack = research.put(id="q", text="body", mode="import")
        slug = ack.body.split("ref '", 1)[1].split("'", 1)[0]
        with pytest.raises(BadInput, match="mode='import'"):
            research.put(id=slug)

    def test_unknown_mode_rejected(self, store: Store) -> None:
        from precis.handlers.perplexity import ResearchHandler

        research = ResearchHandler(hub=Hub(store=store))
        with pytest.raises(BadInput, match="mode='import'"):
            research.put(id="q", text="body", mode="append")


# ── helpers ────────────────────────────────────────────────────────


def _seed_id_of(store: Store, slug: str) -> int:
    """Look up a paper ref id by slug (test helper)."""
    ref = store.get_ref(kind="paper", id=slug)
    assert ref is not None, f"paper slug {slug!r} not seeded"
    return ref.id
