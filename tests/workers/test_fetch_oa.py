"""Tests for ``precis.workers.fetch_oa``.

The OA fetcher walks the stub backlog (papers with identifiers known
but ``pdf_sha256 IS NULL``) through a three-source cascade
(Unpaywall → arXiv → S2), records every attempt as a ``ref_events``
row, and drops successful PDFs into the watch inbox. Coverage shape:

* :func:`claim_stubs_to_fetch`: only papers without a PDF whose
  identifier set isn't empty; honours the retry-window predicate.
* :func:`_try_unpaywall` / :func:`_try_arxiv` / :func:`_try_s2`:
  each returns ``None`` when the relevant identifier is missing;
  emits the right ``FetchOutcome.event`` for each branch.
* :func:`run_oa_fetch_pass` cascade: stops at first ``fetch_ok``,
  records intermediate events, isolates per-stub failures, plays
  nicely with the SSRF guard.

External HTTP is monkeypatched: we replace
``_query_unpaywall``, ``_download_pdf``, ``_query_s2_openaccess``
with stubs that return canned data or raise. The httpx-client-level
shape lives in :mod:`tests.test_safe_fetch` instead — these tests
focus on cascade orchestration + DB shape.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import httpx
import pytest

from precis.ingest.fetch_sidecar import read_sidecar
from precis.store import Store
from precis.utils.safe_fetch import SsrfBlocked
from precis.workers import fetch_oa
from precis.workers.fetch_oa import (
    StubRef,
    _is_elsevier_doi,
    _is_wiley_doi,
    _publisher_pdf_urls,
    _try_arxiv,
    _try_core,
    _try_crossref,
    _try_elsevier,
    _try_europepmc,
    _try_openalex,
    _try_publisher,
    _try_s2,
    _try_unpaywall,
    _try_wiley,
    claim_stubs_to_fetch,
    run_oa_fetch_pass,
)

# ---------------------------------------------------------------------------
# Seeding helpers
# ---------------------------------------------------------------------------


def _seed_paper_stub(
    store: Store,
    *,
    cite_key: str = "smith2024example",
    title: str = "An Example Paper",
    doi: str | None = "10.1234/example",
    arxiv: str | None = None,
    s2_id: str | None = None,
) -> int:
    """Seed a ``kind='paper'`` ref with no PDF and the given identifiers.

    v2: ``refs`` no longer carries a ``slug`` column — the cite_key
    lives under ``ref_identifiers (id_kind='cite_key')``. We use
    the store API which knows the layout instead of hand-rolling
    the INSERT.
    """
    ref = store.insert_ref(
        kind="paper",
        slug=cite_key,
        title=title,
        meta={},
    )
    ref_id = ref.id
    with store.pool.connection() as conn:
        if doi is not None:
            conn.execute(
                "INSERT INTO ref_identifiers (ref_id, id_kind, id_value, source) "
                "VALUES (%s, 'doi', %s, 'manual')",
                (ref_id, doi),
            )
        if arxiv is not None:
            conn.execute(
                "INSERT INTO ref_identifiers (ref_id, id_kind, id_value, source) "
                "VALUES (%s, 'arxiv', %s, 'manual')",
                (ref_id, arxiv),
            )
        if s2_id is not None:
            conn.execute(
                "INSERT INTO ref_identifiers (ref_id, id_kind, id_value, source) "
                "VALUES (%s, 's2', %s, 'manual')",
                (ref_id, s2_id),
            )
        conn.commit()
    return ref_id


def _write_synthetic_pdf(path: Path, *, size: int = 1024) -> int:
    """Drop a tiny file that starts with the ``%PDF-`` magic bytes.

    Used by stub ``_download_pdf`` impls so the inbox path actually
    has a file on disk afterwards — handy for asserting outcome
    payload shape without running the real downloader.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = b"%PDF-1.7\n" + b"x" * (size - len(b"%PDF-1.7\n"))
    path.write_bytes(payload)
    return len(payload)


# ---------------------------------------------------------------------------
# StubRef constructor — shared across legs
# ---------------------------------------------------------------------------


def _stub(
    *,
    ref_id: int = 1,
    doi: str | None = "10.1234/x",
    arxiv: str | None = None,
    s2_id: str | None = None,
    cite_key: str | None = "smith2024example",
) -> StubRef:
    return StubRef(ref_id=ref_id, doi=doi, arxiv=arxiv, s2_id=s2_id, cite_key=cite_key)


# ---------------------------------------------------------------------------
# claim_stubs_to_fetch — derived-queue claim shape
# ---------------------------------------------------------------------------


class TestClaimStubs:
    def test_returns_paper_with_doi(self, store: Store) -> None:
        ref_id = _seed_paper_stub(store, doi="10.1234/a")
        with store.pool.connection() as conn:
            stubs = claim_stubs_to_fetch(conn, limit=10)
            conn.commit()
        assert [s.ref_id for s in stubs] == [ref_id]
        assert stubs[0].doi == "10.1234/a"
        assert stubs[0].cite_key == "smith2024example"

    def test_multi_identifier_does_not_raise_cardinality(self, store: Store) -> None:
        """A ref carrying >1 identifier of the same kind (two DOIs / cite_keys
        from a dedup-merge) must not blow up the per-id scalar subqueries.
        Regression: a bare scalar subquery returning >1 row raised
        CardinalityViolation and took the whole fetch_oa pass down every tick.
        """
        ref_id = _seed_paper_stub(store, doi="10.1234/a", cite_key="dup2024")
        with store.pool.connection() as conn:
            conn.execute(
                "INSERT INTO ref_identifiers (ref_id, id_kind, id_value, source) "
                "VALUES (%s, 'doi', '10.5678/b', 'manual'), "
                "       (%s, 'cite_key', 'dup2024b', 'manual')",
                (ref_id, ref_id),
            )
            conn.commit()
            stubs = claim_stubs_to_fetch(conn, limit=10)  # must not raise
            conn.commit()
        s = next(s for s in stubs if s.ref_id == ref_id)
        # min() yields one stable representative per kind, never an error.
        assert s.doi == "10.1234/a"
        assert s.cite_key == "dup2024"

    def test_excludes_ref_without_identifier(self, store: Store) -> None:
        # paper with NO doi/arxiv/s2 — claim's EXISTS clause drops it.
        _seed_paper_stub(store, cite_key="naked2024", doi=None, arxiv=None, s2_id=None)
        with store.pool.connection() as conn:
            stubs = claim_stubs_to_fetch(conn, limit=10)
            conn.commit()
        assert stubs == []

    def test_excludes_ref_with_pdf(self, store: Store) -> None:
        ref_id = _seed_paper_stub(store, doi="10.1234/b")
        # Promote to non-stub by stamping a pdf_sha256.
        with store.pool.connection() as conn:
            conn.execute(
                "INSERT INTO pdfs (pdf_sha256, content_hash, page_count, "
                "size_bytes, storage_path) "
                "VALUES (%s, %s, 1, 100, '/tmp/stub')",
                ("a" * 64, "a" * 64),
            )
            conn.execute(
                "UPDATE refs SET pdf_sha256 = %s WHERE ref_id = %s",
                ("a" * 64, ref_id),
            )
            conn.commit()
        with store.pool.connection() as conn:
            stubs = claim_stubs_to_fetch(conn, limit=10)
            conn.commit()
        assert stubs == []

    def test_skips_recently_attempted(self, store: Store) -> None:
        ref_id = _seed_paper_stub(store, doi="10.1234/c")
        # Stamp a recent fetcher event — within the default 24h window.
        with store.pool.connection() as conn:
            conn.execute(
                "INSERT INTO ref_events (ref_id, source, event, payload) "
                "VALUES (%s, 'fetcher:unpaywall', 'no_oa_version', '{}'::jsonb)",
                (ref_id,),
            )
            conn.commit()
        with store.pool.connection() as conn:
            stubs = claim_stubs_to_fetch(conn, limit=10)
            conn.commit()
        # The recent-event predicate excludes this stub from the
        # current claim batch.
        assert [s.ref_id for s in stubs] == []

    def test_skips_recently_attempted_any_fetcher_source(self, store: Store) -> None:
        # Regression: the retry window used to key only on
        # ``fetcher:unpaywall``. In prod Unpaywall is disabled (no
        # email) so only arXiv + S2 run, never writing an unpaywall
        # event — the window never armed and S2 got re-polled every
        # pass. A recent ``fetcher:s2`` event must now suppress the
        # stub just like an unpaywall one.
        ref_id = _seed_paper_stub(store, doi="10.1234/s2only", s2_id="s2:abc")
        with store.pool.connection() as conn:
            conn.execute(
                "INSERT INTO ref_events (ref_id, source, event, payload) "
                "VALUES (%s, 'fetcher:s2', 'no_oa_version', '{}'::jsonb)",
                (ref_id,),
            )
            conn.commit()
        with store.pool.connection() as conn:
            stubs = claim_stubs_to_fetch(conn, limit=10)
            conn.commit()
        assert [s.ref_id for s in stubs] == []

    def test_reclaims_after_window_expires(self, store: Store) -> None:
        # An attempt older than the 24h window leaves the stub eligible
        # again — the guard is a backoff, not a permanent exclusion.
        ref_id = _seed_paper_stub(store, doi="10.1234/old")
        with store.pool.connection() as conn:
            conn.execute(
                "INSERT INTO ref_events (ref_id, source, event, payload, ts) "
                "VALUES (%s, 'fetcher:s2', 'no_oa_version', '{}'::jsonb, "
                "now() - interval '25 hours')",
                (ref_id,),
            )
            conn.commit()
        with store.pool.connection() as conn:
            stubs = claim_stubs_to_fetch(conn, limit=10)
            conn.commit()
        assert [s.ref_id for s in stubs] == [ref_id]

    def test_backoff_widens_with_attempt_count(self, store: Store) -> None:
        # After several attempts the retry window doubles per attempt
        # (24h → 48h → 96h …). With 3 prior fetcher events the window
        # is 96h, so a 25h-old last attempt — which WOULD reclaim a
        # once-tried stub — is still suppressed. Stops daily re-polling
        # of papers that have no OA copy anywhere.
        ref_id = _seed_paper_stub(store, doi="10.1234/backoff")
        with store.pool.connection() as conn:
            for age in (200, 100, 25):  # 3 attempts; most recent 25h ago
                conn.execute(
                    "INSERT INTO ref_events (ref_id, source, event, payload, ts) "
                    "VALUES (%s, 'fetcher:s2', 'no_oa_version', '{}'::jsonb, "
                    "now() - make_interval(hours => %s))",
                    (ref_id, age),
                )
            conn.commit()
        with store.pool.connection() as conn:
            stubs = claim_stubs_to_fetch(conn, limit=10)
            conn.commit()
        assert [s.ref_id for s in stubs] == []

    def test_backoff_is_capped(self, store: Store) -> None:
        # The doubling is capped (default 720h / 30d) so a chronically
        # un-fetchable stub still gets one more try per ~month — never a
        # permanent give-up (a paper can become OA later). With many
        # attempts the window pins at the cap; a last attempt older than
        # the cap re-qualifies the stub.
        ref_id = _seed_paper_stub(store, doi="10.1234/capped")
        with store.pool.connection() as conn:
            # 8 attempts (uncapped window would be 24*2^7 = 3072h) but the
            # cap holds it at 720h; last attempt 800h ago > cap → eligible.
            for age in (3000, 2000, 1500, 1200, 1000, 900, 850, 800):
                conn.execute(
                    "INSERT INTO ref_events (ref_id, source, event, payload, ts) "
                    "VALUES (%s, 'fetcher:s2', 'no_oa_version', '{}'::jsonb, "
                    "now() - make_interval(hours => %s))",
                    (ref_id, age),
                )
            conn.commit()
        with store.pool.connection() as conn:
            stubs = claim_stubs_to_fetch(conn, limit=10)
            conn.commit()
        assert [s.ref_id for s in stubs] == [ref_id]

    def test_orders_newest_first(self, store: Store) -> None:
        first = _seed_paper_stub(store, cite_key="a2024", doi="10.1/a")
        second = _seed_paper_stub(store, cite_key="b2024", doi="10.1/b")
        with store.pool.connection() as conn:
            stubs = claim_stubs_to_fetch(conn, limit=10)
            conn.commit()
        # ORDER BY r.ref_id DESC — newest first.
        assert [s.ref_id for s in stubs] == [second, first]

    def test_requeued_stub_sorts_ahead_of_newer(self, store: Store) -> None:
        # An explicitly re-queued (meta.oa_requeued) OLD stub jumps ahead
        # of a newer un-requeued one, despite the lower ref_id — otherwise
        # the reset stub starves at the back of the newest-first backlog.
        old = _seed_paper_stub(store, cite_key="old2024", doi="10.1/old")
        new = _seed_paper_stub(store, cite_key="new2024", doi="10.1/new")
        with store.pool.connection() as conn:
            conn.execute(
                "UPDATE refs SET meta = meta || '{\"oa_requeued\": {}}'::jsonb "
                "WHERE ref_id = %s",
                (old,),
            )
            conn.commit()
        with store.pool.connection() as conn:
            stubs = claim_stubs_to_fetch(conn, limit=10)
            conn.commit()
        # Re-queued `old` first, then the newer `new` — priority beats recency.
        assert [s.ref_id for s in stubs] == [old, new]


# ---------------------------------------------------------------------------
# _try_unpaywall — per-leg outcomes
# ---------------------------------------------------------------------------


class TestTryUnpaywall:
    def test_returns_none_when_no_doi(self, tmp_path: Path) -> None:
        out = _try_unpaywall(_stub(doi=None), inbox_dir=tmp_path, email="a@b")
        assert out is None

    def test_invalid_doi_shape(self, tmp_path: Path) -> None:
        out = _try_unpaywall(_stub(doi="not-a-doi"), inbox_dir=tmp_path, email="a@b")
        assert out is not None
        assert out.event == "invalid_identifier"
        assert out.payload["doi"] == "not-a-doi"

    def test_no_oa_version(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # API returned the paper but no best_oa_location URL.
        monkeypatch.setattr(
            fetch_oa,
            "_query_unpaywall",
            lambda doi, *, email: {
                "is_oa": False,
                "oa_status": "closed",
                "best_oa_location": None,
            },
        )
        out = _try_unpaywall(_stub(doi="10.1234/x"), inbox_dir=tmp_path, email="a@b")
        assert out is not None
        assert out.event == "no_oa_version"
        assert out.payload["oa_status"] == "closed"

    def test_rate_limited(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Build a real httpx HTTPStatusError with status 429.
        def _raise_429(doi: str, *, email: str) -> dict[str, Any]:
            request = httpx.Request("GET", f"https://api.unpaywall.org/v2/{doi}")
            response = httpx.Response(
                429, headers={"retry-after": "60"}, request=request
            )
            raise httpx.HTTPStatusError(
                "rate limited", request=request, response=response
            )

        monkeypatch.setattr(fetch_oa, "_query_unpaywall", _raise_429)
        out = _try_unpaywall(_stub(doi="10.1234/x"), inbox_dir=tmp_path, email="a@b")
        assert out is not None
        assert out.event == "rate_limited"
        assert out.payload["retry_after"] == "60"

    def test_api_error_on_unexpected_exception(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def _boom(doi: str, *, email: str) -> dict[str, Any]:
            raise RuntimeError("connection reset")

        monkeypatch.setattr(fetch_oa, "_query_unpaywall", _boom)
        out = _try_unpaywall(_stub(doi="10.1234/x"), inbox_dir=tmp_path, email="a@b")
        assert out is not None
        assert out.event == "api_error"
        assert "connection reset" in out.payload["error"]

    def test_fetch_ok_writes_pdf(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            fetch_oa,
            "_query_unpaywall",
            lambda doi, *, email: {
                "is_oa": True,
                "oa_status": "gold",
                "best_oa_location": {
                    "url_for_pdf": "https://example.org/x.pdf",
                    "license": "cc-by",
                    "host_type": "publisher",
                    "version": "publishedVersion",
                },
            },
        )
        # Replace the downloader with a stub that drops a real %PDF- on disk.
        monkeypatch.setattr(
            fetch_oa,
            "_download_pdf",
            lambda url, target: _write_synthetic_pdf(target, size=2048),
        )
        out = _try_unpaywall(_stub(doi="10.1234/x"), inbox_dir=tmp_path, email="a@b")
        assert out is not None
        assert out.event == "fetch_ok"
        assert out.payload["license"] == "cc-by"
        assert out.payload["host_type"] == "publisher"
        assert out.payload["size_bytes"] == 2048
        # File landed in the inbox under the cite_key.
        landed = tmp_path / "smith2024example.pdf"
        assert landed.is_file()
        assert landed.read_bytes().startswith(b"%PDF-")

    def test_ssrf_blocked_surfaces_as_fetch_failed(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Unpaywall returns a URL that the safe-fetch guard refuses.
        monkeypatch.setattr(
            fetch_oa,
            "_query_unpaywall",
            lambda doi, *, email: {
                "best_oa_location": {
                    "url_for_pdf": "http://127.0.0.1/leak.pdf",
                }
            },
        )

        def _refuse(url: str, target: Path) -> int:
            raise SsrfBlocked(f"refusing host: {url}")

        monkeypatch.setattr(fetch_oa, "_download_pdf", _refuse)
        out = _try_unpaywall(_stub(doi="10.1234/x"), inbox_dir=tmp_path, email="a@b")
        assert out is not None
        assert out.event == "fetch_failed"
        assert "refusing host" in out.payload["error"]


# ---------------------------------------------------------------------------
# _try_arxiv + _try_s2 — same shape as unpaywall, focused checks
# ---------------------------------------------------------------------------


class TestTryArxiv:
    def test_none_without_arxiv_id(self, tmp_path: Path) -> None:
        assert _try_arxiv(_stub(arxiv=None), inbox_dir=tmp_path) is None

    def test_invalid_id_shape(self, tmp_path: Path) -> None:
        out = _try_arxiv(_stub(arxiv="not an id"), inbox_dir=tmp_path)
        assert out is not None
        assert out.event == "invalid_identifier"

    def test_strips_arxiv_prefix(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        seen_urls: list[str] = []

        def _capture(url: str, target: Path) -> int:
            seen_urls.append(url)
            return _write_synthetic_pdf(target, size=256)

        monkeypatch.setattr(fetch_oa, "_download_pdf", _capture)
        out = _try_arxiv(_stub(arxiv="arxiv:2401.12345"), inbox_dir=tmp_path)
        assert out is not None
        assert out.event == "fetch_ok"
        # URL built without the prefix.
        assert seen_urls == ["https://arxiv.org/pdf/2401.12345.pdf"]
        assert out.payload["license"] == "arxiv"


class TestTryS2:
    def test_priority_doi_over_arxiv(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        seen_paper_ids: list[str] = []

        def _stub_query(paper_id: str) -> str | None:
            seen_paper_ids.append(paper_id)
            return None  # no_oa_version

        monkeypatch.setattr(fetch_oa, "_query_s2_openaccess", _stub_query)
        out = _try_s2(_stub(doi="10.1234/x", arxiv="2401.12345"), inbox_dir=tmp_path)
        assert out is not None
        assert out.event == "no_oa_version"
        # DOI wins the priority race.
        assert seen_paper_ids == ["doi:10.1234/x"]

    def test_none_without_any_identifier(self, tmp_path: Path) -> None:
        assert (
            _try_s2(
                _stub(doi=None, arxiv=None, s2_id=None),
                inbox_dir=tmp_path,
            )
            is None
        )

    def test_fetch_ok(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            fetch_oa,
            "_query_s2_openaccess",
            lambda paper_id: "https://example.org/from-s2.pdf",
        )
        monkeypatch.setattr(
            fetch_oa,
            "_download_pdf",
            lambda url, target: _write_synthetic_pdf(target, size=512),
        )
        out = _try_s2(_stub(doi="10.1234/x"), inbox_dir=tmp_path)
        assert out is not None
        assert out.event == "fetch_ok"
        assert out.payload["host_type"] == "s2_openaccess"


# ---------------------------------------------------------------------------
# _try_publisher — deterministic publisher PDF patterns
# ---------------------------------------------------------------------------


class TestPublisherUrls:
    def test_springer_bmc_prefix(self) -> None:
        assert _publisher_pdf_urls("10.1186/s13027-026-00740-z") == [
            "https://link.springer.com/content/pdf/10.1186/s13027-026-00740-z.pdf"
        ]

    def test_springer_hybrid_prefix(self) -> None:
        assert _publisher_pdf_urls("10.1007/s00018-024-05123-4") == [
            "https://link.springer.com/content/pdf/10.1007/s00018-024-05123-4.pdf"
        ]

    def test_plos_journal_slug_from_infix(self) -> None:
        assert _publisher_pdf_urls("10.1371/journal.pone.0173664") == [
            "https://journals.plos.org/plosone/article/file"
            "?id=10.1371/journal.pone.0173664&type=printable"
        ]
        # A different PLOS journal code picks a different slug.
        assert _publisher_pdf_urls("10.1371/journal.pcbi.1011000") == [
            "https://journals.plos.org/ploscompbiol/article/file"
            "?id=10.1371/journal.pcbi.1011000&type=printable"
        ]

    def test_plos_unknown_journal_code_falls_through(self) -> None:
        # A journal code we haven't verified a slug for → no candidate,
        # cascade falls through to the aggregators rather than 404ing.
        assert _publisher_pdf_urls("10.1371/journal.pxyz.0000001") == []

    def test_unknown_prefix_empty(self) -> None:
        assert _publisher_pdf_urls("10.1234/whatever") == []

    def test_prefix_boundary_not_a_substring_match(self) -> None:
        # ``10.11860`` must NOT match the ``10.1186`` Springer prefix.
        assert _publisher_pdf_urls("10.11860/abc") == []


class TestTryPublisher:
    def test_none_without_doi(self, tmp_path: Path) -> None:
        assert _try_publisher(_stub(doi=None), inbox_dir=tmp_path) is None

    def test_none_for_malformed_doi(self, tmp_path: Path) -> None:
        assert _try_publisher(_stub(doi="not-a-doi"), inbox_dir=tmp_path) is None

    def test_none_for_unregistered_prefix(self, tmp_path: Path) -> None:
        # 10.1234 isn't in the registry → silent fall-through, no event.
        assert _try_publisher(_stub(doi="10.1234/x"), inbox_dir=tmp_path) is None

    def test_fetch_ok_uses_springer_url(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        seen_urls: list[str] = []

        def _capture(url: str, target: Path) -> int:
            seen_urls.append(url)
            return _write_synthetic_pdf(target, size=4096)

        monkeypatch.setattr(fetch_oa, "_download_pdf", _capture)
        out = _try_publisher(
            _stub(doi="10.1186/s13027-026-00740-z"), inbox_dir=tmp_path
        )
        assert out is not None
        assert out.event == "fetch_ok"
        assert out.payload["host_type"] == "publisher_pattern"
        assert out.payload["size_bytes"] == 4096
        assert seen_urls == [
            "https://link.springer.com/content/pdf/10.1186/s13027-026-00740-z.pdf"
        ]

    def test_fetch_failed_when_candidate_not_a_pdf(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Magic-byte guard rejects an HTML interstitial → fetch_failed,
        # which lets the cascade fall through to the aggregators.
        def _reject(url: str, target: Path) -> int:
            raise ValueError("response is not a PDF (got 5000 bytes, head=b'<!DOCTY')")

        monkeypatch.setattr(fetch_oa, "_download_pdf", _reject)
        out = _try_publisher(
            _stub(doi="10.1186/s13027-026-00740-z"), inbox_dir=tmp_path
        )
        assert out is not None
        assert out.event == "fetch_failed"
        assert "is not a PDF" in out.payload["error"]


# ---------------------------------------------------------------------------
# _try_elsevier — key-gated Article API leg
# ---------------------------------------------------------------------------


class TestTryElsevier:
    def test_none_without_key(self, tmp_path: Path) -> None:
        assert (
            _try_elsevier(
                _stub(doi="10.1016/j.x.2025.1"), inbox_dir=tmp_path, api_key=""
            )
            is None
        )

    def test_none_for_non_elsevier_prefix(self, tmp_path: Path) -> None:
        assert (
            _try_elsevier(_stub(doi="10.1186/x"), inbox_dir=tmp_path, api_key="K")
            is None
        )

    def test_none_for_malformed_doi(self, tmp_path: Path) -> None:
        assert (
            _try_elsevier(_stub(doi="not-a-doi"), inbox_dir=tmp_path, api_key="K")
            is None
        )

    def test_is_elsevier_doi_boundary(self) -> None:
        assert _is_elsevier_doi("10.1016/j.amf.2025.200253")
        assert not _is_elsevier_doi("10.10160/x")  # prefix boundary
        assert not _is_elsevier_doi("10.1186/x")

    def test_fetch_ok_sends_api_key_header(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        seen: dict[str, Any] = {}

        def _capture(url: str, target: Path, *, extra_headers: Any = None) -> int:
            seen["url"] = url
            seen["headers"] = extra_headers
            return _write_synthetic_pdf(target, size=4096)

        monkeypatch.setattr(fetch_oa, "_download_pdf", _capture)
        out = _try_elsevier(
            _stub(doi="10.1016/j.amf.2025.200253"), inbox_dir=tmp_path, api_key="SECRET"
        )
        assert out is not None
        assert out.event == "fetch_ok"
        assert out.payload["host_type"] == "elsevier_api"
        assert seen["url"] == (
            "https://api.elsevier.com/content/article/doi/10.1016/j.amf.2025.200253"
        )
        # The key + PDF Accept ride the request as headers.
        assert seen["headers"]["X-ELS-APIKey"] == "SECRET"
        assert seen["headers"]["Accept"] == "application/pdf"

    def test_fetch_failed_on_non_pdf(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Non-entitled article → API returns an XML error body, guard
        # rejects it → fetch_failed, cascade continues.
        def _reject(url: str, target: Path, *, extra_headers: Any = None) -> int:
            raise ValueError("response is not a PDF (got 900 bytes, head=b'<?xml')")

        monkeypatch.setattr(fetch_oa, "_download_pdf", _reject)
        out = _try_elsevier(
            _stub(doi="10.1016/j.x.2025.1"), inbox_dir=tmp_path, api_key="K"
        )
        assert out is not None
        assert out.event == "fetch_failed"


# ---------------------------------------------------------------------------
# _try_wiley — token-gated TDM leg
# ---------------------------------------------------------------------------


class TestTryWiley:
    def test_none_without_token(self, tmp_path: Path) -> None:
        assert (
            _try_wiley(_stub(doi="10.1002/advs.1"), inbox_dir=tmp_path, token="")
            is None
        )

    def test_none_for_non_wiley_prefix(self, tmp_path: Path) -> None:
        assert _try_wiley(_stub(doi="10.1016/x"), inbox_dir=tmp_path, token="T") is None

    def test_is_wiley_doi(self) -> None:
        assert _is_wiley_doi("10.1002/advs.202100707")
        assert _is_wiley_doi("10.1111/jpi.12345")
        assert not _is_wiley_doi("10.1016/j.x.1")

    def test_fetch_ok_sends_token_header(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        seen: dict[str, Any] = {}

        def _capture(url: str, target: Path, *, extra_headers: Any = None) -> int:
            seen["url"] = url
            seen["headers"] = extra_headers
            return _write_synthetic_pdf(target, size=2048)

        monkeypatch.setattr(fetch_oa, "_download_pdf", _capture)
        out = _try_wiley(
            _stub(doi="10.1002/advs.201801586"), inbox_dir=tmp_path, token="TKN"
        )
        assert out is not None
        assert out.event == "fetch_ok"
        assert out.payload["host_type"] == "wiley_tdm"
        assert seen["url"] == (
            "https://api.wiley.com/onlinelibrary/tdm/v1/articles/10.1002/advs.201801586"
        )
        assert seen["headers"]["Wiley-TDM-Client-Token"] == "TKN"


# ---------------------------------------------------------------------------
# _try_core — green-OA repository leg
# ---------------------------------------------------------------------------


class TestTryCore:
    def test_none_without_key(self, tmp_path: Path) -> None:
        assert _try_core(_stub(doi="10.1234/x"), inbox_dir=tmp_path, api_key="") is None

    def test_none_without_doi(self, tmp_path: Path) -> None:
        assert _try_core(_stub(doi=None), inbox_dir=tmp_path, api_key="K") is None

    def test_no_oa_when_no_repo_copy(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            fetch_oa, "_query_core_fulltext_urls", lambda doi, *, api_key: []
        )
        out = _try_core(_stub(doi="10.1234/x"), inbox_dir=tmp_path, api_key="K")
        assert out is not None
        assert out.event == "no_oa_version"

    def test_fetch_ok_sends_browser_ua(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        seen: dict[str, Any] = {}

        def _capture(url: str, target: Path, *, extra_headers: Any = None) -> int:
            seen["headers"] = extra_headers
            return _write_synthetic_pdf(target, size=256)

        monkeypatch.setattr(
            fetch_oa,
            "_query_core_fulltext_urls",
            lambda doi, *, api_key: ["https://repo.example/bitstream/x.pdf"],
        )
        monkeypatch.setattr(fetch_oa, "_download_pdf", _capture)
        out = _try_core(_stub(doi="10.1234/x"), inbox_dir=tmp_path, api_key="K")
        assert out is not None
        assert out.event == "fetch_ok"
        assert out.payload["host_type"] == "core"
        # CORE downloads carry a browser UA to clear repo Cloudflare gates.
        assert "Mozilla" in seen["headers"]["User-Agent"]

    def test_api_error(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        def _boom(doi: str, *, api_key: str) -> list[str]:
            raise RuntimeError("core down")

        monkeypatch.setattr(fetch_oa, "_query_core_fulltext_urls", _boom)
        out = _try_core(_stub(doi="10.1234/x"), inbox_dir=tmp_path, api_key="K")
        assert out is not None
        assert out.event == "api_error"


class TestQueryCorePdfUrls:
    """Unit tests for :func:`_query_core_fulltext_urls` validation and field choice."""

    def test_filters_invalid_urls_and_skips_non_pdf_fulltext(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from precis.workers.fetch_oa import _query_core_fulltext_urls

        class FakeResp:
            status_code = 200

            def raise_for_status(self) -> None:
                pass

            def json(self) -> dict[str, Any]:
                return {
                    "results": [
                        {
                            "doi": "10.1234/example",
                            "downloadUrl": "https://repo.example/paper.pdf",
                            "fullText": "https://repo.example/paper.txt",
                        },
                        {
                            "doi": "10.1234/Example",
                            "downloadUrl": "587670336",
                            "fullText": "not a url",
                        },
                        {
                            "doi": "10.1234/example",
                            "downloadUrl": "ftp://bad.example/paper.pdf",
                            "fullText": "https://repo.example/fulltext.pdf",
                        },
                        {
                            "doi": "10.1234/other",
                            "downloadUrl": "https://repo.example/other.pdf",
                            "fullText": None,
                        },
                    ]
                }

        class FakeClient:
            def __init__(self, *args, **kwargs):
                pass

            def get(self, *args, **kwargs):
                return FakeResp()

            def __enter__(self):
                return self

            def __exit__(self, *args):
                pass

        monkeypatch.setattr(httpx, "Client", FakeClient)

        urls = _query_core_fulltext_urls("10.1234/example", api_key="K")
        # The rec-1 fullText ``paper.txt`` is dropped (clearly non-PDF); the
        # rec-3 fullText ``fulltext.pdf`` is kept (its downloadUrl is an
        # invalid ftp:// URL). Bare-id / non-URL / wrong-DOI recs are skipped.
        assert urls == [
            "https://repo.example/paper.pdf",
            "https://repo.example/fulltext.pdf",
        ]


# ---------------------------------------------------------------------------
# _try_crossref / _try_openalex / _try_europepmc — Tier-1 keyless legs
# ---------------------------------------------------------------------------


class TestTryCrossref:
    def test_none_without_email(self, tmp_path: Path) -> None:
        assert (
            _try_crossref(_stub(doi="10.1234/x"), inbox_dir=tmp_path, email="") is None
        )

    def test_none_without_doi(self, tmp_path: Path) -> None:
        assert _try_crossref(_stub(doi=None), inbox_dir=tmp_path, email="a@b") is None

    def test_no_oa_when_no_pdf_links(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            fetch_oa, "_query_crossref_pdf_links", lambda doi, *, email: []
        )
        out = _try_crossref(_stub(doi="10.1234/x"), inbox_dir=tmp_path, email="a@b")
        assert out is not None
        assert out.event == "no_oa_version"

    def test_fetch_ok(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            fetch_oa,
            "_query_crossref_pdf_links",
            lambda doi, *, email: ["https://pub.example/tdm.pdf"],
        )
        monkeypatch.setattr(
            fetch_oa,
            "_download_pdf",
            lambda url, target, **kw: _write_synthetic_pdf(target, size=256),
        )
        out = _try_crossref(_stub(doi="10.1234/x"), inbox_dir=tmp_path, email="a@b")
        assert out is not None
        assert out.event == "fetch_ok"
        assert out.payload["host_type"] == "crossref_tdm"

    def test_api_error(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        def _boom(doi: str, *, email: str) -> list[str]:
            raise RuntimeError("crossref down")

        monkeypatch.setattr(fetch_oa, "_query_crossref_pdf_links", _boom)
        out = _try_crossref(_stub(doi="10.1234/x"), inbox_dir=tmp_path, email="a@b")
        assert out is not None
        assert out.event == "api_error"


class TestTryOpenalex:
    def test_none_without_doi(self, tmp_path: Path) -> None:
        assert _try_openalex(_stub(doi=None), inbox_dir=tmp_path, email="") is None

    def test_no_oa_when_no_pdf_urls(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            fetch_oa, "_query_openalex_pdf_urls", lambda doi, *, email: []
        )
        out = _try_openalex(_stub(doi="10.1234/x"), inbox_dir=tmp_path, email="")
        assert out is not None
        assert out.event == "no_oa_version"

    def test_fetch_ok(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            fetch_oa,
            "_query_openalex_pdf_urls",
            lambda doi, *, email: ["https://repo.example/green.pdf"],
        )
        monkeypatch.setattr(
            fetch_oa,
            "_download_pdf",
            lambda url, target, **kw: _write_synthetic_pdf(target, size=256),
        )
        out = _try_openalex(_stub(doi="10.1234/x"), inbox_dir=tmp_path, email="a@b")
        assert out is not None
        assert out.event == "fetch_ok"
        assert out.payload["host_type"] == "openalex"


class TestTryOpenalexContent:
    def test_none_without_key(self, tmp_path: Path) -> None:
        assert (
            fetch_oa._try_openalex_content(
                _stub(doi="10.3390/x"), inbox_dir=tmp_path, api_key=""
            )
            is None
        )

    def test_none_without_doi(self, tmp_path: Path) -> None:
        assert (
            fetch_oa._try_openalex_content(
                _stub(doi=None), inbox_dir=tmp_path, api_key="K"
            )
            is None
        )

    def test_no_oa_when_nothing_cached(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            fetch_oa, "_query_openalex_content_urls", lambda doi, *, email="": {}
        )
        out = fetch_oa._try_openalex_content(
            _stub(doi="10.3390/x"), inbox_dir=tmp_path, api_key="K"
        )
        assert out is not None
        assert out.event == "no_oa_version"

    def test_no_oa_when_only_tei_cached(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Phase 1 fetches PDF only; a TEI-only work has no PDF to pull yet.
        monkeypatch.setattr(
            fetch_oa,
            "_query_openalex_content_urls",
            lambda doi, *, email="": {
                "grobid_xml": "https://content.openalex.org/w.grobid-xml"
            },
        )
        out = fetch_oa._try_openalex_content(
            _stub(doi="10.3390/x"), inbox_dir=tmp_path, api_key="K"
        )
        assert out is not None
        assert out.event == "no_oa_version"
        assert out.payload["cached"] == ["grobid_xml"]

    def test_failure_scrubs_key_from_error(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # httpx errors embed the full request URL incl. ?api_key=… — the leg
        # must scrub it before it lands in ref_events / the CLI.
        monkeypatch.setattr(
            fetch_oa,
            "_query_openalex_content_urls",
            lambda doi, *, email="": {"pdf": "https://content.openalex.org/W1.pdf"},
        )

        def _boom(url: str, target: Path, **kw: Any) -> int:
            raise httpx.HTTPStatusError(
                f"Client error '403 Forbidden' for url '{url}'",
                request=httpx.Request("GET", url),
                response=httpx.Response(403),
            )

        monkeypatch.setattr(fetch_oa, "_download_pdf", _boom)
        out = fetch_oa._try_openalex_content(
            _stub(doi="10.3390/x"), inbox_dir=tmp_path, api_key="SECRETKEY"
        )
        assert out is not None
        assert out.event == "fetch_failed"
        assert "SECRETKEY" not in str(out.payload)
        assert "***" in out.payload["error"]

    def test_fetch_ok_records_cost_and_hides_key(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            fetch_oa,
            "_query_openalex_content_urls",
            lambda doi, *, email="": {"pdf": "https://content.openalex.org/W1.pdf"},
        )
        seen: dict[str, str] = {}

        def _fake_download(url: str, target: Path, **kw: Any) -> int:
            seen["url"] = url
            return _write_synthetic_pdf(target, size=256)

        monkeypatch.setattr(fetch_oa, "_download_pdf", _fake_download)
        out = fetch_oa._try_openalex_content(
            _stub(doi="10.3390/x"), inbox_dir=tmp_path, api_key="SECRET"
        )
        assert out is not None
        assert out.event == "fetch_ok"
        assert out.payload["host_type"] == "openalex_content"
        assert out.cost_usd == fetch_oa._OPENALEX_CONTENT_COST_USD
        # key rides the download URL...
        assert "api_key=SECRET" in seen["url"]
        # ...but never the recorded payload.
        assert "SECRET" not in str(out.payload)

    def test_auto_gate_off_by_default(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("PRECIS_OPENALEX_CONTENT_AUTO", raising=False)
        assert fetch_oa._openalex_content_auto() is False
        monkeypatch.setenv("PRECIS_OPENALEX_CONTENT_AUTO", "1")
        assert fetch_oa._openalex_content_auto() is True


class TestQueryOpenalexContentUrls:
    """The keyless metadata call that decides whether to spend."""

    @staticmethod
    def _fake_client(
        monkeypatch: pytest.MonkeyPatch, *, status: int, body: Any
    ) -> None:
        class _Resp:
            status_code = status

            def raise_for_status(self) -> None:
                if status >= 400:
                    raise httpx.HTTPStatusError(
                        f"{status}",
                        request=httpx.Request("GET", "http://x"),
                        response=httpx.Response(status),
                    )

            def json(self) -> Any:
                return body

        class _Client:
            def __init__(self, *a: Any, **k: Any) -> None:
                pass

            def __enter__(self) -> _Client:
                return self

            def __exit__(self, *a: Any) -> None:
                return None

            def get(self, *a: Any, **k: Any) -> _Resp:
                return _Resp()

        monkeypatch.setattr(fetch_oa.httpx, "Client", _Client)

    def test_404_is_empty_not_error(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # A DOI not in OpenAlex → {} (→ no_oa_version), never a raised api_error.
        self._fake_client(monkeypatch, status=404, body=None)
        assert fetch_oa._query_openalex_content_urls("10.1/missing") == {}

    def test_keeps_only_cached_types(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self._fake_client(
            monkeypatch,
            status=200,
            body={
                "has_content": {"pdf": True, "grobid_xml": False},
                "content_urls": {
                    "pdf": "https://c/W.pdf",
                    "grobid_xml": "https://c/W.x",
                },
            },
        )
        got = fetch_oa._query_openalex_content_urls("10.1/x")
        assert got == {"pdf": "https://c/W.pdf"}  # grobid dropped (has_content False)


class TestTryEuropepmc:
    def test_none_without_doi(self, tmp_path: Path) -> None:
        assert _try_europepmc(_stub(doi=None), inbox_dir=tmp_path) is None

    def test_no_oa_when_not_in_pmc(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(fetch_oa, "_query_europepmc_oa_pmcid", lambda doi: None)
        out = _try_europepmc(_stub(doi="10.1234/x"), inbox_dir=tmp_path)
        assert out is not None
        assert out.event == "no_oa_version"

    def test_fetch_ok_renders_pmc_pdf(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        seen_urls: list[str] = []

        def _capture(url: str, target: Path, **kw: Any) -> int:
            seen_urls.append(url)
            return _write_synthetic_pdf(target, size=256)

        monkeypatch.setattr(
            fetch_oa, "_query_europepmc_oa_pmcid", lambda doi: "PMC7513516"
        )
        monkeypatch.setattr(fetch_oa, "_download_pdf", _capture)
        out = _try_europepmc(_stub(doi="10.1234/x"), inbox_dir=tmp_path)
        assert out is not None
        assert out.event == "fetch_ok"
        assert out.payload["host_type"] == "europepmc"
        assert seen_urls == ["https://europepmc.org/articles/PMC7513516?pdf=render"]


# ---------------------------------------------------------------------------
# run_oa_fetch_pass — cascade orchestration
# ---------------------------------------------------------------------------


class TestRunCascade:
    @pytest.fixture(autouse=True)
    def _enable_oa_fetch(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # run_oa_fetch_pass is env-gated (PRECIS_OA_FETCH) so the
        # fetcher only runs on one cluster host. Enable it for the
        # cascade tests; the off-by-default behaviour has its own test.
        monkeypatch.setenv("PRECIS_OA_FETCH", "1")
        # No key-gated leg fires by default (the dev host's env may
        # carry real credentials); leg-specific tests pass them
        # explicitly.
        monkeypatch.delenv("PRECIS_ELSEVIER_API_KEY", raising=False)
        monkeypatch.delenv("PRECIS_WILEY_TDM_TOKEN", raising=False)
        monkeypatch.delenv("PRECIS_CORE_API_KEY", raising=False)
        # Default every external query to "found nothing" so the cascade
        # is hermetic — no test accidentally hits Crossref/OpenAlex/
        # Europe PMC/CORE/Unpaywall/S2 over the network. Individual tests
        # override the leg they exercise.
        monkeypatch.setattr(
            fetch_oa,
            "_query_unpaywall",
            lambda doi, *, email: {"best_oa_location": None},
        )
        monkeypatch.setattr(
            fetch_oa, "_query_crossref_pdf_links", lambda doi, *, email: []
        )
        monkeypatch.setattr(
            fetch_oa, "_query_openalex_pdf_urls", lambda doi, *, email: []
        )
        monkeypatch.setattr(fetch_oa, "_query_europepmc_oa_pmcid", lambda doi: None)
        monkeypatch.setattr(
            fetch_oa, "_query_core_fulltext_urls", lambda doi, *, api_key: []
        )
        monkeypatch.setattr(fetch_oa, "_query_s2_openaccess", lambda paper_id: None)

    def test_disabled_by_default_short_circuits(
        self,
        store: Store,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # Without PRECIS_OA_FETCH the pass must claim nothing — even
        # with a fetchable stub present — so non-pinned hosts never
        # race the inbox.
        monkeypatch.delenv("PRECIS_OA_FETCH", raising=False)
        _seed_paper_stub(store, doi="10.1234/off")
        monkeypatch.setattr(
            fetch_oa,
            "_download_pdf",
            lambda url, target: pytest.fail("fetcher must not run when gated off"),
        )
        result = run_oa_fetch_pass(store, limit=10, inbox_dir=tmp_path, email="a@b")
        assert result == {"claimed": 0, "ok": 0, "failed": 0}

    def test_publisher_pattern_runs_first_and_stops_cascade(
        self,
        store: Store,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # A BMC DOI must land via the deterministic Springer URL on the
        # publisher leg — before Unpaywall/S2 are ever consulted.
        ref_id = _seed_paper_stub(store, doi="10.1186/s13027-026-00740-z")
        seen_urls: list[str] = []

        def _capture(url: str, target: Path) -> int:
            seen_urls.append(url)
            return _write_synthetic_pdf(target, size=256)

        monkeypatch.setattr(fetch_oa, "_download_pdf", _capture)
        monkeypatch.setattr(
            fetch_oa,
            "_query_unpaywall",
            lambda doi, *, email: pytest.fail("Unpaywall must not run"),
        )
        monkeypatch.setattr(
            fetch_oa,
            "_query_s2_openaccess",
            lambda paper_id: pytest.fail("S2 must not run"),
        )

        result = run_oa_fetch_pass(store, limit=10, inbox_dir=tmp_path, email="a@b")
        assert result == {"claimed": 1, "ok": 1, "failed": 0}
        assert seen_urls == [
            "https://link.springer.com/content/pdf/10.1186/s13027-026-00740-z.pdf"
        ]
        with store.pool.connection() as conn:
            rows = conn.execute(
                "SELECT source, event FROM ref_events WHERE ref_id = %s ORDER BY ts",
                (ref_id,),
            ).fetchall()
        assert rows == [("fetcher:publisher", "fetch_ok")]

    def test_fetch_ok_writes_sidecar_manifest(
        self,
        store: Store,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # A successful fetch drops an acquisition manifest next to the PDF
        # carrying the stub's ref_id, so ingest folds into *this* stub
        # instead of minting a duplicate when Marker mis-extracts identity.
        ref_id = _seed_paper_stub(
            store, doi="10.1186/s13027-026-00740-z", cite_key="thorpe83"
        )
        monkeypatch.setattr(
            fetch_oa,
            "_download_pdf",
            lambda url, target: _write_synthetic_pdf(target, size=256),
        )
        run_oa_fetch_pass(store, limit=10, inbox_dir=tmp_path, email="a@b")

        pdf = tmp_path / "thorpe83.pdf"
        assert pdf.exists()
        sc = read_sidecar(pdf)
        assert sc is not None
        assert sc.ref_id == ref_id
        assert sc.identifiers.get("doi") == "10.1186/s13027-026-00740-z"
        assert sc.source == "fetcher:publisher"

    def test_publisher_miss_falls_through_to_unpaywall(
        self,
        store: Store,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # Unregistered DOI prefix → publisher leg is a silent no-op
        # (no event), and Unpaywall handles it as before.
        ref_id = _seed_paper_stub(store, doi="10.1234/plain")
        monkeypatch.setattr(
            fetch_oa,
            "_query_unpaywall",
            lambda doi, *, email: {
                "best_oa_location": {"url_for_pdf": "https://x/y.pdf"}
            },
        )
        monkeypatch.setattr(
            fetch_oa,
            "_download_pdf",
            lambda url, target: _write_synthetic_pdf(target, size=128),
        )
        run_oa_fetch_pass(store, limit=10, inbox_dir=tmp_path, email="a@b")
        with store.pool.connection() as conn:
            rows = conn.execute(
                "SELECT source, event FROM ref_events WHERE ref_id = %s ORDER BY ts",
                (ref_id,),
            ).fetchall()
        # No fetcher:publisher row — the leg skipped silently.
        assert rows == [("fetcher:unpaywall", "fetch_ok")]

    def test_unpaywall_ok_stops_cascade(
        self,
        store: Store,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _seed_paper_stub(store, doi="10.1234/d")
        monkeypatch.setattr(
            fetch_oa,
            "_query_unpaywall",
            lambda doi, *, email: {
                "best_oa_location": {"url_for_pdf": "https://x/y.pdf"}
            },
        )
        monkeypatch.setattr(
            fetch_oa,
            "_download_pdf",
            lambda url, target: _write_synthetic_pdf(target, size=128),
        )
        # arXiv + S2 must not be called.
        monkeypatch.setattr(
            fetch_oa,
            "_query_s2_openaccess",
            lambda paper_id: pytest.fail("S2 should not be called after fetch_ok"),
        )

        result = run_oa_fetch_pass(store, limit=10, inbox_dir=tmp_path, email="a@b")
        assert result == {"claimed": 1, "ok": 1, "failed": 0}

    def test_falls_through_to_arxiv(
        self,
        store: Store,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _seed_paper_stub(store, doi="10.1234/e", arxiv="2401.99999")
        # Unpaywall says no_oa_version → cascade tries arXiv.
        monkeypatch.setattr(
            fetch_oa,
            "_query_unpaywall",
            lambda doi, *, email: {"best_oa_location": None},
        )

        seen_urls: list[str] = []

        def _capture(url: str, target: Path) -> int:
            seen_urls.append(url)
            return _write_synthetic_pdf(target, size=128)

        monkeypatch.setattr(fetch_oa, "_download_pdf", _capture)

        result = run_oa_fetch_pass(store, limit=10, inbox_dir=tmp_path, email="a@b")
        assert result == {"claimed": 1, "ok": 1, "failed": 0}
        # arXiv URL was downloaded (not the Unpaywall one).
        assert seen_urls == ["https://arxiv.org/pdf/2401.99999.pdf"]

    def test_records_every_attempted_provider(
        self,
        store: Store,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        ref_id = _seed_paper_stub(store, doi="10.1234/f", arxiv="2401.88888")
        # Every DOI aggregator (defaulted to no_oa by the autouse
        # fixture) is recorded in cascade order; arXiv then lands the
        # preprint. The publisher/elsevier legs return None (no DOI
        # match / no key) so they leave no row.
        monkeypatch.setattr(
            fetch_oa,
            "_download_pdf",
            lambda url, target: _write_synthetic_pdf(target, size=128),
        )

        run_oa_fetch_pass(store, limit=10, inbox_dir=tmp_path, email="a@b")

        with store.pool.connection() as conn:
            rows = conn.execute(
                "SELECT source, event FROM ref_events WHERE ref_id = %s ORDER BY ts",
                (ref_id,),
            ).fetchall()
        # Aggregators recorded in order; arXiv fetch_ok stops the cascade
        # before S2.
        assert rows == [
            ("fetcher:unpaywall", "no_oa_version"),
            ("fetcher:crossref", "no_oa_version"),
            ("fetcher:openalex", "no_oa_version"),
            ("fetcher:europepmc", "no_oa_version"),
            ("fetcher:arxiv", "fetch_ok"),
        ]

    def test_elsevier_leg_lands_sciencedirect_doi(
        self,
        store: Store,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # An Elsevier DOI with a key set lands via the Elsevier API
        # leg — before Unpaywall (which only has the doi.org landing
        # page) is consulted.
        ref_id = _seed_paper_stub(store, doi="10.1016/j.amf.2025.200253")
        seen: dict[str, Any] = {}

        def _capture(url: str, target: Path, *, extra_headers: Any = None) -> int:
            seen["url"] = url
            seen["headers"] = extra_headers
            return _write_synthetic_pdf(target, size=4096)

        monkeypatch.setattr(fetch_oa, "_download_pdf", _capture)
        monkeypatch.setattr(
            fetch_oa,
            "_query_unpaywall",
            lambda doi, *, email: pytest.fail("Unpaywall must not run"),
        )

        result = run_oa_fetch_pass(
            store, limit=10, inbox_dir=tmp_path, email="a@b", api_key="SECRET"
        )
        assert result == {"claimed": 1, "ok": 1, "failed": 0}
        assert seen["url"] == (
            "https://api.elsevier.com/content/article/doi/10.1016/j.amf.2025.200253"
        )
        assert seen["headers"]["X-ELS-APIKey"] == "SECRET"
        with store.pool.connection() as conn:
            rows = conn.execute(
                "SELECT source, event FROM ref_events WHERE ref_id = %s ORDER BY ts",
                (ref_id,),
            ).fetchall()
        assert rows == [("fetcher:elsevier", "fetch_ok")]

    def test_empty_queue_zero_counts(self, store: Store, tmp_path: Path) -> None:
        result = run_oa_fetch_pass(store, limit=10, inbox_dir=tmp_path, email="a@b")
        assert result == {"claimed": 0, "ok": 0, "failed": 0}

    def test_unhandled_provider_exception_counts_as_ok_stub(
        self,
        store: Store,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # Cascade-level contract: per-provider exceptions log
        # ``api_error`` and the cascade moves on. The stub is counted
        # in ``ok`` because the cascade itself didn't raise. Only an
        # exception that escapes _run_cascade bumps ``failed``.
        _seed_paper_stub(store, doi="10.1234/g", arxiv="2401.77777")

        def _boom(doi: str, *, email: str) -> dict[str, Any]:
            raise RuntimeError("simulated Unpaywall blowup")

        monkeypatch.setattr(fetch_oa, "_query_unpaywall", _boom)
        # arXiv works.
        monkeypatch.setattr(
            fetch_oa,
            "_download_pdf",
            lambda url, target: _write_synthetic_pdf(target, size=128),
        )
        result = run_oa_fetch_pass(store, limit=10, inbox_dir=tmp_path, email="a@b")
        assert result == {"claimed": 1, "ok": 1, "failed": 0}
