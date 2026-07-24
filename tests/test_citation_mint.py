"""Tests for :func:`precis.quest.citation_mint.mint_citation`.

Rung 6d-1: a thin, code-callable wrapper over
:class:`precis.handlers.citation.CitationHandler` — mints a ``citation``
ref the way the weave (rung 6d-2) will, reusing the handler's own
validation (fabricated-bib-key guard, ``source_handle`` normalization)
rather than reimplementing it.
"""

from __future__ import annotations

import pytest

from precis.errors import NotFound
from precis.quest.citation_mint import mint_citation


def _seed_paper(store, slug: str, title: str) -> int:
    ref = store.insert_ref(kind="paper", slug=slug, title=title)
    return ref.id


class TestMintHappyPath:
    def test_mints_citation_with_link_and_card(self, store) -> None:
        paper_id = _seed_paper(store, "collins06", "Collins 2006 CO2 study")

        cid = mint_citation(
            store,
            claim="MOF X achieves 12% FE for CO2 reduction",
            paper_ref_id=paper_id,
            source_handle="collins06~7",
            source_quote="we observed 12% Faradaic efficiency for CO2 reduction",
            verifier_confidence=0.9,
        )
        assert isinstance(cid, int)

        ref = store.get_ref(kind="citation", id=cid)
        assert ref is not None
        assert ref.title == "MOF X achieves 12% FE for CO2 reduction"
        meta = ref.meta or {}
        assert meta["claim"] == "MOF X achieves 12% FE for CO2 reduction"
        assert meta["source_handle"] == "collins06~7"
        assert "Faradaic efficiency" in meta["source_quote"]
        assert meta["verifier_confidence"] == 0.9

        # cites -> paper link exists.
        links = store.links_for(cid, direction="out", relation="cites")
        assert any(link.dst_ref_id == paper_id for link in links)

        # card_combined chunk was embedded (ord=-1) — proves upsert_card_combined
        # ran, i.e. the handler's own create path executed, not a bypass.
        with store.pool.connection() as conn:
            card = conn.execute(
                "SELECT chunk_kind, text FROM chunks WHERE ref_id = %s AND ord = -1",
                (cid,),
            ).fetchone()
        assert card is not None
        assert card[0] == "card_combined"
        assert card[1] == "MOF X achieves 12% FE for CO2 reduction"

    def test_defaults_source_handle_and_quote_when_omitted(self, store) -> None:
        """Without an explicit chunk anchor, the wrapper falls back to the
        bare paper slug (enough for the paper-must-exist check) and the
        claim text itself as the quote — still a valid citation record."""
        paper_id = _seed_paper(store, "nospan24", "A paper with no chunk anchor")

        cid = mint_citation(
            store,
            claim="Nospan reports a 4x speedup",
            paper_ref_id=paper_id,
        )

        ref = store.get_ref(kind="citation", id=cid)
        assert ref is not None
        meta = ref.meta or {}
        assert meta["source_handle"] == "nospan24"
        assert meta["source_quote"] == "Nospan reports a 4x speedup"

        links = store.links_for(cid, direction="out", relation="cites")
        assert any(link.dst_ref_id == paper_id for link in links)

    def test_universal_chunk_handle_is_normalized(self, store) -> None:
        """A ``pc<id>`` universal handle (the form
        precis.quest.claims.own_chunks hands back) round-trips through the
        handler's own normalization to the canonical ``slug~ord`` form —
        exercising that mint_citation doesn't bypass it."""
        paper_id = _seed_paper(store, "handleform24", "Handle form paper")
        with store.pool.connection() as conn:
            row = conn.execute(
                "INSERT INTO chunks (ref_id, ord, text, chunk_kind) "
                "VALUES (%s, 0, %s, 'paragraph') RETURNING chunk_id",
                (paper_id, "the chunk body text"),
            ).fetchone()
            conn.commit()
        chunk_id = row[0]

        cid = mint_citation(
            store,
            claim="A claim grounded in a specific chunk",
            paper_ref_id=paper_id,
            source_handle=f"pc{chunk_id}",
            source_quote="the chunk body text",
        )

        ref = store.get_ref(kind="citation", id=cid)
        assert ref is not None
        meta = ref.meta or {}
        assert meta["source_handle"] == "handleform24~0"


class TestMintMissingPaper:
    def test_raises_when_paper_ref_id_does_not_exist(self, store) -> None:
        with pytest.raises(NotFound):
            mint_citation(
                store,
                claim="A claim about a paper that was never inserted",
                paper_ref_id=999_999,
            )
