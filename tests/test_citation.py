"""Contract tests for :class:`precis.handlers.citation.CitationHandler`."""

from __future__ import annotations

import re

import pytest

from precis.dispatch import Hub
from precis.errors import BadInput
from precis.handlers.citation import CitationHandler


def _search(pattern: str, text: str) -> re.Match[str]:
    """``re.search`` narrowed for tests — asserts the pattern actually hit."""
    m = re.search(pattern, text)
    assert m is not None, f"pattern {pattern!r} not found in {text!r}"
    return m


def _make_handler(store):
    """Build a CitationHandler bound to a real fresh store."""
    return CitationHandler(hub=Hub(store=store))


# ── put validation ──────────────────────────────────────────────────


class TestPutValidation:
    def test_id_rejected(self, store) -> None:
        h = _make_handler(store)
        with pytest.raises(BadInput) as excinfo:
            h.put(id=5, text="claim", source_handle="x~1", source_quote="q")
        assert "write-once" in str(excinfo.value)

    def test_requires_text(self, store) -> None:
        h = _make_handler(store)
        with pytest.raises(BadInput) as excinfo:
            h.put(text=None, source_handle="x~1", source_quote="q")
        assert "text" in str(excinfo.value)

    def test_requires_source_handle(self, store) -> None:
        h = _make_handler(store)
        with pytest.raises(BadInput) as excinfo:
            h.put(text="claim", source_handle="", source_quote="q")
        assert "source_handle" in str(excinfo.value)

    def test_requires_source_quote(self, store) -> None:
        h = _make_handler(store)
        with pytest.raises(BadInput) as excinfo:
            h.put(text="claim", source_handle="x~1", source_quote="")
        assert "source_quote" in str(excinfo.value)

    def test_confidence_range_validation(self, store) -> None:
        h = _make_handler(store)
        with pytest.raises(BadInput) as excinfo:
            h.put(
                text="claim",
                source_handle="x~1",
                source_quote="q",
                verifier_confidence=1.5,
            )
        assert "between 0.0 and 1.0" in str(excinfo.value)
        with pytest.raises(BadInput):
            h.put(
                text="claim",
                source_handle="x~1",
                source_quote="q",
                verifier_confidence=-0.1,
            )


# ── put happy path ──────────────────────────────────────────────────


class TestPutHappy:
    def test_creates_with_meta_populated(self, store) -> None:
        h = _make_handler(store)
        store.insert_ref(kind="paper", slug="collins06", title="Collins 2006 CO2 study")
        resp = h.put(
            text="MOF X achieves 12% FE for CO2 reduction",
            source_handle="collins06~7",
            source_quote=(
                "we observed 12% Faradaic efficiency for CO2 reduction at -0.3 V"
            ),
            char_offset=142,
            verifier_confidence=0.95,
            verifier_caveats=None,
        )
        m = re.search(r"id=(\d+)", resp.body)
        assert m, f"expected create-ack with id=N; got {resp.body!r}"
        ref_id = int(m.group(1))

        # Read back the row directly to confirm the meta landed.
        ref = store.get_ref(kind="citation", id=ref_id)
        assert ref is not None
        assert ref.title == "MOF X achieves 12% FE for CO2 reduction"
        meta = ref.meta or {}
        assert meta["claim"] == "MOF X achieves 12% FE for CO2 reduction"
        assert meta["source_handle"] == "collins06~7"
        assert "Faradaic efficiency" in meta["source_quote"]
        assert meta["char_offset"] == 142
        assert meta["verifier_confidence"] == 0.95
        assert meta.get("verified_at")

    def test_long_claim_stored_in_full_not_truncated(self, store) -> None:
        """A claim longer than the old 200-char cap is now stored whole in
        refs.title — display truncation is the web layer's job."""
        h = _make_handler(store)
        store.insert_ref(kind="paper", slug="longp", title="Long Paper")
        claim = (
            "Across Cu, Ni, Pt and Pd catalysts the measured Faradaic "
            "efficiency for the C2+ pathway climbs monotonically with "
            "applied overpotential up to -0.9 V vs RHE, beyond which "
            "hydrogen evolution dominates and the C2+ selectivity collapses "
            "to below ten percent in every system we tested."
        )
        assert len(claim) > 200
        resp = h.put(
            text=claim,
            source_handle="longp~2",
            source_quote=claim[:40],
            verifier_confidence=0.8,
        )
        ref_id = int(_search(r"id=(\d+)", resp.body).group(1))
        ref = store.get_ref(kind="citation", id=ref_id)
        assert ref is not None
        assert ref.title == claim  # full, not clipped at 200
        assert (ref.meta or {})["claim"] == claim

    def test_create_ack_carries_summary(self, store) -> None:
        h = _make_handler(store)
        store.insert_ref(kind="paper", slug="paperA", title="Paper A")
        resp = h.put(
            text="X improves Y by 12%",
            source_handle="paperA~3",
            source_quote="X improves Y by 12% under conditions Z.",
            verifier_confidence=0.9,
        )
        assert "created citation id=" in resp.body
        assert "source: paperA~3" in resp.body
        assert "verifier_confidence: 0.9" in resp.body
        assert "verified_at:" in resp.body


# ── get (round-trip) ────────────────────────────────────────────────


class TestRoundTrip:
    def test_get_renders_stored_record(self, store) -> None:
        h = _make_handler(store)
        store.insert_ref(kind="paper", slug="smith21", title="Smith 2021 MOF synthesis")
        resp = h.put(
            text="MOF synthesis yields 85%",
            source_handle="smith21~5",
            source_quote="Yields exceeded 85 percent in all batches.",
            verifier_confidence=0.88,
            verifier_caveats="single replicate",
        )
        ref_id = int(_search(r"id=(\d+)", resp.body).group(1))

        out = h.get(id=ref_id)
        body = out.body
        assert "MOF synthesis yields 85%" in body
        assert "smith21~5" in body
        assert "Yields exceeded 85 percent" in body
        assert "0.88" in body
        assert "single replicate" in body


# ── paper-must-exist validation ─────────────────────────────────────


class TestPaperMustExist:
    """The source_handle must resolve to a real ``kind='paper'`` ref.

    Catches the "LLM hallucinates a bib key, latexmk explodes" failure
    mode at put time instead of compile time.
    """

    def test_rejects_when_paper_absent(self, store) -> None:
        h = _make_handler(store)
        with pytest.raises(BadInput) as excinfo:
            h.put(
                text="Claim about a paper not in the corpus",
                source_handle="ghost2099~1",
                source_quote="hallucinated quote",
                verifier_confidence=0.9,
            )
        msg = str(excinfo.value)
        assert "ghost2099" in msg
        assert "no such paper" in msg

    def test_rejects_when_paper_absent_with_kind_prefix(self, store) -> None:
        h = _make_handler(store)
        with pytest.raises(BadInput) as excinfo:
            h.put(
                text="Claim about a paper not in the corpus",
                source_handle="paper:ghost2099",
                source_quote="hallucinated quote",
                verifier_confidence=0.9,
            )
        assert "ghost2099" in str(excinfo.value)

    def test_accepts_when_paper_present(self, store) -> None:
        h = _make_handler(store)
        store.insert_ref(
            kind="paper", slug="real2024", title="A real paper that exists"
        )
        resp = h.put(
            text="Real claim from a real paper",
            source_handle="real2024~3",
            source_quote="this is what the paper actually says",
            verifier_confidence=0.95,
        )
        assert "created citation id=" in resp.body

    def test_strips_chunk_range(self, store) -> None:
        h = _make_handler(store)
        store.insert_ref(kind="paper", slug="range2024", title="Range")
        resp = h.put(
            text="A claim with a chunk-range handle",
            source_handle="range2024~5..8",
            source_quote="quote spanning multiple chunks",
            verifier_confidence=0.7,
        )
        assert "created citation id=" in resp.body


# ── patent sources (docs/backlog/patent-evidence-parity.md Phase 3) ──


class TestPatentSource:
    def test_accepts_full_patent_source_handle_and_wires_cites_link(
        self, store
    ) -> None:
        ref = store.insert_ref(
            kind="patent",
            slug="ep1234567b1",
            title="A widget with improved catalysis",
            meta={"applicants": [{"name": "Acme Corp"}]},
        )
        h = _make_handler(store)
        resp = h.put(
            text="The widget improves conversion by 12%",
            source_handle="patent:ep1234567b1~3",
            source_quote="conversion improved by 12 percent",
            verifier_confidence=0.9,
            link="patent:ep1234567b1",
            rel="cites",
        )
        cid = int(_search(r"id=(\d+)", resp.body).group(1))

        stored = store.get_ref(kind="citation", id=cid)
        assert stored is not None
        # Explicit ``kind:`` prefixes aren't handle_registry-parseable (the
        # ':' defeats the universal-handle grammar), so they store verbatim
        # -- same pre-existing behavior an explicit 'paper:<slug>' handle
        # already gets.
        assert (stored.meta or {})["source_handle"] == "patent:ep1234567b1~3"

        links = store.links_for(cid, direction="out", relation="cites")
        assert any(link.dst_ref_id == ref.id for link in links)

    def test_universal_patent_chunk_handle_normalizes_and_validates(
        self, store
    ) -> None:
        """A ``pk<id>`` universal chunk handle (ADR 0036) resolves to its
        patent's slug~ord form *and* is validated against kind='patent',
        not silently mis-defaulted to 'paper' (the resolved-kind carry-through
        fix)."""
        ref = store.insert_ref(
            kind="patent", slug="ep7654321b1", title="Universal-handle patent"
        )
        with store.pool.connection() as conn:
            row = conn.execute(
                "INSERT INTO chunks (ref_id, ord, text, chunk_kind) "
                "VALUES (%s, 0, %s, 'paragraph') RETURNING chunk_id",
                (ref.id, "the claim body text"),
            ).fetchone()
            conn.commit()
        chunk_id = row[0]

        h = _make_handler(store)
        resp = h.put(
            text="A claim grounded in a patent chunk",
            source_handle=f"pk{chunk_id}",
            source_quote="the claim body text",
            verifier_confidence=0.9,
        )
        cid = int(_search(r"id=(\d+)", resp.body).group(1))
        stored = store.get_ref(kind="citation", id=cid)
        assert stored is not None
        assert (stored.meta or {})["source_handle"] == "ep7654321b1~0"

    def test_universal_handle_of_non_accepted_kind_rejected(self, store) -> None:
        """A bare universal chunk handle (no ``kind:`` prefix) of a kind
        outside {paper, patent} — e.g. an EDGAR filing's ``ec<id>`` chunk
        handle — must not slip past the allowlist just because it resolves.
        Before the fix, only the explicit ``kind:`` prefix branch enforced
        _ACCEPTED_SOURCE_KINDS; a bare handle's resolved kind rode through
        unchecked as ``default_kind``."""
        ref = store.insert_ref(
            kind="edgar", slug="0000320193-24-000010", title="An EDGAR filing"
        )
        with store.pool.connection() as conn:
            row = conn.execute(
                "INSERT INTO chunks (ref_id, ord, text, chunk_kind) "
                "VALUES (%s, 0, %s, 'paragraph') RETURNING chunk_id",
                (ref.id, "filing body text"),
            ).fetchone()
            conn.commit()
        chunk_id = row[0]

        h = _make_handler(store)
        with pytest.raises(BadInput) as excinfo:
            h.put(
                text="Claim wrongly sourced from an EDGAR chunk",
                source_handle=f"ec{chunk_id}",
                source_quote="filing body text",
                verifier_confidence=0.5,
            )
        assert "unsupported source kind" in str(excinfo.value)

    def test_bare_slug_still_defaults_to_paper(self, store) -> None:
        """A bare (unprefixed) source_handle keeps validating against
        kind='paper' — backward compatible default."""
        h = _make_handler(store)
        with pytest.raises(BadInput) as excinfo:
            h.put(
                text="Claim about an unqualified handle",
                source_handle="not-ingested-anywhere~1",
                source_quote="q",
                verifier_confidence=0.5,
            )
        assert "paper" in str(excinfo.value)

    def test_stub_patent_source_rejected_naming_representative(self, store) -> None:
        from precis.handlers._patent_ingest import FAMILY_STUB_META_KEY

        store.insert_ref(
            kind="patent",
            slug="ep0000001a1",
            title="Family representative",
            meta={"family_id": "fam-cite", "publication_date": "2019-01-01"},
        )
        store.insert_ref(
            kind="patent",
            slug="ep0000002a1",
            title="Family stub member",
            meta={
                "family_id": "fam-cite",
                "publication_date": "2020-01-01",
                FAMILY_STUB_META_KEY: True,
            },
        )
        h = _make_handler(store)
        with pytest.raises(BadInput) as excinfo:
            h.put(
                text="Claim sourced from a stub",
                source_handle="patent:ep0000002a1~1",
                source_quote="q",
                verifier_confidence=0.5,
            )
        msg = str(excinfo.value)
        assert "stub" in msg
        assert "ep0000001a1" in msg  # names the representative

    def test_unknown_source_kind_rejected(self, store) -> None:
        h = _make_handler(store)
        with pytest.raises(BadInput) as excinfo:
            h.put(
                text="Claim with a bogus source kind",
                source_handle="memory:5",
                source_quote="q",
                verifier_confidence=0.5,
            )
        assert "unsupported source kind" in str(excinfo.value)


# ── claim → embeddable card ──────────────────────────────────────────


class TestClaimCard:
    """The full claim is mirrored into a ``card_combined`` chunk (ord=-1)
    so the embed + chunk_keywords workers index it. ``refs.title`` only
    holds a 200-char truncation and ``refs.meta`` isn't indexed at all,
    so without the card a long claim is unreachable by semantic search."""

    def test_full_claim_emitted_as_card(self, store) -> None:
        h = _make_handler(store)
        store.insert_ref(kind="paper", slug="cardpaper", title="Card Paper")
        # > 200 chars so we can prove the card carries the *full* claim,
        # not the truncated title.
        long_claim = (
            "Quantum-dot photocathodes sustain " + "record efficiency " * 15
        ).strip()
        assert len(long_claim) > 200
        resp = h.put(
            text=long_claim,
            source_handle="cardpaper~2",
            source_quote="the cells reached record efficiency",
            verifier_confidence=0.9,
        )
        ref_id = int(_search(r"id=(\d+)", resp.body).group(1))

        with store.pool.connection() as conn:
            card = conn.execute(
                "SELECT chunk_kind, text FROM chunks WHERE ref_id = %s AND ord = -1",
                (ref_id,),
            ).fetchone()
        assert card is not None, "expected a card_combined chunk at ord=-1"
        assert card[0] == "card_combined"
        # The card holds the full claim, not the truncated refs.title.
        assert card[1] == long_claim

    def test_quote_is_not_chunked(self, store) -> None:
        """source_quote is a verbatim copy of the source_handle span, which
        is already an embedded chunk — only the claim card is emitted, so
        a citation has exactly one negative-ord chunk and no body chunk."""
        h = _make_handler(store)
        store.insert_ref(kind="paper", slug="quotepaper", title="Quote Paper")
        resp = h.put(
            text="A short claim",
            source_handle="quotepaper~1",
            source_quote="a verbatim quote that should not become its own chunk",
            verifier_confidence=0.8,
        )
        ref_id = int(_search(r"id=(\d+)", resp.body).group(1))

        with store.pool.connection() as conn:
            rows = conn.execute(
                "SELECT ord, chunk_kind FROM chunks WHERE ref_id = %s ORDER BY ord",
                (ref_id,),
            ).fetchall()
        assert rows == [(-1, "card_combined")]
