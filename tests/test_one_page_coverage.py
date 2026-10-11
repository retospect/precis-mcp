"""One-page-PDF coverage flag (gr372863): paper read banner + hub evidence edge."""

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
    assert _paper(store, "onep01a", "[0,1)").is_one_page_pdf
    assert _paper(store, "onep02a", "[1,1]").is_one_page_pdf
    assert not _paper(store, "onep03a", "[1,13)").is_one_page_pdf
    assert not _paper(store, "onep04a", None).is_one_page_pdf


def test_banner_flags_only_one_page(store: Any) -> None:
    banner = _paper_banner(_paper(store, "onep05a", "[0,1)"))
    assert banner is not None and "ONE-PAGE SOURCE" in banner
    assert _paper_banner(_paper(store, "onep06a", "[1,13)")) is None
    assert _paper_banner(_paper(store, "onep07a", None)) is None


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
    flagged = _edge_meta(store, hub, _paper(store, "onep08a", "[0,1)"))
    assert flagged["coverage"] == "one_page"
    assert ONE_PAGE_COVERAGE_NOTE in flagged["caveats"]
    normal = _edge_meta(store, hub, _paper(store, "onep09a", "[1,13)"))
    assert "coverage" not in normal
    assert not normal.get("caveats")
