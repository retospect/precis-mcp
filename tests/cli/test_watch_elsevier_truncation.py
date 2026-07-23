"""Tests for :func:`precis.cli.watch._check_elsevier_truncation`.

Regression guard for gr162364/gr162363: ``fetcher:elsevier`` can return a
clean, complete PDF that is nonetheless just the entitlement-limited
preview page (no article body) — no exception, no ``fetch_failed``,
just a handful of front-matter chunks silently accepted as a full-text
ingest. This detector compares the cached payload's size against the
resulting chunk count and raises a ``critical`` alert when a paper this
thin came from a payload this large.
"""

from __future__ import annotations

from pathlib import Path

from precis.alerts import list_open_alerts
from precis.cli.watch import _check_elsevier_truncation
from precis.ingest.add import IngestResult
from precis.ingest.fetch_sidecar import FetchSidecar
from precis.store import Store


def _result(ref_id: int, *, chunks_written: int, inserted: bool = True) -> IngestResult:
    return IngestResult(
        ref_id=ref_id,
        inserted=inserted,
        paper_id=f"paper:{ref_id}",
        pub_id=None,
        cite_key=f"cite{ref_id}",
        pdf_sha256="0" * 64,
        content_hash="1" * 64,
        chunks_written=chunks_written,
        identifiers={"doi": "10.1016/j.desal.2026.120449"},
    )


def _sidecar(ref_id: int, *, source: str = "fetcher:elsevier") -> FetchSidecar:
    return FetchSidecar(
        ref_id=ref_id,
        identifiers={"doi": "10.1016/j.desal.2026.120449"},
        source=source,
    )


def _write(path: Path, *, size: int) -> None:
    path.write_bytes(b"%PDF-1.7\n" + b"x" * max(size - 9, 0))


class TestCheckElsevierTruncation:
    def test_flags_large_payload_with_too_few_chunks(
        self, tmp_path: Path, store: Store
    ) -> None:
        # 4 chunks (title/authors/abstract cards + one front-matter body
        # block) from a 647KB payload — well under the threshold, which
        # is set above the gripe's own pa162036 incident (9 chunks) so a
        # recurrence of that exact incident would also be caught, not
        # just a smaller one.
        pdf = tmp_path / "hierarchical26d.pdf"
        _write(pdf, size=647_277)
        ref = store.insert_ref(kind="paper", slug="h26d", title="X", meta={})

        _check_elsevier_truncation(
            pdf, _sidecar(ref.id), _result(ref.id, chunks_written=4), store=store
        )

        alerts = list_open_alerts(store)
        matching = [a for a in alerts if a["subject_ref_id"] == ref.id]
        assert len(matching) == 1
        assert matching[0]["severity"] == "critical"
        assert matching[0]["source"] == "watch:elsevier_truncation"

    def test_no_alert_when_chunk_count_is_healthy(
        self, tmp_path: Path, store: Store
    ) -> None:
        pdf = tmp_path / "fine.pdf"
        _write(pdf, size=647_277)
        ref = store.insert_ref(kind="paper", slug="fine", title="X", meta={})

        _check_elsevier_truncation(
            pdf, _sidecar(ref.id), _result(ref.id, chunks_written=40), store=store
        )

        alerts = list_open_alerts(store)
        assert not [a for a in alerts if a["subject_ref_id"] == ref.id]

    def test_no_alert_when_payload_is_small(self, tmp_path: Path, store: Store) -> None:
        # A genuinely short paper (or a real fetch_failed-sized blip) with
        # a small cached payload isn't the gr162364 signature — don't flag it.
        pdf = tmp_path / "small.pdf"
        _write(pdf, size=2_048)
        ref = store.insert_ref(kind="paper", slug="small", title="X", meta={})

        _check_elsevier_truncation(
            pdf, _sidecar(ref.id), _result(ref.id, chunks_written=2), store=store
        )

        alerts = list_open_alerts(store)
        assert not [a for a in alerts if a["subject_ref_id"] == ref.id]

    def test_no_alert_for_non_elsevier_source(
        self, tmp_path: Path, store: Store
    ) -> None:
        pdf = tmp_path / "other.pdf"
        _write(pdf, size=647_277)
        ref = store.insert_ref(kind="paper", slug="other", title="X", meta={})

        _check_elsevier_truncation(
            pdf,
            _sidecar(ref.id, source="fetcher:wiley"),
            _result(ref.id, chunks_written=3),
            store=store,
        )

        alerts = list_open_alerts(store)
        assert not [a for a in alerts if a["subject_ref_id"] == ref.id]

    def test_no_alert_without_sidecar(self, tmp_path: Path, store: Store) -> None:
        # Manual drop / no acquisition manifest — nothing to attribute to
        # fetcher:elsevier, so this detector has no signal either way.
        pdf = tmp_path / "manual.pdf"
        _write(pdf, size=647_277)
        ref = store.insert_ref(kind="paper", slug="manual", title="X", meta={})

        _check_elsevier_truncation(
            pdf, None, _result(ref.id, chunks_written=3), store=store
        )

        alerts = list_open_alerts(store)
        assert not [a for a in alerts if a["subject_ref_id"] == ref.id]

    def test_no_alert_on_idempotency_hit(self, tmp_path: Path, store: Store) -> None:
        # inserted=False means this call didn't write anything new (the
        # paper already existed) — the thin-chunk paper, if any, was
        # already flagged (or not) on the insert that actually wrote it.
        pdf = tmp_path / "existed.pdf"
        _write(pdf, size=647_277)
        ref = store.insert_ref(kind="paper", slug="existed", title="X", meta={})

        _check_elsevier_truncation(
            pdf,
            _sidecar(ref.id),
            _result(ref.id, chunks_written=3, inserted=False),
            store=store,
        )

        alerts = list_open_alerts(store)
        assert not [a for a in alerts if a["subject_ref_id"] == ref.id]
