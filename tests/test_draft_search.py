"""Draft search — lexical / semantic over draft chunks, cross-draft and
within-draft (slug scope or ¶handle subtree), plus headings-only."""

from __future__ import annotations

import pytest

from precis.dispatch import Hub
from precis.embedder import MockEmbedder
from precis.errors import NotFound
from precis.handlers.draft import DraftHandler
from precis.handlers.todo import TodoHandler
from precis.store import Store


def _proj(hub: Hub, text: str = "Project root") -> int:
    t = TodoHandler(hub=hub).put(text=text, tags=["level:strategic"])
    return int(t.body.split("id=")[1].split()[0].rstrip(",.()"))


def _handle_of(put_body: str) -> str:
    # ADR 0036: the put-ack carries the chunk's universal handle ``dc<id>``.
    import re

    m = re.search(r"dc\d+", put_body)
    assert m is not None, f"no dc handle in {put_body!r}"
    return m.group(0)


@pytest.fixture
def draft(hub: Hub) -> DraftHandler:
    return DraftHandler(hub=hub)


def _seed_draft(draft: DraftHandler, hub: Hub, *, slug: str) -> dict[str, str]:
    """A small draft with a heading + two paragraphs; returns handles."""
    proj = _proj(hub, f"proj for {slug}")
    draft.put(id=slug, title="CO2 capture", project=proj)
    sec = draft.put(
        id=slug, chunk_kind="heading", text="Capture methods", at={"last": True}
    )
    sec_h = _handle_of(sec.body)
    p1 = draft.put(
        id=slug,
        chunk_kind="paragraph",
        text="Amine functionalization improves carbon dioxide uptake.",
        at={"into": sec_h, "last": True},
    )
    p2 = draft.put(
        id=slug,
        chunk_kind="paragraph",
        text="Pore defect engineering tunes selectivity in frameworks.",
        at={"last": True},
    )
    return {"sec": sec_h, "p1": _handle_of(p1.body), "p2": _handle_of(p2.body)}


def _embed_draft(store: Store, slug: str) -> None:
    """Stand in for the embed worker: vectorise every chunk of the draft
    with the test MockEmbedder under the default embedder name, so the
    semantic leg has vectors to rank."""
    dim = store.embedding_dim()
    e = MockEmbedder(dim=dim)
    with store.pool.connection() as conn:
        name = conn.execute(
            "SELECT name FROM embedders WHERE is_default = TRUE LIMIT 1"
        ).fetchone()[0]
        rows = conn.execute(
            """SELECT c.chunk_id, c.text, c.content_sha
                 FROM chunks c
                 JOIN ref_identifiers ri
                   ON ri.ref_id = c.ref_id AND ri.id_kind = 'cite_key'
                WHERE ri.id_value = %s AND c.retired_at IS NULL""",
            (slug,),
        ).fetchall()
        for cid, text, sha in rows:
            conn.execute(
                "INSERT INTO chunk_embeddings "
                "(chunk_id, embedder, vector, status, content_sha) "
                "VALUES (%s, %s, %s, 'ok', %s) ON CONFLICT DO NOTHING",
                (cid, name, e.embed_one(text), sha),
            )
        conn.commit()


def test_search_supported_now(draft: DraftHandler) -> None:
    assert DraftHandler.spec.supports_search is True


def test_lexical_finds_keyword(draft: DraftHandler, hub: Hub) -> None:
    h = _seed_draft(draft, hub, slug="d1")
    out = draft.search(q="amine", mode="lexical").body
    assert f"{h['p1']}" in out
    assert f"{h['p2']}" not in out  # 'amine' not in p2


def test_cross_draft_search(draft: DraftHandler, hub: Hub) -> None:
    _seed_draft(draft, hub, slug="da")
    _seed_draft(draft, hub, slug="db")
    out = draft.search(q="defect engineering", mode="lexical").body
    # both drafts have the same paragraph → both surface
    assert "draft:da" in out and "draft:db" in out


def test_scope_to_one_draft(draft: DraftHandler, hub: Hub) -> None:
    _seed_draft(draft, hub, slug="da")
    _seed_draft(draft, hub, slug="db")
    out = draft.search(q="defect engineering", mode="lexical", scope="da").body
    assert "draft:da" in out
    assert "draft:db" not in out


def test_subtree_scope_via_handle(draft: DraftHandler, hub: Hub) -> None:
    h = _seed_draft(draft, hub, slug="d1")
    # p1 is inside the 'Capture methods' heading subtree; p2 is a sibling
    # at top level. Scoping to the heading must exclude p2.
    out = draft.search(
        q="carbon dioxide uptake", mode="lexical", scope=f"{h['sec']}"
    ).body
    assert f"{h['p1']}" in out
    assert f"{h['p2']}" not in out


def test_id_handle_is_scope_alias(draft: DraftHandler, hub: Hub) -> None:
    h = _seed_draft(draft, hub, slug="d1")
    out = draft.search(id=f"{h['sec']}", q="amine uptake", mode="lexical").body
    assert f"{h['p1']}" in out
    assert f"{h['p2']}" not in out


def test_headings_only(draft: DraftHandler, hub: Hub) -> None:
    h = _seed_draft(draft, hub, slug="d1")
    out = draft.search(q="capture methods", mode="lexical", headings_only=True).body
    assert f"{h['sec']}" in out  # the heading
    assert f"{h['p1']}" not in out  # body paragraphs excluded


def test_semantic_mode_runs(draft: DraftHandler, hub: Hub) -> None:
    # hub fixture carries a MockEmbedder; embed the draft so the semantic
    # leg has vectors (put() doesn't embed — ADR 0007).
    assert isinstance(hub.embedder, MockEmbedder)
    _seed_draft(draft, hub, slug="d1")
    _embed_draft(hub.store, "d1")
    out = draft.search(q="carbon dioxide uptake", mode="semantic").body
    assert "draft:d1" in out  # returns ranked matches over embedded chunks


def test_unknown_subtree_handle_raises(draft: DraftHandler, hub: Hub) -> None:
    _seed_draft(draft, hub, slug="d1")
    with pytest.raises(NotFound):
        draft.search(q="anything", scope="¶ZZZZZZ")


def test_empty_query_rejected(draft: DraftHandler) -> None:
    from precis.errors import BadInput

    with pytest.raises(BadInput, match="requires q="):
        draft.search(q="")
