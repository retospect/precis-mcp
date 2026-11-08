"""``?chunk=N`` deep links: pres reader Jump tab + the ``/r/`` resolver's
per-kind landing (gr593143 — a draft export's ``[pr<id>~N]`` link must
land on the slide, not the record overview)."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from precis.dispatch import Hub
from precis.handlers.presentation import PresentationHandler
from precis.runtime import PrecisRuntime
from precis.store import Store
from precis.utils.handle_registry import format_handle
from precis_web.app import create_app
from precis_web.config import WebConfig


def _pres_ref(store: Store, slug: str) -> Any:
    PresentationHandler(hub=Hub(store=store)).put(
        id=slug, title="Deck", text="Slide body"
    )
    ref = store.get_ref(kind="pres", id=slug)
    assert ref is not None
    return ref


def test_pres_detail_chunk_deep_link(client: TestClient) -> None:
    """``/pres/<slug>?chunk=124`` opens the reader on slide 124 (the
    paperDoc ``citedOrd`` argument the Jump tab jumps to)."""
    response = client.get("/pres/2001-lecture01?chunk=124")

    assert response.status_code == 200
    assert re.search(r"paperDoc\(\s*60\s*,\s*124\s*,", response.text)


def test_pres_detail_without_chunk_keeps_meta_tab(client: TestClient) -> None:
    response = client.get("/pres/2001-lecture01")

    assert response.status_code == 200
    assert re.search(r"paperDoc\(\s*60\s*,\s*-1\s*,", response.text)


@pytest.fixture()
def web_client(
    runtime_with_store: PrecisRuntime, store: Store, tmp_path: Path
) -> TestClient:
    app = create_app(
        runtime=runtime_with_store, web_config=WebConfig(corpus_dir=tmp_path)
    )
    return TestClient(app)


def test_r_resolver_pres_chunk_redirects_to_deck(
    web_client: TestClient, store: Store
) -> None:
    """``/r/pres/<id>?chunk=124`` → ``/pres/<id>?chunk=124``."""
    ref = _pres_ref(store, "sel-deck-1")
    response = web_client.get(f"/r/pres/{ref.id}?chunk=124", follow_redirects=False)

    assert response.status_code == 303
    assert response.headers["location"] == f"/pres/{ref.id}?chunk=124"


def test_r_resolver_pres_record_still_lands_on_deck(
    web_client: TestClient, store: Store
) -> None:
    ref = _pres_ref(store, "sel-deck-2")
    response = web_client.get(f"/r/pres/{ref.id}", follow_redirects=False)

    assert response.status_code == 303
    assert response.headers["location"] == f"/pres/{ref.id}"


def test_r_resolver_draft_chunk_focuses_the_dc(
    web_client: TestClient, store: Store
) -> None:
    """``/r/draft/<id>?chunk=0`` → ``/smartdraft/<slug>?focus=dc<chunk>``."""
    ref, _ = store.drafts.create_draft(name="sel-report", title="Report")
    chunk = store.drafts.add_chunks(
        ref_id=ref.id,
        chunk_kind="paragraph",
        text="body",
        at={"last": True},
    )[0]
    ord_pos = next(
        b.ord
        for b in store.chunks.list_chunks_for_ref(ref.id)
        if b.id == chunk.chunk_id
    )
    dc = format_handle("draft", chunk.chunk_id, chunk=True)
    response = web_client.get(
        f"/r/draft/{ref.id}?chunk={ord_pos}", follow_redirects=False
    )

    assert response.status_code == 303
    assert response.headers["location"].endswith(f"?focus={dc}")


def _markdown_ref(store: Store, slug: str) -> Any:
    from precis.store.types import ChunkInsert

    ref = store.insert_ref(kind="markdown", slug=slug, title="Notes")
    store.chunks.insert_chunks(
        ref.id,
        [
            ChunkInsert(ord=0, text="first block"),
            ChunkInsert(ord=3, text="the cited passage"),
        ],
    )
    return ref


def test_r_resolver_markdown_chunk_redirects_to_detail(
    web_client: TestClient, store: Store
) -> None:
    """``/r/markdown/<id>?chunk=3`` → ``/refs/markdown/<id>?chunk=3`` — a
    ``[md<id>~3]`` export link lands on the cited chunk, not the record
    overview."""
    ref = _markdown_ref(store, "sel-md-1")
    response = web_client.get(f"/r/markdown/{ref.id}?chunk=3", follow_redirects=False)

    assert response.status_code == 303
    assert response.headers["location"] == f"/refs/markdown/{ref.id}?chunk=3"


def test_markdown_detail_chunk_surfaces_cited_passage(
    web_client: TestClient, store: Store
) -> None:
    """``/refs/markdown/<id>?chunk=3`` renders the cited chunk as a card."""
    ref = _markdown_ref(store, "sel-md-2")
    response = web_client.get(f"/refs/markdown/{ref.id}?chunk=3")

    assert response.status_code == 200
    assert "cited ~3" in response.text
    assert "the cited passage" in response.text
