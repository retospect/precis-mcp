"""gr560965: paper → pres re-extracts pages and moves identity atomically.

These are real-Postgres companions to the conversion door. PDFs are generated
from short synthetic labels, including a blank page, so the tests exercise
the production extractor rather than a mocked slide list.
"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from precis.dispatch import Hub
from precis.errors import BadInput, Upstream
from precis.handlers.paper import PaperHandler
from precis.identity import make_pdf_sha256
from precis.store import Store, Tag
from precis.store.revision_context import revision_context


@dataclass(frozen=True)
class Deck:
    ref_id: int
    path: Path
    sha: str
    slug: str = "misfiled-deck"


def _pdf(path: Path) -> None:
    import fitz

    with fitz.open() as doc:
        for label in ("Alpha heading", "", "Omega heading"):
            page = doc.new_page()
            if label:
                page.insert_text((72, 72), label)
        doc.save(str(path))


@pytest.fixture
def deck(store: Store, tmp_path: Path) -> Deck:
    path = tmp_path / "misfiled-deck.pdf"
    _pdf(path)
    sha = make_pdf_sha256(path.read_bytes())
    ref = store.insert_ref(
        kind="paper",
        slug="misfiled-deck",
        title="Synthetic slide deck",
        provider="manual",
        meta={},
    )
    with store.pool.connection() as conn:
        conn.execute(
            "INSERT INTO pdfs (pdf_sha256, content_hash, page_count, "
            "size_bytes, storage_path) VALUES (%s, %s, 3, %s, %s)",
            (sha, sha, path.stat().st_size, str(path)),
        )
        conn.execute("UPDATE refs SET pdf_sha256 = %s WHERE ref_id = %s", (sha, ref.id))
        for pos, page in ((0, 0), (1, 2)):
            conn.execute(
                "INSERT INTO chunks (ref_id, ord, chunk_kind, text, "
                "page_first, page_last, set_by) "
                "VALUES (%s, %s, 'paragraph', %s, %s, %s, 'system')",
                (ref.id, pos, f"Old extraction block {pos}", page, page),
            )
    store.set_ref_identifier(ref.id, "pdf_sha256", sha, source="test")
    store.set_ref_identifier(ref.id, "doi", "10.1/synthetic-deck", source="test")
    return Deck(ref.id, path, sha)


@pytest.fixture
def handler(hub: Hub) -> PaperHandler:
    return PaperHandler(hub=hub)


def _convert(handler: PaperHandler, deck: Deck, **kw: Any) -> Any:
    return handler.edit(
        id=f"pa{deck.ref_id}",
        args={"convert_to": "pres", "reason": "misfiled slide deck"},
        **kw,
    )


def _successor(store: Store, deck: Deck) -> int:
    old = store.fetch_refs_by_ids([deck.ref_id], include_deleted=True)[deck.ref_id]
    assert old.retired_at is not None
    result = int(old.meta["superseded_by"])
    assert result != deck.ref_id
    new = store.fetch_refs_by_ids([result])[result]
    assert new.kind == "pres"
    assert new.meta["converted_from_kind"] == "paper"
    return result


def _state(store: Store) -> dict[str, Any]:
    """Capture rows a failed conversion must leave byte-for-byte unchanged."""
    queries = {
        "refs": "SELECT to_jsonb(r) FROM refs r ORDER BY ref_id",
        "chunks": "SELECT to_jsonb(c) FROM chunks c ORDER BY chunk_id",
        "identifiers": "SELECT to_jsonb(i) FROM ref_identifiers i "
        "ORDER BY id_kind, id_value",
        "links": "SELECT to_jsonb(l) FROM links l ORDER BY link_id",
        "tags": "SELECT to_jsonb(t) FROM ref_tags t ORDER BY ref_id, tag_id",
        "events": "SELECT to_jsonb(e) FROM ref_events e ORDER BY ref_id, event_id",
        "pdfs": "SELECT to_jsonb(p) FROM pdfs p ORDER BY pdf_sha256",
    }
    with store.pool.connection() as conn:
        return {name: conn.execute(query).fetchall() for name, query in queries.items()}


def _tags(store: Store, ref_id: int) -> set[str]:
    with store.pool.connection() as conn:
        rows = conn.execute(
            "SELECT t.namespace || ':' || t.value FROM ref_tags rt "
            "JOIN tags t ON t.tag_id = rt.tag_id WHERE rt.ref_id = %s",
            (ref_id,),
        ).fetchall()
    return {str(row[0]) for row in rows}


def test_convert_keeps_pdf_moves_identifiers_and_rechunks_pages(
    store: Store, handler: PaperHandler, deck: Deck
) -> None:
    before = _state(store)
    pdf_bytes = deck.path.read_bytes()
    response = _convert(handler, deck)
    new_id = _successor(store, deck)
    assert f"pa{deck.ref_id}" in response.body
    assert f"pr{new_id}" in response.body
    assert "retir" in response.body.lower()
    assert deck.path.read_bytes() == pdf_bytes
    assert store.pdf_storage_path(deck.sha) == str(deck.path)
    assert store.fetch_refs_by_ids([new_id])[new_id].meta["image_only_pages"] == 1
    with store.pool.connection() as conn:
        assert conn.execute(
            "SELECT pdf_sha256 FROM refs WHERE ref_id = %s", (new_id,)
        ).fetchone() == (deck.sha,)
        identifiers = conn.execute(
            "SELECT id_kind, id_value, ref_id FROM ref_identifiers "
            "WHERE id_value = ANY(%s)",
            ([deck.sha, deck.slug, "10.1/synthetic-deck"],),
        ).fetchall()
        assert set(identifiers) == {
            ("pdf_sha256", deck.sha, new_id),
            ("cite_key", deck.slug, new_id),
            ("doi", "10.1/synthetic-deck", new_id),
        }
        slides = conn.execute(
            "SELECT ord, chunk_kind, page_first, page_last, text, meta "
            "FROM chunks WHERE ref_id = %s AND ord >= 0 ORDER BY ord",
            (new_id,),
        ).fetchall()
        assert [(r[0], r[1], r[2], r[3]) for r in slides] == [
            (page, "pres_slide", page, page) for page in range(3)
        ]
        assert "Alpha heading" in slides[0][4]
        assert slides[1][4] == ""
        assert "Omega heading" in slides[2][4]
        assert [r[5]["slide_index"] for r in slides] == [0, 1, 2]
        assert conn.execute(
            "SELECT count(*) FROM chunk_embeddings e "
            "JOIN chunks c ON c.chunk_id = e.chunk_id WHERE c.ref_id = %s",
            (new_id,),
        ).fetchone() == (0,)
        original_chunks = conn.execute(
            "SELECT to_jsonb(c) FROM chunks c WHERE ref_id = %s ORDER BY chunk_id",
            (deck.ref_id,),
        ).fetchall()
        assert original_chunks == before["chunks"]
    assert _state(store)["pdfs"] == before["pdfs"]
    converted = [e for e in store.events_for(deck.ref_id) if e.event == "converted"]
    assert len(converted) == 1
    assert converted[0].payload["to_kind"] == "pres"
    new_events = [e for e in store.events_for(new_id) if e.event == "converted"]
    assert len(new_events) == 1
    assert new_events[0].payload == converted[0].payload
    assert new_events[0].payload["old_ref_id"] == deck.ref_id


def test_missing_cite_key_keeps_stable_fallback(
    store: Store, handler: PaperHandler, deck: Deck
) -> None:
    with store.pool.connection() as conn:
        conn.execute(
            "DELETE FROM ref_identifiers WHERE ref_id = %s AND id_kind = 'cite_key'",
            (deck.ref_id,),
        )
    _convert(handler, deck)
    new_id = _successor(store, deck)
    with store.pool.connection() as conn:
        assert conn.execute(
            "SELECT id_value FROM ref_identifiers "
            "WHERE ref_id = %s AND id_kind = 'cite_key'",
            (new_id,),
        ).fetchall() == [(f"pa{deck.ref_id}",)]


def test_retired_paper_handle_reads_new_pres(
    store: Store, runtime_with_store: Any, deck: Deck
) -> None:
    _convert(PaperHandler(hub=runtime_with_store.hub), deck)
    new_id = _successor(store, deck)
    resolved = store.resolve_handle(f"pa{deck.ref_id}")
    assert resolved is not None
    assert resolved.ref_id == new_id
    assert resolved.kind == "pres"
    assert resolved.redirected_from == f"pa{deck.ref_id}"
    body, is_error = runtime_with_store.dispatch_with_status(
        "get", {"kind": "paper", "id": f"pa{deck.ref_id}"}
    )
    assert not is_error, body
    assert "Synthetic slide deck" in body


def test_conversion_carries_allowed_tags_and_links(
    store: Store, handler: PaperHandler, deck: Deck
) -> None:
    other = store.insert_ref(kind="paper", slug="link-neighbour", title="Neighbour")
    outward = store.add_link(
        src_ref_id=deck.ref_id, dst_ref_id=other.id, relation="related-to"
    )
    inward = store.add_link(
        src_ref_id=other.id, dst_ref_id=deck.ref_id, relation="related-to"
    )
    for tag in (
        Tag.open("topic:conversion"),
        Tag.closed("SRC", "primary"),
        Tag.closed("JUNK", "junk-title"),
        Tag.closed("JUNKCASCADE", "v3"),
        Tag.closed("DREAM", "acquire"),
    ):
        store.add_tag(deck.ref_id, tag)
    _convert(handler, deck)
    new_id = _successor(store, deck)
    assert _tags(store, new_id) == {
        "OPEN:topic:conversion",
        "OPEN:subtype:slides",
        "OPEN:ocr:needed",
        "SRC:primary",
    }
    with store.pool.connection() as conn:
        assert conn.execute(
            "SELECT src_ref_id, dst_ref_id, src_chunk_id, dst_chunk_id "
            "FROM links WHERE link_id = %s",
            (outward.id,),
        ).fetchone() == (new_id, other.id, None, None)
        assert conn.execute(
            "SELECT src_ref_id, dst_ref_id, src_chunk_id, dst_chunk_id "
            "FROM links WHERE link_id = %s",
            (inward.id,),
        ).fetchone() == (other.id, new_id, None, None)


def test_dry_run_changes_nothing(
    store: Store, handler: PaperHandler, deck: Deck
) -> None:
    before = _state(store)
    response = _convert(handler, deck, dry_run=True)
    assert "DRY RUN" in response.body
    assert "pres" in response.body
    assert _state(store) == before


@pytest.mark.parametrize("missing", ["pointer", "storage-row", "file"])
def test_missing_pdf_refuses_without_changes(
    store: Store, handler: PaperHandler, deck: Deck, missing: str
) -> None:
    if missing == "pointer":
        with store.pool.connection() as conn:
            conn.execute(
                "UPDATE refs SET pdf_sha256 = NULL WHERE ref_id = %s", (deck.ref_id,)
            )
    elif missing == "storage-row":
        store.set_pdf_storage_path(deck.sha, "")
        deck.path.unlink()
    else:
        deck.path.unlink()
    before = _state(store)
    with pytest.raises(BadInput) as exc:
        _convert(handler, deck)
    assert exc.value.next is not None
    assert "pdf" in str(exc.value).lower()
    assert _state(store) == before


def test_pdf_hash_mismatch_refuses_without_changes(
    store: Store, handler: PaperHandler, deck: Deck
) -> None:
    # A still-readable PDF at the recorded path is insufficient: it must
    # contain the exact bytes named by refs.pdf_sha256.
    deck.path.write_bytes(deck.path.read_bytes() + b"\n% changed bytes\n")
    before = _state(store)
    with pytest.raises(BadInput) as exc:
        _convert(handler, deck)
    assert exc.value.next is not None
    assert _state(store) == before


def test_failure_after_retirement_rolls_back_entire_conversion(
    store: Store, handler: PaperHandler, deck: Deck, monkeypatch: pytest.MonkeyPatch
) -> None:
    store.add_tag(deck.ref_id, Tag.open("topic:rollback"))
    other = store.insert_ref(kind="paper", slug="rollback-neighbour", title="Neighbour")
    store.add_link(src_ref_id=deck.ref_id, dst_ref_id=other.id, relation="related-to")
    before = _state(store)
    reached_after_retirement = False

    def fail_event(*args: Any, **kwargs: Any) -> None:
        nonlocal reached_after_retirement
        conn = kwargs["conn"]
        old = conn.execute(
            "SELECT retired_at, meta->>'superseded_by' FROM refs WHERE ref_id = %s",
            (deck.ref_id,),
        ).fetchone()
        assert old is not None and old[0] is not None and old[1] is not None
        new_id = int(old[1])
        assert conn.execute(
            "SELECT count(*) FROM chunks WHERE ref_id = %s AND ord >= 0", (new_id,)
        ).fetchone() == (3,)
        assert conn.execute(
            "SELECT ref_id FROM ref_identifiers "
            "WHERE id_kind = 'pdf_sha256' AND id_value = %s",
            (deck.sha,),
        ).fetchone() == (new_id,)
        reached_after_retirement = True
        raise RuntimeError("injected conversion failure")

    monkeypatch.setattr(store, "append_event", fail_event)
    with pytest.raises(RuntimeError, match="injected conversion failure"):
        _convert(handler, deck)
    assert reached_after_retirement
    assert _state(store) == before
    assert store.fetch_refs_by_ids([deck.ref_id])[deck.ref_id].retired_at is None


@pytest.mark.parametrize(
    "args",
    [
        {"convert_to": "pres"},
        {"convert_to": "pres", "reason": "misfiled", "subtype": "writeup"},
    ],
    ids=["missing-reason", "wrong-subtype"],
)
def test_invalid_arguments_refuse_without_changes(
    store: Store, handler: PaperHandler, deck: Deck, args: dict[str, Any]
) -> None:
    before = _state(store)
    with pytest.raises(BadInput) as exc:
        handler.edit(id=deck.ref_id, args=args)
    assert exc.value.next is not None
    assert _state(store) == before


def test_retired_paper_refuses_without_changes(
    store: Store, handler: PaperHandler, deck: Deck
) -> None:
    store.retire_ref(deck.ref_id)
    before = _state(store)
    with pytest.raises(BadInput, match="retired") as exc:
        _convert(handler, deck)
    assert exc.value.next is not None
    assert _state(store) == before


@pytest.mark.parametrize("role", ["supplement", "parent"])
def test_supplement_conversion_refuses_without_changes(
    store: Store, handler: PaperHandler, deck: Deck, role: str
) -> None:
    if role == "supplement":
        with store.pool.connection() as conn:
            conn.execute(
                "UPDATE refs SET pdf_role = 'supplement' WHERE ref_id = %s",
                (deck.ref_id,),
            )
    else:
        child = store.insert_ref(kind="paper", slug="deck-supplement", title="Child")
        with store.pool.connection() as conn:
            conn.execute(
                "UPDATE refs SET pdf_role = 'supplement' WHERE ref_id = %s", (child.id,)
            )
            conn.execute(
                "INSERT INTO links (src_ref_id, dst_ref_id, relation, set_by, meta) "
                "VALUES (%s, %s, 'part-of', 'agent', "
                '\'{"role":"supplement"}\'::jsonb)',
                (child.id, deck.ref_id),
            )
    before = _state(store)
    with pytest.raises(BadInput) as exc:
        _convert(handler, deck)
    assert exc.value.next is not None
    assert _state(store) == before


@pytest.mark.parametrize("relation", ["establishes", "corroborates"])
@pytest.mark.parametrize("dry_run", [False, True], ids=["write", "dry-run"])
def test_evidence_edge_refuses_without_changes(
    store: Store, handler: PaperHandler, deck: Deck, relation: str, dry_run: bool
) -> None:
    finding = store.insert_ref(kind="finding", slug=None, title="Constraint target")
    with store.pool.connection() as conn:
        edge = conn.execute(
            "INSERT INTO links (src_ref_id, dst_ref_id, relation, set_by) "
            "VALUES (%s, %s, %s, 'agent') RETURNING link_id",
            (deck.ref_id, finding.id, relation),
        ).fetchone()
    assert edge is not None
    before = _state(store)
    with pytest.raises(BadInput, match=relation) as exc:
        _convert(handler, deck, dry_run=dry_run)
    assert f"link:{edge[0]}" in str(exc.value)
    assert exc.value.next is not None and "unlink" in exc.value.next
    assert _state(store) == before


@pytest.mark.parametrize(
    ("first", "last"),
    [(0, 0), (2, 2), (None, None), (0, 2), (3, 3)],
    ids=[
        "estimated-zero",
        "single-page",
        "missing-page",
        "multiple-pages",
        "outside-pdf",
    ],
)
@pytest.mark.parametrize("direction", ["outgoing", "incoming"])
@pytest.mark.parametrize("dry_run", [False, True], ids=["write", "dry-run"])
def test_chunk_anchor_refuses_without_changes(
    store: Store,
    handler: PaperHandler,
    deck: Deck,
    first: int | None,
    last: int | None,
    direction: str,
    dry_run: bool,
) -> None:
    other = store.insert_ref(kind="paper", slug="anchor-neighbour", title="Neighbour")
    with store.pool.connection() as conn:
        chunk = conn.execute(
            "UPDATE chunks SET page_first = %s, page_last = %s "
            "WHERE ref_id = %s AND ord = 0 RETURNING chunk_id",
            (first, last, deck.ref_id),
        ).fetchone()
    assert chunk is not None
    outgoing = direction == "outgoing"
    link = store.add_link(
        src_ref_id=deck.ref_id if outgoing else other.id,
        dst_ref_id=other.id if outgoing else deck.ref_id,
        relation="related-to",
        src_pos=0 if outgoing else None,
        dst_pos=None if outgoing else 0,
    )
    before = _state(store)
    with pytest.raises(BadInput, match="anchor") as exc:
        _convert(handler, deck, dry_run=dry_run)
    assert f"pc{chunk[0]}" in str(exc.value)
    assert f"link:{link.id}" in str(exc.value)
    assert exc.value.next is not None
    assert _state(store) == before


def test_refusal_lists_every_chunk_and_link(
    store: Store, handler: PaperHandler, deck: Deck
) -> None:
    other = store.insert_ref(kind="paper", slug="collapse-neighbour", title="Neighbour")
    with store.pool.connection() as conn:
        conn.execute(
            "UPDATE chunks SET page_first = 0, page_last = 0 "
            "WHERE ref_id = %s AND ord = 1",
            (deck.ref_id,),
        )
    links = [
        store.add_link(
            src_ref_id=deck.ref_id,
            dst_ref_id=other.id,
            relation="related-to",
            src_pos=pos,
        )
        for pos in (0, 1)
    ]
    with store.pool.connection() as conn:
        chunks = conn.execute(
            "SELECT chunk_id FROM chunks WHERE ref_id = %s", (deck.ref_id,)
        ).fetchall()
    before = _state(store)
    with pytest.raises(BadInput, match="anchor") as exc:
        _convert(handler, deck)
    for link in links:
        assert f"link:{link.id}" in str(exc.value)
    for chunk in chunks:
        assert f"pc{chunk[0]}" in str(exc.value)
    assert exc.value.next is not None
    assert _state(store) == before


@pytest.mark.parametrize("drift", ["retired", "pdf"])
def test_locked_recheck_catches_post_probe_drift(
    store: Store,
    handler: PaperHandler,
    deck: Deck,
    monkeypatch: pytest.MonkeyPatch,
    drift: str,
) -> None:
    before = _state(store)
    real_tx = store.tx

    @contextmanager
    def tx_with_race() -> Any:
        with real_tx() as conn:
            if drift == "retired":
                conn.execute(
                    "UPDATE refs SET retired_at = now() WHERE ref_id = %s",
                    (deck.ref_id,),
                )
            else:
                conn.execute(
                    "UPDATE refs SET pdf_sha256 = NULL WHERE ref_id = %s",
                    (deck.ref_id,),
                )
            yield conn

    monkeypatch.setattr(store, "tx", tx_with_race)
    with pytest.raises(BadInput, match="since the check") as exc:
        _convert(handler, deck)
    assert exc.value.next is not None
    assert _state(store) == before


def test_model_bearing_conversion_creates_lifecycle_edge(
    store: Store, handler: PaperHandler, deck: Deck
) -> None:
    """The explicit conversion door can create its structural supersedes edge
    even when the MCP caller's revision context names a model."""
    with revision_context(
        trust_origin="generated", model="test-model", trust_actor_type="agent"
    ):
        _convert(handler, deck)
    new_id = _successor(store, deck)
    with store.pool.connection() as conn:
        assert conn.execute(
            "SELECT src_ref_id, dst_ref_id FROM links "
            "WHERE src_ref_id = %s AND dst_ref_id = %s AND relation = 'supersedes'",
            (new_id, deck.ref_id),
        ).fetchall() == [(new_id, deck.ref_id)]


def test_unreadable_pdf_extraction_is_upstream_without_changes(
    store: Store, handler: PaperHandler, deck: Deck
) -> None:
    deck.path.write_bytes(b"This is not a PDF file.")
    before = _state(store)
    with pytest.raises(Upstream, match="extract") as exc:
        _convert(handler, deck)
    assert exc.value.next is not None
    assert _state(store) == before


@pytest.mark.parametrize("dry_run", [False, True], ids=["write", "dry-run"])
def test_foreign_pdf_identifier_owner_refuses_without_changes(
    store: Store, handler: PaperHandler, deck: Deck, dry_run: bool
) -> None:
    other = store.insert_ref(kind="paper", slug="pdf-owner", title="Other PDF owner")
    with store.pool.connection() as conn:
        conn.execute(
            "UPDATE ref_identifiers SET ref_id = %s "
            "WHERE id_kind = 'pdf_sha256' AND id_value = %s AND ref_id = %s",
            (other.id, deck.sha, deck.ref_id),
        )
    before = _state(store)
    with pytest.raises(BadInput, match="identifier belongs") as exc:
        _convert(handler, deck, dry_run=dry_run)
    assert exc.value.next is not None
    assert _state(store) == before


@pytest.mark.parametrize("shape", ["string", "list"])
@pytest.mark.parametrize("external_link", [False, True])
@pytest.mark.parametrize("dry_run", [False, True], ids=["write", "dry-run"])
def test_link_evidence_pins_refuse_without_changes(
    store: Store,
    handler: PaperHandler,
    deck: Deck,
    shape: str,
    external_link: bool,
    dry_run: bool,
) -> None:
    other = store.insert_ref(kind="paper", slug="evidence-neighbour", title="Neighbour")
    with store.pool.connection() as conn:
        source = conn.execute(
            "SELECT chunk_id FROM chunks WHERE ref_id = %s AND ord = 1",
            (deck.ref_id,),
        ).fetchone()
        external = conn.execute(
            "INSERT INTO chunks (ref_id, ord, chunk_kind, text, set_by) "
            "VALUES (%s, 0, 'paragraph', 'External evidence label', 'system') "
            "RETURNING chunk_id",
            (other.id,),
        ).fetchone()
    assert source is not None and external is not None
    old_pin = f"pc{source[0]}"
    external_pin = f"pc{external[0]}"
    evidence: str | list[str] = (
        old_pin if shape == "string" else [old_pin, external_pin]
    )
    unrelated = {"why": "Synthetic evidence mapping", "test_marker": {"retain": True}}
    source_id = deck.ref_id
    if external_link:
        source_id = store.insert_ref(
            kind="paper", slug="external-source", title="External source"
        ).id
    link = store.add_link(
        src_ref_id=source_id,
        dst_ref_id=other.id,
        relation="related-to",
        meta={"evidence": evidence, **unrelated},
    )

    before = _state(store)
    with pytest.raises(BadInput, match="evidence pins") as exc:
        _convert(handler, deck, dry_run=dry_run)
    assert old_pin in str(exc.value)
    assert f"link:{link.id}" in str(exc.value)
    assert exc.value.next is not None
    assert _state(store) == before
