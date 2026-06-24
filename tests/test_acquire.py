"""PaperHandler.acquire — the gated dream stub-mint tool (#9).

Mirrors the supersede test shape: handler-level invocation (MCP wiring
is deferred to the agent loop). S2 enrichment is patched out so every
test stays offline; the identifier parser is unit-tested standalone.
"""

from __future__ import annotations

import pytest

from precis.dispatch import Hub
from precis.errors import BadInput
from precis.handlers import paper as paper_mod
from precis.handlers.paper import PaperHandler, _parse_acquire_identifier
from precis.store import Store
from tests.conftest import record_handle


@pytest.fixture
def handler(hub: Hub) -> PaperHandler:
    return PaperHandler(hub=hub)


@pytest.fixture(autouse=True)
def _offline(monkeypatch: pytest.MonkeyPatch) -> None:
    """Default: a placeholder S2 lookup that satisfies the verify=True
    validator without populating any stub fields. Individual tests
    override to return real metadata, or ``None`` to exercise the
    hallucinated-identifier rejection path.
    """
    monkeypatch.setattr(
        paper_mod, "_lookup_acquire_metadata", lambda *_: {"_resolved": True}
    )


def _ref_id(body: str) -> int:
    return int(body.split("id=", 1)[1].split()[0])


def _identifiers(store: Store, ref_id: int) -> set[tuple[str, str]]:
    with store.pool.connection() as conn:
        rows = conn.execute(
            "SELECT id_kind, id_value FROM ref_identifiers WHERE ref_id = %s",
            (ref_id,),
        ).fetchall()
    return {(str(k), str(v)) for k, v in rows}


def _tags(store: Store, ref_id: int) -> set[str]:
    return {str(t) for t in store.tags_for(ref_id)}


# ── identifier parser (pure) ────────────────────────────────────────


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("doi:10.1/X", ("doi", "10.1/x")),
        ("arxiv:2401.00001", ("arxiv", "2401.00001")),
        ("s2:abc123", ("s2", "abc123")),
        ("pubmed:99", ("pubmed", "99")),
        ("10.1038/nature10352", ("doi", "10.1038/nature10352")),
        ("2401.00001", ("arxiv", "2401.00001")),
        ("2401.00001v2", ("arxiv", "2401.00001v2")),
        ("not an id", None),
        ("", None),
        ("doi:", None),
    ],
)
def test_parse_acquire_identifier(raw: str, expected: tuple[str, str] | None) -> None:
    assert _parse_acquire_identifier(raw) == expected


# ── guards ──────────────────────────────────────────────────────────


def test_acquire_requires_identifier_or_title(handler: PaperHandler) -> None:
    with pytest.raises(BadInput, match="identifier= .* or title="):
        handler.acquire()


def test_acquire_rejects_unrecognised_identifier(handler: PaperHandler) -> None:
    with pytest.raises(BadInput, match="unrecognised identifier"):
        handler.acquire(identifier="gibberish")


def test_acquire_rejects_dead_context_ref(handler: PaperHandler) -> None:
    with pytest.raises(BadInput, match="not a live ref"):
        handler.acquire(identifier="doi:10.1/x", context_ref_id=999999)


def test_acquire_rejects_non_int_context_ref(handler: PaperHandler) -> None:
    with pytest.raises(BadInput, match="must be an int"):
        handler.acquire(identifier="doi:10.1/x", context_ref_id="abc")


# ── minting + idempotency ───────────────────────────────────────────


def test_acquire_mints_tagged_stub(handler: PaperHandler, store: Store) -> None:
    r = handler.acquire(identifier="doi:10.1/x", reason="cited a lot")
    assert "minted stub" in r.body
    rid = _ref_id(r.body)
    assert ("doi", "10.1/x") in _identifiers(store, rid)
    assert "DREAM:acquire" in _tags(store, rid)
    # it's a stub: a live paper ref with no pdf
    assert store.get_ref(kind="paper", id=rid) is not None


def test_acquire_is_idempotent(handler: PaperHandler, store: Store) -> None:
    first = _ref_id(handler.acquire(identifier="doi:10.1/x").body)
    second = handler.acquire(identifier="doi:10.1/X")  # case-insensitive
    assert "already tracked" in second.body
    assert _ref_id(second.body) == first
    # A collapse hit returns the existing paper, not just "already tracked":
    # the slug handle + a get hint so the caller can read it directly.
    second_ref = store.get_ref(kind="paper", id=first)
    assert second_ref is not None and second_ref.slug is not None
    assert record_handle(store, second_ref.slug) in second.body
    assert "get(id=" in second.body


def test_acquire_title_only_mints_backlog_stub(
    handler: PaperHandler, store: Store
) -> None:
    r = handler.acquire(title="Some Uncited Paper We Want")
    assert "minted stub" in r.body
    rid = _ref_id(r.body)
    assert "DREAM:acquire" in _tags(store, rid)
    # no external id → fetch_oa can't auto-grab; it's a pure backlog stub
    assert _identifiers(store, rid) == {
        ("cite_key", store.get_ref(kind="paper", id=rid).slug)
    }


def test_acquire_links_from_context(handler: PaperHandler, store: Store) -> None:
    ctx = store.insert_ref(kind="memory", slug=None, title="where it came up")
    r = handler.acquire(
        identifier="arxiv:2401.00001", context_ref_id=ctx.id, reason="rev"
    )
    rid = _ref_id(r.body)
    out = store.links_for(ctx.id, direction="out", relation="related-to")
    assert any(link.dst_ref_id == rid for link in out)
    assert f"ref {ctx.id}" in r.body


def test_acquire_does_not_retag_existing_paper(
    handler: PaperHandler, store: Store
) -> None:
    # A paper the corpus already holds, registered under a DOI.
    existing = store.insert_ref(kind="paper", slug="held2020", title="Held Paper")
    with store.pool.connection() as conn:
        conn.execute(
            "INSERT INTO ref_identifiers (id_kind, id_value, ref_id, source) "
            "VALUES ('doi', '10.9/held', %s, 'test')",
            (existing.id,),
        )
        conn.commit()
    r = handler.acquire(identifier="doi:10.9/held")
    assert "already tracked" in r.body
    assert _ref_id(r.body) == existing.id
    # the response returns the existing paper: its handle + a get hint
    assert record_handle(store, "held2020") in r.body
    assert "get(id=" in r.body
    # never slap DREAM:acquire onto an already-held paper
    assert "DREAM:acquire" not in _tags(store, existing.id)


# ── verify=True (default): reject hallucinated identifiers ─────────


def test_acquire_rejects_unresolved_identifier_by_default(
    handler: PaperHandler, monkeypatch: pytest.MonkeyPatch
) -> None:
    """verify=True (default) + S2 returns None + no title → BadInput."""
    monkeypatch.setattr(paper_mod, "_lookup_acquire_metadata", lambda *_: None)
    with pytest.raises(BadInput, match="did not resolve"):
        handler.acquire(identifier="doi:10.fake/hallucination")


def test_acquire_verify_false_skips_validation(
    handler: PaperHandler, store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """verify=False mints even when S2 returns nothing (real-but-unindexed)."""
    monkeypatch.setattr(paper_mod, "_lookup_acquire_metadata", lambda *_: None)
    r = handler.acquire(identifier="doi:10.brandnew/preprint", verify=False)
    assert "minted stub" in r.body
    rid = _ref_id(r.body)
    assert ("doi", "10.brandnew/preprint") in _identifiers(store, rid)


def test_acquire_title_hint_with_unresolved_id_marks_unverified(
    handler: PaperHandler, store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An unresolved identifier + title=hint mints with acquire:unverified tag."""
    monkeypatch.setattr(paper_mod, "_lookup_acquire_metadata", lambda *_: None)
    r = handler.acquire(identifier="doi:10.niche/venue", title="My niche preprint")
    assert "minted stub" in r.body
    rid = _ref_id(r.body)
    assert "acquire:unverified" in _tags(store, rid)


# ── enrichment ──────────────────────────────────────────────────────


def test_acquire_enriches_from_s2(
    handler: PaperHandler, store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        paper_mod,
        "_lookup_acquire_metadata",
        lambda *_: {
            "title": "Resolved Title",
            "year": 2021,
            "doi": "10.1/X",
            "arxiv_id": "2401.99999",
            "s2_id": "S2DEADBEEF",
        },
    )
    r = handler.acquire(identifier="doi:10.1/x")
    rid = _ref_id(r.body)
    got = store.get_ref(kind="paper", id=rid)
    assert got is not None
    assert got.title == "Resolved Title"
    assert got.year == 2021
    ids = _identifiers(store, rid)
    assert ("doi", "10.1/x") in ids
    assert ("arxiv", "2401.99999") in ids
    assert ("s2", "S2DEADBEEF") in ids
