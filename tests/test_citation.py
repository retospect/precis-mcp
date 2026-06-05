"""Contract tests for :class:`precis.handlers.citation.CitationHandler`."""

from __future__ import annotations

import re

import pytest

from precis.errors import BadInput
from precis.handlers.citation import CitationHandler
from precis.hints import HintBus


def _make_handler(store):
    """Build a CitationHandler bound to a real fresh store."""
    # Minimal Hub stand-in — we only need .store + .embedder=None.
    class _StubHub:
        def __init__(self) -> None:
            self.store = store
            self.embedder = None
            self.hints = HintBus()

    return CitationHandler(hub=_StubHub())


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
                text="claim", source_handle="x~1", source_quote="q",
                verifier_confidence=1.5,
            )
        assert "between 0.0 and 1.0" in str(excinfo.value)
        with pytest.raises(BadInput):
            h.put(
                text="claim", source_handle="x~1", source_quote="q",
                verifier_confidence=-0.1,
            )


# ── put happy path ──────────────────────────────────────────────────


class TestPutHappy:
    def test_creates_with_meta_populated(self, store) -> None:
        h = _make_handler(store)
        resp = h.put(
            text="MOF X achieves 12% FE for CO2 reduction",
            source_handle="collins06~7",
            source_quote=(
                "we observed 12% Faradaic efficiency for CO2 "
                "reduction at -0.3 V"
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
