"""Slug routing + sidebar-nav endpoints on the paper detail page."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

pytest.importorskip("fastapi")


def _block(pos: int, text: str, keywords: list[str]) -> SimpleNamespace:
    """A minimal stand-in for store.types.Block (only the fields the
    nav route reads: pos / text / keywords)."""
    return SimpleNamespace(pos=pos, text=text, keywords=keywords)


# ── slug routing ────────────────────────────────────────────────────


def test_detail_numeric_id_redirects_to_slug(client) -> None:
    """A numeric /papers/<id> 301-redirects to the canonical slug URL."""
    resp = client.get("/papers/10", follow_redirects=False)
    assert resp.status_code == 301
    assert resp.headers["location"] == "/papers/smith2024"


def test_detail_numeric_redirect_preserves_query(client) -> None:
    resp = client.get("/papers/10?chunk=3", follow_redirects=False)
    assert resp.status_code == 301
    assert resp.headers["location"] == "/papers/smith2024?chunk=3"


def test_detail_by_slug_renders(client) -> None:
    """The slug URL resolves straight through (no redirect) and renders
    the sidebar-nav reader."""
    resp = client.get("/papers/smith2024", follow_redirects=False)
    assert resp.status_code == 200
    assert "smith2024" in resp.text  # cite_key still shown
    assert "pa10" in resp.text  # universal handle (pa<ref_id>)
    assert "/static/paper-viewer.js" in resp.text


def test_detail_wires_pdfjs_viewer_when_pdf_on_disk(client, tmp_path) -> None:
    """With the PDF present on disk the main pane is the vendored pdf.js
    viewer iframe, pointed at the numeric-id pdf route."""
    pdf = tmp_path / "s" / "smith2024.pdf"
    pdf.parent.mkdir(parents=True)
    pdf.write_bytes(b"%PDF-1.4 fake")
    resp = client.get("/papers/smith2024")
    assert resp.status_code == 200
    assert "/static/pdfjs/web/viewer.html?file=/papers/10/pdf" in resp.text


def test_detail_finds_pdf_filed_under_nondisplay_alias(client, tmp_path) -> None:
    """A paper whose PDF is filed under a *non-display* cite_key alias still
    resolves. Paper 11's display slug is ``jones2025`` but the fake gives it a
    second alias ``jonesalt25``; the file lives at ``j/jonesalt25.pdf`` (the
    tex-import / fetcher-picked-a-different-key case). The resolver must try
    every alias, not just ``ref.slug``."""
    pdf = tmp_path / "j" / "jonesalt25.pdf"
    pdf.parent.mkdir(parents=True)
    pdf.write_bytes(b"%PDF-1.4 fake")
    # Detail page reports the PDF as present (viewer wired to the id route).
    resp = client.get("/papers/jones2025")
    assert resp.status_code == 200
    assert "/papers/11/pdf" in resp.text
    # And the pdf endpoint streams those bytes rather than 404ing.
    pdf_resp = client.get("/papers/11/pdf")
    assert pdf_resp.status_code == 200
    assert pdf_resp.content == b"%PDF-1.4 fake"


def test_detail_unknown_slug_404s(client) -> None:
    resp = client.get("/papers/nope9999", follow_redirects=False)
    assert resp.status_code == 400  # NotFound -> PrecisError handler


# ── search endpoint ─────────────────────────────────────────────────


def test_search_keyword_shapes_results(client, runtime) -> None:
    runtime.store.nav_hits[10] = [
        (
            _block(5, "Ballistic transport in nanotubes", ["nanotube", "ballistic"]),
            None,
            0.42,
        ),
    ]
    resp = client.get("/papers/10/search", params={"q": "nanotube", "mode": "keyword"})
    assert resp.status_code == 200
    data = resp.json()
    assert data["mode"] == "keyword"
    assert data["results"][0]["ord"] == 5
    assert data["results"][0]["keywords"] == ["nanotube", "ballistic"]
    assert "Ballistic transport" in data["results"][0]["text"]
    # No page provenance in the fake corpus -> page hint is null, not fatal.
    assert data["results"][0]["page"] is None


def test_search_semantic_degrades_to_keyword_without_embedder(client, runtime) -> None:
    """No embedder on the runtime -> semantic falls back to lexical and
    the response reflects the degrade so the client relabels."""
    runtime.store.nav_hits[10] = [(_block(1, "alpha beta", ["alpha"]), None, 0.1)]
    resp = client.get("/papers/10/search", params={"q": "alpha", "mode": "semantic"})
    assert resp.status_code == 200
    assert resp.json()["mode"] == "keyword"
    assert resp.json()["results"][0]["ord"] == 1


def test_search_semantic_reports_similarity_best_first(client, runtime) -> None:
    """With an embedder wired the semantic path stays semantic and reports
    cosine *similarity* (1 - distance), in the store's best-first order."""
    runtime.hub = SimpleNamespace(embedder=SimpleNamespace(embed_one=lambda q: [0.1]))
    runtime.store.nav_hits[10] = [
        (_block(7, "closest chunk", ["k"]), None, 0.2),  # distance 0.2 -> sim 0.8
        (_block(9, "further chunk", ["k"]), None, 0.5),  # distance 0.5 -> sim 0.5
    ]
    resp = client.get("/papers/10/search", params={"q": "x", "mode": "semantic"})
    assert resp.status_code == 200
    data = resp.json()
    assert data["mode"] == "semantic"
    assert [r["ord"] for r in data["results"]] == [7, 9]  # best-first preserved
    assert data["results"][0]["score"] == 0.8  # similarity, higher = better
    assert data["results"][0]["score"] > data["results"][1]["score"]


def test_search_empty_query_returns_empty(client) -> None:
    resp = client.get("/papers/10/search", params={"q": "  ", "mode": "keyword"})
    assert resp.status_code == 200
    assert resp.json()["results"] == []


# ── toc + chunk endpoints ───────────────────────────────────────────


def test_toc_endpoint_returns_segments_key(client) -> None:
    # Fake corpus has no paper body chunks -> empty outline, but the
    # contract (a `segments` list) holds.
    resp = client.get("/papers/10/toc")
    assert resp.status_code == 200
    assert resp.json()["segments"] == []


def test_chunk_endpoint_returns_chunk_key(client) -> None:
    resp = client.get("/papers/10/chunk/3")
    assert resp.status_code == 200
    assert "chunk" in resp.json()
