"""Tests for :func:`precis.cli.watch._check_marker_fallback`.

Hardening half of gr236139: on melchior, every Marker invocation failed
for weeks (surya-ocr 0.22.1's new hard ``llama-server`` backend
requirement) and ingest silently fell back to fitz page-level
extraction — visible only as a per-PDF ``log.warning`` that rotted
unseen. This detector raises a rate-limited ops alert whenever a fresh
insert used the fallback, deduped onto one open alert row (not one per
PDF) so a standing fallback regime shows up as a single climbing
``seen_count`` rather than an alert flood.
"""

from __future__ import annotations

from pathlib import Path

from precis.alerts import list_open_alerts
from precis.cli.watch import (
    _MARKER_FALLBACK_ALERT_SOURCE,
    _check_marker_fallback,
)
from precis.ingest.add import IngestResult
from precis.store import Store


def _result(
    ref_id: int,
    *,
    inserted: bool = True,
    used_marker_fallback: bool = True,
) -> IngestResult:
    return IngestResult(
        ref_id=ref_id,
        inserted=inserted,
        paper_id=f"paper:{ref_id}",
        pub_id=None,
        cite_key=f"cite{ref_id}",
        pdf_sha256="a" * 64,
        content_hash="b" * 64,
        chunks_written=1,
        identifiers={"cite_key": f"cite{ref_id}"},
        used_marker_fallback=used_marker_fallback,
    )


class TestCheckMarkerFallback:
    def test_fires_on_fresh_insert_with_fallback(
        self, tmp_path: Path, store: Store
    ) -> None:
        pdf = tmp_path / "scanned.pdf"
        pdf.write_bytes(b"%PDF-1.7\nscanned")
        ref = store.insert_ref(kind="paper", slug="scanned1", title="X", meta={})

        _check_marker_fallback(pdf, _result(ref.id), store=store)

        alerts = list_open_alerts(store)
        matching = [a for a in alerts if a["source"] == _MARKER_FALLBACK_ALERT_SOURCE]
        assert len(matching) == 1
        assert matching[0]["severity"] == "warn"
        assert matching[0]["subject_ref_id"] == ref.id

    def test_no_alert_when_marker_succeeded(self, tmp_path: Path, store: Store) -> None:
        pdf = tmp_path / "clean.pdf"
        pdf.write_bytes(b"%PDF-1.7\nclean")
        ref = store.insert_ref(kind="paper", slug="clean1", title="X", meta={})

        _check_marker_fallback(
            pdf, _result(ref.id, used_marker_fallback=False), store=store
        )

        alerts = list_open_alerts(store)
        assert not [a for a in alerts if a["source"] == _MARKER_FALLBACK_ALERT_SOURCE]

    def test_no_alert_on_idempotency_hit(self, tmp_path: Path, store: Store) -> None:
        # inserted=False means no fresh extraction happened this call — the
        # fallback flag (if any) describes a *previous* ingest, already
        # handled (or not) on the insert that actually ran Marker.
        pdf = tmp_path / "existed.pdf"
        pdf.write_bytes(b"%PDF-1.7\nexisted")
        ref = store.insert_ref(kind="paper", slug="existed1", title="X", meta={})

        _check_marker_fallback(pdf, _result(ref.id, inserted=False), store=store)

        alerts = list_open_alerts(store)
        assert not [a for a in alerts if a["source"] == _MARKER_FALLBACK_ALERT_SOURCE]

    def test_dedups_across_multiple_pdfs_into_one_alert(
        self, tmp_path: Path, store: Store
    ) -> None:
        """A standing fallback regime — many different PDFs, same
        condition — must dedup onto a single open alert row (the whole
        point: 'this has become a regime' vs. an alert-table flood)."""
        pdf_a = tmp_path / "a.pdf"
        pdf_a.write_bytes(b"%PDF-1.7\na")
        pdf_b = tmp_path / "b.pdf"
        pdf_b.write_bytes(b"%PDF-1.7\nb")
        ref_a = store.insert_ref(kind="paper", slug="fba", title="A", meta={})
        ref_b = store.insert_ref(kind="paper", slug="fbb", title="B", meta={})

        _check_marker_fallback(pdf_a, _result(ref_a.id), store=store)
        _check_marker_fallback(pdf_b, _result(ref_b.id), store=store)

        alerts = list_open_alerts(store)
        matching = [a for a in alerts if a["source"] == _MARKER_FALLBACK_ALERT_SOURCE]
        assert len(matching) == 1
        assert matching[0]["seen_count"] >= 2
