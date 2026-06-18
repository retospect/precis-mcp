"""Contract tests for :class:`precis.handlers.citation.CitationHandler`."""

from __future__ import annotations

import re

import pytest

from precis.dispatch import Hub
from precis.errors import BadInput
from precis.handlers.citation import CitationHandler


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
        ref_id = int(re.search(r"id=(\d+)", resp.body).group(1))

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
        long_claim = ("Quantum-dot photocathodes sustain " + "record efficiency " * 15).strip()
        assert len(long_claim) > 200
        resp = h.put(
            text=long_claim,
            source_handle="cardpaper~2",
            source_quote="the cells reached record efficiency",
            verifier_confidence=0.9,
        )
        ref_id = int(re.search(r"id=(\d+)", resp.body).group(1))

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
        ref_id = int(re.search(r"id=(\d+)", resp.body).group(1))

        with store.pool.connection() as conn:
            rows = conn.execute(
                "SELECT ord, chunk_kind FROM chunks WHERE ref_id = %s ORDER BY ord",
                (ref_id,),
            ).fetchall()
        assert rows == [(-1, "card_combined")]
