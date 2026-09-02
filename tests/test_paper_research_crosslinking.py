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
    """Paper bodies are import-only. ``put`` mints *stubs* only
    (doi=/arxiv=/identifier=/title=); it never writes a body, so a
    ``put`` carrying ``text=`` is rejected. Classification +
    cross-citation move to the dedicated ``tag`` / ``link`` verbs.

    These tests pin the failure modes — the body-write rejection and
    the per-axis validation that survives on the new verbs (e.g.
    ``STATUS:`` is still not on paper's allowed closed-axis list).
    """

    def test_put_unsupported(self, paper: PaperHandler, store: Store) -> None:
        """``put`` with ``text=`` is a body rewrite — unsupported;
        bodies arrive via .acatome bundle ingest, not the agent
        surface. (Stub minting via doi=/title= is a separate path.)"""
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


# ── apply_link_ops — disputes/contradicts routing (D1-D4) ──────────
#
# docs/backlog/disputes-edge-nonblocking-disagreement.md: `contradicts` is
# adjudication-derived only (Part 2) and unfileable through the generic
# write door, except the pre-existing memory<->memory subsystem (D2);
# `disputes` between two live claim hubs delegates to
# `taproot.hub.link_claims` (D4). Exercised directly against
# `apply_link_ops` — the two production callers that route through it
# (``PaperHandler``/the Perplexity caches above) never have a claim-hub
# source, so the claim-hub-pair delegation can only be pinned at the
# function level here.


class TestApplyLinkOpsDisputesContradicts:
    def test_contradicts_rejected_between_two_findings(self, store: Store) -> None:
        from precis.handlers._link_tag_ops import apply_link_ops

        a = store.insert_ref(kind="finding", slug=None, title="a plain finding").id
        b = store.insert_ref(kind="finding", slug=None, title="another finding").id
        with pytest.raises(BadInput, match="adjudication-derived"):
            apply_link_ops(
                store, a, link=f"finding:{b}", unlink=None, rel="contradicts"
            )
        assert store.links_for(a, direction="out") == []

    def test_contradicts_still_allowed_between_two_memories(self, store: Store) -> None:
        from precis.handlers._link_tag_ops import apply_link_ops

        a = store.insert_ref(kind="memory", slug=None, title="memory a").id
        b = store.insert_ref(kind="memory", slug=None, title="memory b").id
        n_added, _ = apply_link_ops(
            store, a, link=f"memory:{b}", unlink=None, rel="contradicts"
        )
        assert n_added == 1
        out = store.links_for(a, direction="out")
        assert len(out) == 1 and out[0].relation == "contradicts"

    def test_disputes_between_two_claim_hubs_delegates_to_link_claims(
        self, store: Store
    ) -> None:
        from precis.handlers._link_tag_ops import apply_link_ops
        from precis.taproot.canon import CanonicalClaim
        from precis.taproot.hub import mint_hub

        a = mint_hub(
            store, CanonicalClaim(sentence="Claim A holds under condition X.", scope={})
        )
        b = mint_hub(
            store, CanonicalClaim(sentence="Claim B holds under condition Y.", scope={})
        )

        n_added, _ = apply_link_ops(
            store, a, link=f"finding:{b}", unlink=None, rel="disputes"
        )
        assert n_added == 1
        out = store.links_for(a, direction="out")
        assert (
            len(out) == 1 and out[0].relation == "disputes" and out[0].dst_ref_id == b
        )

        # Idempotent — link_claims' own no-op-on-existing, surfaced honestly
        # as n_added=0 rather than a second row.
        n_added_again, _ = apply_link_ops(
            store, a, link=f"finding:{b}", unlink=None, rel="disputes"
        )
        assert n_added_again == 0
        assert len(store.links_for(a, direction="out")) == 1

    def test_disputes_paper_to_finding_uses_plain_add_link(self, store: Store) -> None:
        """A non-claim-hub endpoint (e.g. paper->hub, the shape
        ``reattach_as_disputes`` also writes) falls through to the plain
        write — claim-hub delegation is only for a claim-hub pair."""
        from precis.handlers._link_tag_ops import apply_link_ops
        from precis.taproot.canon import CanonicalClaim
        from precis.taproot.hub import mint_hub

        paper_id = _seed_paper(store, "review2026")
        hub_id = mint_hub(
            store, CanonicalClaim(sentence="A claim a review note disputes.", scope={})
        )

        n_added, _ = apply_link_ops(
            store, paper_id, link=f"finding:{hub_id}", unlink=None, rel="disputes"
        )
        assert n_added == 1
        out = store.links_for(paper_id, direction="out")
        assert (
            len(out) == 1
            and out[0].relation == "disputes"
            and out[0].dst_ref_id == hub_id
        )


# ── NumericRefHandler.link — the same guard, a different door ──────
#
# `apply_link_ops` above pins the policy at the function level;
# `NumericRefHandler.link` (memory/todo/gripe/anki/conv, and a plain
# non-hub `finding` that falls through `FindingHandler.link`) is a SEPARATE
# add-mode write path that used to bypass the guard entirely (the gap
# `guard_and_route_contradicts_disputes` closes by being shared code, not a
# second copy of the policy).


class TestNumericRefHandlerLinkDisputesContradicts:
    def test_contradicts_rejected_finding_to_paper(self, store: Store) -> None:
        """A plain (non-hub) finding falls through ``FindingHandler.link``
        to the generic ``NumericRefHandler.link`` door — same guard as
        ``apply_link_ops``."""
        from precis.handlers.finding import FindingHandler

        a_id = store.insert_ref(kind="finding", slug=None, title="a plain finding").id
        _seed_paper(store, "contra-numeric-ref")
        h = FindingHandler(hub=Hub(store=store))
        with pytest.raises(BadInput, match="adjudication-derived"):
            h.link(id=a_id, target="paper:contra-numeric-ref", rel="contradicts")
        assert store.links_for(a_id, direction="out") == []

    def test_disputes_accepted_finding_to_paper(self, store: Store) -> None:
        """The same non-hub-finding->paper pair accepts ``disputes`` —
        neither endpoint is a live claim hub, so it falls through to the
        plain write (the claim-pair delegation only fires hub<->hub)."""
        from precis.handlers.finding import FindingHandler

        a_id = store.insert_ref(kind="finding", slug=None, title="a plain finding").id
        _seed_paper(store, "disp-numeric-ref")
        h = FindingHandler(hub=Hub(store=store))
        out = h.link(id=a_id, target="paper:disp-numeric-ref", rel="disputes")
        assert "linked" in out.body
        links = store.links_for(a_id, direction="out")
        assert len(links) == 1 and links[0].relation == "disputes"

    def test_contradicts_still_allowed_memory_to_memory(self, store: Store) -> None:
        """Memory<->memory is a different subsystem (D2) and keeps working
        through its own handler, not just through ``apply_link_ops``
        directly."""
        from precis.handlers.memory import MemoryHandler

        a_id = store.insert_ref(kind="memory", slug=None, title="memory a").id
        b_id = store.insert_ref(kind="memory", slug=None, title="memory b").id
        h = MemoryHandler(hub=Hub(store=store))
        out = h.link(id=a_id, target=f"memory:{b_id}", rel="contradicts")
        assert "linked" in out.body
        links = store.links_for(a_id, direction="out")
        assert len(links) == 1 and links[0].relation == "contradicts"


# ── helpers ────────────────────────────────────────────────────────


def _seed_id_of(store: Store, slug: str) -> int:
    """Look up a paper ref id by slug (test helper)."""
    ref = store.get_ref(kind="paper", id=slug)
    assert ref is not None, f"paper slug {slug!r} not seeded"
    return ref.id
