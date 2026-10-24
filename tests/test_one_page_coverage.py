"""One-page-PDF coverage flag (gr372863): paper read banner + hub evidence edge.
Page 0 is Marker's "no page number" sentinel — a ``[0,1)`` span means the
body blocks never carried a page, not that the PDF has one page (gr510135)."""

from __future__ import annotations

from typing import Any

from precis.handlers.paper import _paper_banner
from precis.store.types import ONE_PAGE_COVERAGE_NOTE
from precis.taproot.canon import CanonicalClaim
from precis.taproot.hub import attach_evidence, mint_hub

_CLAIM = CanonicalClaim(
    sentence="Pd/C catalyzes Suzuki coupling at room temperature with a mild base.",
    scope={"material": "Pd/C", "method": "Suzuki coupling", "regime": "RT"},
)


def _paper(store: Any, slug: str, pages: str | None) -> Any:
    ref = store.insert_ref(kind="paper", slug=slug, title=f"T {slug}", meta={})
    if pages is not None:
        with store.pool.connection() as conn:
            conn.execute(
                "UPDATE refs SET pdf_pages = %s::int4range WHERE ref_id = %s",
                (pages, ref.id),
            )
    return store.get_ref(kind="paper", id=slug)


def test_is_one_page_pdf(store: Any) -> None:
    assert _paper(store, "onep01a", "[1,2)").is_one_page_pdf
    assert not _paper(store, "onep02a", "[0,1)").is_one_page_pdf
    assert not _paper(store, "onep03a", "[1,13)").is_one_page_pdf
    assert not _paper(store, "onep04a", None).is_one_page_pdf


def test_banner_flags_only_one_page(store: Any) -> None:
    banner = _paper_banner(_paper(store, "onep05a", "[1,2)"))
    assert banner is not None and "ONE-PAGE SOURCE" in banner
    assert _paper_banner(_paper(store, "onep06a", "[0,1)")) is None
    assert _paper_banner(_paper(store, "onep06b", "[1,13)")) is None
    assert _paper_banner(_paper(store, "onep07a", None)) is None


def _attach_pdf(store: Any, ref_id: int, *, page_count: int) -> str:
    sha = "ab" * 32
    with store.pool.connection() as conn:
        conn.execute(
            "INSERT INTO pdfs (pdf_sha256, content_hash, page_count, "
            "size_bytes, storage_path) VALUES (%s, %s, %s, 1, '')",
            (sha, sha, page_count),
        )
        conn.execute("UPDATE refs SET pdf_sha256 = %s WHERE ref_id = %s", (sha, ref_id))
    return sha


def _add_body_chunks(store: Any, ref_id: int, n: int, *, chars: int = 40) -> None:
    with store.pool.connection() as conn:
        for i in range(n):
            conn.execute(
                "INSERT INTO chunks (ref_id, set_by, ord, chunk_kind, text) "
                "VALUES (%s, 'system', %s, 'paragraph', %s)",
                (ref_id, i, "x" * chars),
            )


def test_banner_suppressed_by_real_page_count(store: Any) -> None:
    """A one-page-numbered ref whose file physically has more pages is not
    a preview — the numbered span lied (gr514187)."""
    ref = _paper(store, "onep11a", "[1,2)")
    _attach_pdf(store, ref.id, page_count=6)
    assert _paper_banner(store.get_ref(kind="paper", id="onep11a"), store=store) is None


def test_banner_suppressed_by_substantial_body(store: Any) -> None:
    """A one-page-numbered ref carrying a real body (scroll-PDF / markup
    refetch) is not a preview either — the corpus holds the methods."""
    ref = _paper(store, "onep12a", "[1,2)")
    _attach_pdf(store, ref.id, page_count=1)
    _add_body_chunks(store, ref.id, 11, chars=900)
    assert _paper_banner(store.get_ref(kind="paper", id="onep12a"), store=store) is None


def test_banner_still_fires_for_true_preview(store: Any) -> None:
    """One numbered page + a thin body + a physically one-page file: the
    preview warning still applies."""
    ref = _paper(store, "onep13a", "[1,2)")
    _attach_pdf(store, ref.id, page_count=1)
    _add_body_chunks(store, ref.id, 3, chars=400)
    banner = _paper_banner(store.get_ref(kind="paper", id="onep13a"), store=store)
    assert banner is not None and "ONE-PAGE SOURCE" in banner


def test_preview_verdict_without_pdf_row(store: Any) -> None:
    """No pdfs row → the body-volume leg alone corroborates."""
    thin = _paper(store, "onep14a", "[1,2)")
    banner = _paper_banner(thin, store=store)
    assert banner is not None and "ONE-PAGE SOURCE" in banner
    full = _paper(store, "onep14b", "[1,2)")
    _add_body_chunks(store, full.id, 20, chars=800)
    assert _paper_banner(store.get_ref(kind="paper", id="onep14b"), store=store) is None


def _edge_meta(store: Any, hub: int, paper: Any) -> dict[str, Any]:
    attach_evidence(
        store,
        hub_ref_id=hub,
        paper_ref_id=paper.id,
        role="corroborates",
        check_retraction=False,
    )
    (link,) = [
        lk
        for lk in store.links_for(hub, direction="in", relation="corroborates")
        if lk.src_ref_id == paper.id
    ]
    return dict(link.meta or {})


def test_hub_edge_carries_flag_only_for_one_page(store: Any) -> None:
    hub = mint_hub(store, _CLAIM)
    flagged = _edge_meta(store, hub, _paper(store, "onep08a", "[1,2)"))
    assert flagged["coverage"] == "one_page"
    assert ONE_PAGE_COVERAGE_NOTE in flagged["caveats"]
    unknown = _edge_meta(store, hub, _paper(store, "onep09a", "[0,1)"))
    assert "coverage" not in unknown
    assert not unknown.get("caveats")
    normal = _edge_meta(store, hub, _paper(store, "onep10a", "[1,13)"))
    assert "coverage" not in normal
    assert not normal.get("caveats")
