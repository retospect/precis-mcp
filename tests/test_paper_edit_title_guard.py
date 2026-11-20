"""Paper identity edits must inspect the article, not an issue-page prefix."""

from __future__ import annotations

import pytest

from precis.dispatch import Hub
from precis.handlers.paper import PaperHandler
from precis.store.types import ChunkInsert, Tag


def _seed(hub: Hub, texts: list[str], *, start: int = 0) -> int:
    ref = hub.live_store.insert_ref(
        kind="paper",
        slug="identity-guard",
        title="Original paper title",
        meta={"article_start_ord": start},
    )
    hub.live_store.chunks.insert_chunks(
        ref.id, [ChunkInsert(ord=i, text=text) for i, text in enumerate(texts)]
    )
    return ref.id


def test_unsupported_title_warns(hub: Hub) -> None:
    rid = _seed(hub, ["Quantum magnetic spin dynamics"] * 8)
    title = "Ecological population adaptation"
    response = PaperHandler(hub=hub).edit(id=rid, title=title)
    assert "title not supported by the article body (score 0.00" in response.body
    assert "missing: ecological, population, adaptation" in response.body
    assert "check the title block, not chunk 0" in response.body
    stored = hub.live_store.get_ref(kind="paper", id=rid)
    assert stored is not None and stored.title == title


def test_supported_title_silent(hub: Hub) -> None:
    rid = _seed(hub, ["Quantum magnetic spin dynamics"] * 8)
    response = PaperHandler(hub=hub).edit(
        id=rid, title="Quantum magnetic spin dynamics"
    )
    assert "warning:" not in response.body


def test_multi_article_paper_warns_split(hub: Hub) -> None:
    rid = _seed(hub, [])
    hub.live_store.add_tag(rid, Tag.open("multi-article"), set_by="agent")
    response = PaperHandler(hub=hub).edit(id=rid, title="A corrected title")
    assert "tagged multi-article: no single title exists - split it" in response.body
    assert "precis-paper-ref-help" in response.body
    stored = hub.live_store.get_ref(kind="paper", id=rid)
    assert stored is not None and stored.title == "A corrected title"


@pytest.mark.parametrize("in_body", [False, True])
def test_doi_body_support(
    hub: Hub, monkeypatch: pytest.MonkeyPatch, in_body: bool
) -> None:
    # Keep the existing registry advisory offline; this check uses body evidence.
    monkeypatch.setattr(PaperHandler, "_doi_edit_warning", lambda *a, **k: "")
    texts = ["Quantum magnetic spin dynamics"] * 8
    texts[7] += " 10.1234/ARTICLE" if in_body else ""
    rid = _seed(hub, texts)
    response = PaperHandler(hub=hub).edit(id=rid, doi="10.1234/article")
    assert (
        "doi not found in the body outside the leading chunks" in response.body
    ) is not in_body
    assert hub.live_store.identifiers_for_refs([rid])[rid]["doi"] == "10.1234/article"


def test_foreign_prefix_cannot_support_identity(
    hub: Hub, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(PaperHandler, "_doi_edit_warning", lambda *a, **k: "")
    rid = _seed(
        hub,
        ["Ecological population adaptation 10.1234/foreign"] * 3
        + ["Quantum magnetic spin dynamics"] * 8,
        start=3,
    )
    handler = PaperHandler(hub=hub)
    wrong = handler.edit(
        id=rid, title="Ecological population adaptation", doi="10.1234/foreign"
    )
    assert "title not supported" in wrong.body
    assert "doi not found" in wrong.body
    right = handler.edit(id=rid, title="Quantum magnetic spin dynamics")
    assert "warning:" not in right.body


def test_title_window_is_six_article_chunks(hub: Hub) -> None:
    rid = _seed(
        hub,
        ["Quantum magnetic spin dynamics"] * 6 + ["Ecological population adaptation"],
    )
    response = PaperHandler(hub=hub).edit(
        id=rid, title="Ecological population adaptation"
    )
    assert "title not supported" in response.body


def test_structural_boundary_excludes_foreign_references(hub: Hub) -> None:
    rid = _seed(hub, [])
    # References metadata is the extraction signal used by foreign_tail_ord.
    # Seed it through the same append-only chunk API as ingest.
    hub.live_store.chunks.insert_chunks(
        rid,
        [
            ChunkInsert(
                ord=0,
                text="- 11. Foreign, A. Ecological population adaptation. Nature 400, 1 (1999).",
                meta={"chunk_kind": "references"},
            ),
            ChunkInsert(ord=1, text="Acknowledgements. We thank the foreign funder."),
            ChunkInsert(
                ord=2,
                text="Quantum magnetic spin dynamics\nM. Buess & G. Aeppli\n"
                "The diode pumps a magnetic field through the junction.",
            ),
        ],
    )
    response = PaperHandler(hub=hub).edit(
        id=rid, title="Ecological population adaptation"
    )
    assert "title not supported" in response.body
