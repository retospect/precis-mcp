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

from precis.store import Store
from precis.utils.safe_fetch import SsrfBlocked
from precis.workers import fetch_oa
from precis.workers.fetch_oa import (
    StubRef,
    _try_arxiv,
    _try_s2,
    _try_unpaywall,
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
    """Seed a ``kind='paper'`` ref with no PDF and the given identifiers."""
    with store.pool.connection() as conn:
        row = conn.execute(
            "INSERT INTO refs (kind, slug, set_by, title) "
            "VALUES ('paper', %s, 'system', %s) RETURNING ref_id",
            (cite_key, title),
        ).fetchone()
        assert row is not None
        ref_id = int(row[0])
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
        conn.execute(
            "INSERT INTO ref_identifiers (ref_id, id_kind, id_value, source) "
            "VALUES (%s, 'cite_key', %s, 'manual')",
            (ref_id, cite_key),
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
    return StubRef(
        ref_id=ref_id, doi=doi, arxiv=arxiv, s2_id=s2_id, cite_key=cite_key
    )


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

    def test_excludes_ref_without_identifier(self, store: Store) -> None:
        # paper with NO doi/arxiv/s2 — claim's EXISTS clause drops it.
        _seed_paper_stub(
            store, cite_key="naked2024", doi=None, arxiv=None, s2_id=None
        )
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

    def test_orders_newest_first(self, store: Store) -> None:
        first = _seed_paper_stub(store, cite_key="a2024", doi="10.1/a")
        second = _seed_paper_stub(store, cite_key="b2024", doi="10.1/b")
        with store.pool.connection() as conn:
            stubs = claim_stubs_to_fetch(conn, limit=10)
            conn.commit()
        # ORDER BY r.ref_id DESC — newest first.
        assert [s.ref_id for s in stubs] == [second, first]


# ---------------------------------------------------------------------------
# _try_unpaywall — per-leg outcomes
# ---------------------------------------------------------------------------


class TestTryUnpaywall:
    def test_returns_none_when_no_doi(self, tmp_path: Path) -> None:
        out = _try_unpaywall(
            _stub(doi=None), inbox_dir=tmp_path, email="a@b"
        )
        assert out is None

    def test_invalid_doi_shape(self, tmp_path: Path) -> None:
        out = _try_unpaywall(
            _stub(doi="not-a-doi"), inbox_dir=tmp_path, email="a@b"
        )
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
        out = _try_unpaywall(
            _stub(doi="10.1234/x"), inbox_dir=tmp_path, email="a@b"
        )
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
        out = _try_unpaywall(
            _stub(doi="10.1234/x"), inbox_dir=tmp_path, email="a@b"
        )
        assert out is not None
        assert out.event == "rate_limited"
        assert out.payload["retry_after"] == "60"

    def test_api_error_on_unexpected_exception(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def _boom(doi: str, *, email: str) -> dict[str, Any]:
            raise RuntimeError("connection reset")

        monkeypatch.setattr(fetch_oa, "_query_unpaywall", _boom)
        out = _try_unpaywall(
            _stub(doi="10.1234/x"), inbox_dir=tmp_path, email="a@b"
        )
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
        out = _try_unpaywall(
            _stub(doi="10.1234/x"), inbox_dir=tmp_path, email="a@b"
        )
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
        out = _try_unpaywall(
            _stub(doi="10.1234/x"), inbox_dir=tmp_path, email="a@b"
        )
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
        out = _try_s2(
            _stub(doi="10.1234/x", arxiv="2401.12345"), inbox_dir=tmp_path
        )
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

    def test_fetch_ok(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
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
# run_oa_fetch_pass — cascade orchestration
# ---------------------------------------------------------------------------


class TestRunCascade:
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

        result = run_oa_fetch_pass(
            store, limit=10, inbox_dir=tmp_path, email="a@b"
        )
        assert result == {"claimed": 1, "ok": 1, "failed": 0}

    def test_falls_through_to_arxiv(
        self,
        store: Store,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _seed_paper_stub(
            store, doi="10.1234/e", arxiv="2401.99999"
        )
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

        result = run_oa_fetch_pass(
            store, limit=10, inbox_dir=tmp_path, email="a@b"
        )
        assert result == {"claimed": 1, "ok": 1, "failed": 0}
        # arXiv URL was downloaded (not the Unpaywall one).
        assert seen_urls == ["https://arxiv.org/pdf/2401.99999.pdf"]

    def test_records_every_attempted_provider(
        self,
        store: Store,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        ref_id = _seed_paper_stub(
            store, doi="10.1234/f", arxiv="2401.88888"
        )
        # Unpaywall: no_oa_version. arXiv: fetch_ok.
        monkeypatch.setattr(
            fetch_oa,
            "_query_unpaywall",
            lambda doi, *, email: {"best_oa_location": None},
        )
        monkeypatch.setattr(
            fetch_oa,
            "_download_pdf",
            lambda url, target: _write_synthetic_pdf(target, size=128),
        )

        run_oa_fetch_pass(store, limit=10, inbox_dir=tmp_path, email="a@b")

        with store.pool.connection() as conn:
            rows = conn.execute(
                "SELECT source, event FROM ref_events "
                "WHERE ref_id = %s ORDER BY ts",
                (ref_id,),
            ).fetchall()
        # Both attempts recorded; no S2 entry because cascade stopped
        # at arXiv's fetch_ok.
        assert rows == [
            ("fetcher:unpaywall", "no_oa_version"),
            ("fetcher:arxiv", "fetch_ok"),
        ]

    def test_empty_queue_zero_counts(
        self, store: Store, tmp_path: Path
    ) -> None:
        result = run_oa_fetch_pass(
            store, limit=10, inbox_dir=tmp_path, email="a@b"
        )
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
        result = run_oa_fetch_pass(
            store, limit=10, inbox_dir=tmp_path, email="a@b"
        )
        assert result == {"claimed": 1, "ok": 1, "failed": 0}
