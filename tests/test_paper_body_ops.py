"""``edit(args={…})`` body ops — drop/move/split a body, clear an abstract.

gr514995: the Reto-approved gr513878 wrong-PDF repair needed four doors
``edit(kind='paper')`` didn't have — detach a wrong body (``drop_body``),
hand it to the live ref its metadata describes (``move_body_to``),
re-parent it under a freshly minted ref (``split_body``), and remove a
junk abstract (``clear_abstract``). All DELETE+INSERT (append-only
invariant), ``reason=``-audited, one transaction per call.
"""

from __future__ import annotations

from typing import Any

import pytest

from precis.dispatch import Hub
from precis.errors import BadInput, NotFound
from precis.handlers.paper import PaperHandler
from precis.response import Response
from precis.store import ChunkInsert, Store

_SHA = "a" * 64


def _seed(store: Store, slug: str, **kw: Any) -> int:
    return store.insert_ref(
        kind="paper",
        slug=slug,
        title=kw.pop("title", f"Title of {slug}"),
        provider="manual",
        meta=kw.pop("meta", {}),
        **kw,
    ).id


def _body(store: Store, ref_id: int, n: int = 2, *, embed: bool = True) -> None:
    vec = [0.01] * store.embedding_dim() if embed else None
    store.chunks.insert_chunks(
        ref_id,
        [
            ChunkInsert(ord=i, text=f"body block {i} of {ref_id}", embedding=vec)
            for i in range(n)
        ],
    )


def _ords(store: Store, ref_id: int) -> list[int]:
    with store.pool.connection() as conn:
        rows = conn.execute(
            "SELECT ord FROM chunks WHERE ref_id = %s AND ord >= 0 "
            "AND retired_at IS NULL ORDER BY ord",
            (ref_id,),
        ).fetchall()
    return [int(r[0]) for r in rows]


def _emb_count(store: Store, ref_id: int) -> int:
    with store.pool.connection() as conn:
        row = conn.execute(
            "SELECT count(*) FROM chunk_embeddings e JOIN chunks c "
            "ON c.chunk_id = e.chunk_id WHERE c.ref_id = %s AND c.ord >= 0",
            (ref_id,),
        ).fetchone()
    return int((row or (0,))[0])


def _pdf(store: Store, ref_id: int, sha: str = _SHA) -> None:
    with store.pool.connection() as conn:
        conn.execute(
            "INSERT INTO pdfs (pdf_sha256, content_hash, page_count, "
            "size_bytes, storage_path) VALUES (%s, %s, 3, 10, 'x.pdf') "
            "ON CONFLICT DO NOTHING",
            (sha, sha),
        )
        conn.execute("UPDATE refs SET pdf_sha256 = %s WHERE ref_id = %s", (sha, ref_id))
        conn.execute(
            "INSERT INTO ref_identifiers (id_kind, id_value, ref_id, source) "
            "VALUES ('pdf_sha256', %s, %s, 'test') ON CONFLICT DO NOTHING",
            (sha, ref_id),
        )


def _pdf_sha(store: Store, ref_id: int) -> str | None:
    with store.pool.connection() as conn:
        row = conn.execute(
            "SELECT pdf_sha256 FROM refs WHERE ref_id = %s", (ref_id,)
        ).fetchone()
    return row[0] if row else None


def _meta(store: Store, ref_id: int) -> dict[str, Any]:
    ref = store.fetch_refs_by_ids([ref_id], include_deleted=True)[ref_id]
    return dict(ref.meta or {})


def _events(store: Store, ref_id: int, event: str) -> list[Any]:
    return [e for e in store.events_for(ref_id) if e.event == event]


def _id_kinds(store: Store, ref_id: int) -> set[str]:
    with store.pool.connection() as conn:
        rows = conn.execute(
            "SELECT id_kind FROM ref_identifiers WHERE ref_id = %s", (ref_id,)
        ).fetchall()
    return {str(r[0]) for r in rows}


def _chunk_ids(store: Store, ref_id: int) -> dict[int, int]:
    """``ord -> chunk_id`` for the ref's live body chunks."""
    with store.pool.connection() as conn:
        rows = conn.execute(
            "SELECT ord, chunk_id FROM chunks WHERE ref_id = %s "
            "AND ord >= 0 AND retired_at IS NULL",
            (ref_id,),
        ).fetchall()
    return {int(r[0]): int(r[1]) for r in rows}


def _link_row(store: Store, link_id: int) -> Any:
    with store.pool.connection() as conn:
        return conn.execute(
            "SELECT src_ref_id, src_chunk_id, dst_ref_id, dst_chunk_id "
            "FROM links WHERE link_id = %s",
            (link_id,),
        ).fetchone()


def _v1_link(store: Store, **kw: Any) -> Any:
    """A ``trust_write_version = 1`` link — written under a converted
    context with a converted-kind (``memory``) endpoint, the prod shape
    that makes a context-less chunk re-point refuse
    (``links_trust_guard``, migration 0214)."""
    from precis.store.revision_context import revision_context

    with revision_context(trust_origin="generated", trust_actor_type="agent"):
        return store.add_link(**kw)


def _link_stamp(store: Store, link_id: int) -> tuple[str, str, int]:
    state = store.trust_state("link", link_id)
    assert state is not None
    return (state.origin, state.confidence, state.version)


def _memory_ref(store: Store) -> int:
    return store.insert_ref(kind="memory", slug=None, title="m", meta={}).id


def _derived_count(store: Store, ref_id: int, table: str) -> int:
    with store.pool.connection() as conn:
        row = conn.execute(
            f"SELECT count(*) FROM {table} t JOIN chunks c "
            "ON c.chunk_id = t.chunk_id "
            "WHERE c.ref_id = %s AND c.ord >= 0",
            (ref_id,),
        ).fetchone()
    return int((row or (0,))[0])


def _seed_derived(store: Store, ref_id: int) -> None:
    """One derived row per copy-table on each live body chunk."""
    with store.pool.connection() as conn:
        chunk_ids = [
            int(r[0])
            for r in conn.execute(
                "SELECT chunk_id FROM chunks WHERE ref_id = %s AND ord >= 0",
                (ref_id,),
            ).fetchall()
        ]
        bib = conn.execute(
            "INSERT INTO paper_bib_entries (ref_id, marker, raw_text, "
            "parse_version) VALUES (%s, 1, 'Someone 2020', 1) RETURNING id",
            (ref_id,),
        ).fetchone()
        tag = conn.execute(
            "INSERT INTO tags (namespace, value) VALUES ('OPEN', 'bodyops') "
            "RETURNING tag_id"
        ).fetchone()
        assert bib is not None and tag is not None
        for cid in chunk_ids:
            conn.execute(
                "INSERT INTO chunk_summaries (chunk_id, summarizer, text) "
                "VALUES (%s, 'llm-v1', 'a summary')",
                (cid,),
            )
            conn.execute(
                "INSERT INTO chunk_citations (chunk_id, marker, bib_entry_id) "
                "VALUES (%s, 1, %s)",
                (cid, int(bib[0])),
            )
            conn.execute(
                "INSERT INTO chunk_blobs (chunk_id, bytes, mime, sha256, "
                "size_bytes) VALUES (%s, %s, 'image/png', %s, 1)",
                (cid, b"\x00", "f" * 64),
            )
            conn.execute(
                "INSERT INTO chunk_tags (chunk_id, tag_id, set_by) "
                "VALUES (%s, %s, 'agent')",
                (cid, int(tag[0])),
            )
            conn.execute(
                "INSERT INTO chunk_review (chunk_id, checker, approved_sha, "
                "verdict) VALUES (%s, 'checker', 'abc', 'ok')",
                (cid,),
            )


@pytest.fixture
def handler(hub: Hub) -> PaperHandler:
    return PaperHandler(hub=hub)


def _edit(handler: PaperHandler, id: Any, args: dict[str, Any], **kw: Any) -> Response:
    return handler.edit(id=id, args=args, **kw)


class TestDropBody:
    def test_happy_path(self, store: Store, handler: PaperHandler) -> None:
        ref = _seed(store, "droptarget2020", year=2020, meta={"source_format": "pdf"})
        _body(store, ref, 3)
        _pdf(store, ref)
        store.set_ref_identifier(ref, "doi", "10.1/abc", source="test")
        resp = _edit(handler, ref, {"drop_body": True, "reason": "wrong pdf gr513878"})
        assert _ords(store, ref) == []
        assert _emb_count(store, ref) == 0
        assert _pdf_sha(store, ref) is None
        # bibliographic id kept, content ids dropped
        assert _id_kinds(store, ref) == {"cite_key", "doi"}
        assert "source_format" not in _meta(store, ref)
        assert "dropped" in resp.body
        ev = _events(store, ref, "body_dropped")
        assert len(ev) == 1
        assert ev[0].payload["reason"] == "wrong pdf gr513878"
        assert ev[0].payload["body_chunks"] == 3
        assert ev[0].payload["pdf_cleared"] is True
        # not retired — still a live stub
        ref_obj = store.fetch_refs_by_ids([ref])[ref]
        assert ref_obj.retired_at is None
        assert ref_obj.title == "Title of droptarget2020"

    def test_noop_when_already_stub(self, store: Store, handler: PaperHandler) -> None:
        ref = _seed(store, "dropstub2020", year=2020)
        resp = _edit(handler, ref, {"drop_body": True, "reason": "x"})
        assert "no change" in resp.body
        assert _events(store, ref, "body_dropped") == []

    def test_dry_run_writes_nothing(self, store: Store, handler: PaperHandler) -> None:
        ref = _seed(store, "dropdry2020", year=2020)
        _body(store, ref, 2)
        _pdf(store, ref)
        resp = _edit(
            handler,
            ref,
            {"drop_body": True, "reason": "x"},
            dry_run=True,
        )
        assert "DRY RUN" in resp.body
        assert _ords(store, ref) == [0, 1]
        assert _emb_count(store, ref) == 2
        assert _pdf_sha(store, ref) == _SHA

    def test_missing_reason_refused(self, store: Store, handler: PaperHandler) -> None:
        ref = _seed(store, "dropnor2020", year=2020)
        _body(store, ref, 1)
        with pytest.raises(BadInput, match="reason") as exc:
            _edit(handler, ref, {"drop_body": True})
        assert exc.value.next is not None
        assert _ords(store, ref) == [0]

    def test_retired_refused(self, store: Store, handler: PaperHandler) -> None:
        ref = _seed(store, "dropdead2020", year=2020)
        _body(store, ref, 1)
        store.retire_ref(ref)
        with pytest.raises(BadInput, match="retired"):
            _edit(handler, ref, {"drop_body": True, "reason": "x"})
        assert _ords(store, ref) == [0]

    def test_chunk_links_counted_on_drop(
        self, store: Store, handler: PaperHandler
    ) -> None:
        ref = _seed(store, "droplinks2020", year=2020)
        other = _seed(store, "dropother2020", year=2020)
        _body(store, ref, 2)
        out = store.add_link(
            src_ref_id=ref, dst_ref_id=other, relation="related-to", src_pos=0
        )
        inc = store.add_link(
            src_ref_id=other, dst_ref_id=ref, relation="related-to", dst_pos=1
        )
        resp = _edit(handler, ref, {"drop_body": True, "reason": "x"})
        # Chunk-anchored links die with the chunks — and the loss is
        # counted in the ack and the ref_event.
        assert "2 chunk link(s) removed" in resp.body
        with store.pool.connection() as conn:
            n = conn.execute(
                "SELECT count(*) FROM links WHERE link_id = ANY(%s)",
                ([out.id, inc.id],),
            ).fetchone()
        assert n is not None and n[0] == 0
        ev = _events(store, ref, "body_dropped")
        assert ev[0].payload["chunk_links_removed"] == 2

    def test_chunk_links_removed_includes_retired(
        self, store: Store, handler: PaperHandler
    ) -> None:
        """A drop's link count is the union over live and retired
        chunks — links anchored on retired chunks die too."""
        ref = _seed(store, "dropmix2020", year=2020)
        other = _seed(store, "dropmixo2020", year=2020)
        _body(store, ref, 2)
        store.add_link(
            src_ref_id=other, dst_ref_id=ref, relation="related-to", dst_pos=0
        )
        store.add_link(src_ref_id=other, dst_ref_id=ref, relation="cites", dst_pos=1)
        with store.pool.connection() as conn:
            conn.execute(
                "UPDATE chunks SET retired_at = now() WHERE ref_id = %s AND ord = 1",
                (ref,),
            )
        resp = _edit(handler, ref, {"drop_body": True, "reason": "x"})
        assert "2 chunk link(s) removed" in resp.body
        ev = _events(store, ref, "body_dropped")
        assert ev[0].payload["chunk_links_removed"] == 2
        with store.pool.connection() as conn:
            n = conn.execute("SELECT count(*) FROM links").fetchone()
        assert n is not None and n[0] == 0

    def test_intx_recheck_refuses_retired_ref(
        self, store: Store, handler: PaperHandler, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A ref tombstoned between probe and tx is caught by the locked
        re-check (FOR NO KEY UPDATE) — the drop rolls back, body intact."""
        import contextlib

        a = _seed(store, "droprace2020", year=2020)
        _body(store, a, 1)
        real_tx = store.tx

        @contextlib.contextmanager
        def tx_with_race() -> Any:
            with real_tx() as conn:
                conn.execute(
                    "UPDATE refs SET retired_at = now() WHERE ref_id = %s", (a,)
                )
                yield conn

        monkeypatch.setattr(store, "tx", tx_with_race)
        with pytest.raises(BadInput, match="retired or merged") as exc:
            _edit(handler, a, {"drop_body": True, "reason": "x"})
        assert exc.value.next is not None
        assert _ords(store, a) == [0]
        assert store.fetch_refs_by_ids([a])[a].retired_at is None
        assert _events(store, a, "body_dropped") == []

    def test_retired_chunks_counted(self, store: Store, handler: PaperHandler) -> None:
        ref = _seed(store, "dropret2020", year=2020)
        _body(store, ref, 3)
        with store.pool.connection() as conn:
            conn.execute(
                "UPDATE chunks SET retired_at = now() WHERE ref_id = %s AND ord = 2",
                (ref,),
            )
        resp = _edit(handler, ref, {"drop_body": True, "reason": "x"})
        assert "2 chunk(s)" in resp.body and "(+1 retired)" in resp.body
        ev = _events(store, ref, "body_dropped")
        assert ev[0].payload["body_chunks"] == 2
        assert ev[0].payload["retired_chunks"] == 1
        # the retired row deleted too
        with store.pool.connection() as conn:
            n = conn.execute(
                "SELECT count(*) FROM chunks WHERE ref_id = %s AND ord >= 0",
                (ref,),
            ).fetchone()
        assert n is not None and n[0] == 0


class TestMoveBody:
    def test_happy_path(self, store: Store, handler: PaperHandler) -> None:
        a = _seed(store, "movedonor2020", year=2020, meta={"source_format": "pdf"})
        b = _seed(store, "movetarget2020", year=2021)
        _body(store, a, 3)
        _pdf(store, a)
        resp = _edit(
            handler, a, {"move_body_to": f"pa{b}", "reason": "body is B's paper"}
        )
        assert _ords(store, a) == []
        assert _ords(store, b) == [0, 1, 2]
        # embeddings follow the body
        assert _emb_count(store, a) == 0
        assert _emb_count(store, b) == 3
        assert _pdf_sha(store, a) is None
        assert _pdf_sha(store, b) == _SHA
        # pdf_sha256 identifier followed the body; cite_key stayed
        assert _id_kinds(store, a) == {"cite_key"}
        # body-owned meta moved
        assert "source_format" in _meta(store, b)
        assert "source_format" not in _meta(store, a)
        assert "moved" in resp.body
        ev_a = _events(store, a, "body_moved")
        ev_b = _events(store, b, "body_moved")
        assert len(ev_a) == len(ev_b) == 1
        assert ev_a[0].payload["moved_to"] == b
        assert ev_b[0].payload["moved_from"] == a
        assert ev_a[0].payload["reason"] == "body is B's paper"
        # neither ref retired
        live = store.fetch_refs_by_ids([a, b])
        assert live[a].retired_at is None and live[b].retired_at is None
        # the target got search cards
        with store.pool.connection() as conn:
            n_cards = conn.execute(
                "SELECT count(*) FROM chunks WHERE ref_id = %s AND ord < 0", (b,)
            ).fetchone()
        assert n_cards is not None and n_cards[0] >= 1

    def test_refuses_nonempty_target(self, store: Store, handler: PaperHandler) -> None:
        a = _seed(store, "mvsrc2020", year=2020)
        b = _seed(store, "mvfull2020", year=2020)
        _body(store, a, 2)
        _body(store, b, 1)
        with pytest.raises(BadInput, match="body chunk") as exc:
            _edit(handler, a, {"move_body_to": b, "reason": "x"})
        assert "drop_body" in str(exc.value.next)
        assert _ords(store, a) == [0, 1]
        assert _ords(store, b) == [0]

    def test_refuses_two_pdfs(self, store: Store, handler: PaperHandler) -> None:
        a = _seed(store, "mvpa2020", year=2020)
        b = _seed(store, "mvpb2020", year=2020)
        _body(store, a, 1)
        _pdf(store, a)
        _pdf(store, b, sha="b" * 64)
        with pytest.raises(BadInput, match="pdf"):
            _edit(handler, a, {"move_body_to": b, "reason": "x"})
        assert _pdf_sha(store, a) == _SHA

    def test_refuses_pdf_target_with_pdfless_donor(
        self, store: Store, handler: PaperHandler
    ) -> None:
        """A pdf-holding target takes no pdf-less donor's body — that
        welds text extracted from a different file next to its pdf."""
        a = _seed(store, "mvnopdfa2020", year=2020)
        b = _seed(store, "mvnopdfb2020", year=2020)
        _body(store, a, 2)
        _pdf(store, b, sha="b" * 64)
        with pytest.raises(BadInput, match="different file") as exc:
            _edit(handler, a, {"move_body_to": f"pa{b}", "reason": "x"})
        assert "bodiless-heal" in str(exc.value.next)
        assert _ords(store, a) == [0, 1]
        assert _ords(store, b) == []
        assert _pdf_sha(store, b) == "b" * 64
        assert _events(store, a, "body_moved") == []

    def test_intx_recheck_refuses_pdf_when_donor_has_none(
        self, store: Store, handler: PaperHandler, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A pdf landing on the target between probe and tx is caught by
        the locked re-check even when the donor carries no pdf."""
        import contextlib

        a = _seed(store, "mvnpdfa2020", year=2020)
        b = _seed(store, "mvnpdfb2020", year=2020)
        _body(store, a, 1)
        real_tx = store.tx

        @contextlib.contextmanager
        def tx_with_race() -> Any:
            with real_tx() as conn:
                _pdf(store, b, sha="d" * 64)
                yield conn

        monkeypatch.setattr(store, "tx", tx_with_race)
        with pytest.raises(BadInput, match="gained a pdf") as exc:
            _edit(handler, a, {"move_body_to": f"pa{b}", "reason": "x"})
        assert exc.value.next is not None
        assert _ords(store, a) == [0]
        assert _ords(store, b) == []

    def test_target_stale_pdf_ids_deleted(
        self, store: Store, handler: PaperHandler
    ) -> None:
        """(ref_id, id_kind) has no unique constraint — the pk is
        (id_kind, id_value) — so re-homing the donor's pdf-kind ids onto
        a target that still holds stale rows of those kinds would double
        them. The stale rows are deleted in the same transaction."""
        a = _seed(store, "mvstalea2020", year=2020)
        b = _seed(store, "mvstaleb2020", year=2020)
        _body(store, a, 1)
        _pdf(store, a)
        store.set_ref_identifier(a, "content_hash", "f" * 64, source="test")
        with store.pool.connection() as conn:
            conn.execute(
                "INSERT INTO ref_identifiers (id_kind, id_value, ref_id, "
                "source) VALUES ('pdf_sha256', %s, %s, 'test'), "
                "('content_hash', %s, %s, 'test')",
                ("d" * 64, b, "e" * 64, b),
            )
        _edit(handler, a, {"move_body_to": f"pa{b}", "reason": "x"})
        with store.pool.connection() as conn:
            rows = conn.execute(
                "SELECT id_kind, id_value FROM ref_identifiers "
                "WHERE ref_id = %s AND id_kind = ANY(%s) ORDER BY id_kind",
                (b, ["content_hash", "pdf_sha256"]),
            ).fetchall()
        assert [(str(r[0]), str(r[1])) for r in rows] == [
            ("content_hash", "f" * 64),
            ("pdf_sha256", _SHA),
        ]

    def test_dry_run_writes_nothing(self, store: Store, handler: PaperHandler) -> None:
        a = _seed(store, "mvdrya2020", year=2020)
        b = _seed(store, "mvdryb2020", year=2020)
        _body(store, a, 2)
        resp = _edit(handler, a, {"move_body_to": b, "reason": "x"}, dry_run=True)
        assert "DRY RUN" in resp.body
        assert _ords(store, a) == [0, 1]
        assert _ords(store, b) == []
        assert _events(store, a, "body_moved") == []
        assert _events(store, b, "body_moved") == []

    def test_chunk_links_repointed(self, store: Store, handler: PaperHandler) -> None:
        a = _seed(store, "mvlinka2020", year=2020)
        b = _seed(store, "mvlinkb2020", year=2020)
        other = _seed(store, "mvlinko2020", year=2020)
        _body(store, a, 3)
        out = store.add_link(
            src_ref_id=a, dst_ref_id=other, relation="related-to", src_pos=0
        )
        inc = store.add_link(
            src_ref_id=other, dst_ref_id=a, relation="related-to", dst_pos=2
        )
        resp = _edit(handler, a, {"move_body_to": b, "reason": "x"})
        assert "2 chunk link(s) re-pointed" in resp.body
        new = _chunk_ids(store, b)
        row = _link_row(store, out.id)
        assert (int(row[0]), int(row[1]), int(row[2]), row[3]) == (
            b,
            new[0],
            other,
            None,
        )
        row = _link_row(store, inc.id)
        assert (int(row[0]), row[1], int(row[2]), int(row[3])) == (
            other,
            None,
            b,
            new[2],
        )
        ev = _events(store, b, "body_moved")
        assert ev[0].payload["chunk_links_repointed"] == 2

    def test_derived_rows_copied(self, store: Store, handler: PaperHandler) -> None:
        a = _seed(store, "mvsumm2020", year=2020)
        b = _seed(store, "mvsummt2020", year=2020)
        _body(store, a, 2)
        _seed_derived(store, a)
        _edit(handler, a, {"move_body_to": b, "reason": "x"})
        for table in (
            "chunk_summaries",
            "chunk_citations",
            "chunk_blobs",
            "chunk_tags",
            "chunk_review",
        ):
            assert _derived_count(store, a, table) == 0
            assert _derived_count(store, b, table) == 2
        # the chunk_embeddings trigger fills the denormalised ref_id/kind
        # from the new parent chunk (migration 0226) — assert it
        with store.pool.connection() as conn:
            rows = conn.execute(
                "SELECT e.ref_id, e.kind FROM chunk_embeddings e "
                "JOIN chunks c ON c.chunk_id = e.chunk_id "
                "WHERE c.ref_id = %s AND c.ord >= 0",
                (b,),
            ).fetchall()
        assert rows and all(int(r[0]) == b and r[1] == "paper" for r in rows)

    def test_refuses_tombstone_target(
        self, store: Store, handler: PaperHandler
    ) -> None:
        a = _seed(store, "mvtsrc2020", year=2020)
        b = _seed(store, "mvtomb2020", year=2020)
        survivor = _seed(store, "mvtsurv2020", year=2020)
        _body(store, a, 1)
        store.retire_ref(b)
        store.stamp_ref_meta(b, {"superseded_by": survivor})
        with pytest.raises(BadInput, match="merged into") as exc:
            _edit(handler, a, {"move_body_to": f"pa{b}", "reason": "x"})
        assert exc.value.next is not None
        assert _ords(store, a) == [0]

    def test_intx_recheck_refuses_new_target_pdf(
        self, store: Store, handler: PaperHandler, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A pdf landing on the target between probe and tx is caught by
        the locked re-check."""
        import contextlib

        a = _seed(store, "mvracea2020", year=2020)
        b = _seed(store, "mvraceb2020", year=2020)
        _body(store, a, 1)
        _pdf(store, a)
        real_tx = store.tx

        @contextlib.contextmanager
        def tx_with_race() -> Any:
            with real_tx() as conn:
                # Committed on its own connection inside the open tx —
                # the FOR NO KEY UPDATE re-read sees it.
                _pdf(store, b, sha="c" * 64)
                yield conn

        monkeypatch.setattr(store, "tx", tx_with_race)
        with pytest.raises(BadInput, match="gained a pdf") as exc:
            _edit(handler, a, {"move_body_to": f"pa{b}", "reason": "x"})
        assert exc.value.next is not None
        assert _ords(store, a) == [0]
        assert _pdf_sha(store, a) == _SHA
        assert _ords(store, b) == []

    def test_intx_recheck_refuses_retired_target(
        self, store: Store, handler: PaperHandler, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A target retired between probe and tx is caught by the
        locked re-check."""
        import contextlib

        a = _seed(store, "mvrra2020", year=2020)
        b = _seed(store, "mvrrb2020", year=2020)
        _body(store, a, 1)
        real_tx = store.tx

        @contextlib.contextmanager
        def tx_with_race() -> Any:
            with real_tx() as conn:
                conn.execute(
                    "UPDATE refs SET retired_at = now() WHERE ref_id = %s", (b,)
                )
                yield conn

        monkeypatch.setattr(store, "tx", tx_with_race)
        with pytest.raises(BadInput, match="retired or merged") as exc:
            _edit(handler, a, {"move_body_to": f"pa{b}", "reason": "x"})
        assert exc.value.next is not None
        # rolled back: donor still holds its body, target has none
        assert _ords(store, a) == [0]
        assert _ords(store, b) == []

    def test_intx_recheck_refuses_new_target_body(
        self, store: Store, handler: PaperHandler, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A body landing on the target between probe and tx is caught
        by the locked re-check."""
        import contextlib

        a = _seed(store, "mvrba2020", year=2020)
        b = _seed(store, "mvrbb2020", year=2020)
        _body(store, a, 1)
        real_tx = store.tx

        @contextlib.contextmanager
        def tx_with_race() -> Any:
            with real_tx() as conn:
                conn.execute(
                    "INSERT INTO chunks (ref_id, ord, chunk_kind, text) "
                    "VALUES (%s, 0, 'paragraph', 'raced body')",
                    (b,),
                )
                yield conn

        monkeypatch.setattr(store, "tx", tx_with_race)
        with pytest.raises(BadInput, match="gained a body") as exc:
            _edit(handler, a, {"move_body_to": f"pa{b}", "reason": "x"})
        assert exc.value.next is not None
        assert _ords(store, a) == [0]

    def test_trust_v1_link_repointed(self, store: Store, handler: PaperHandler) -> None:
        """A version-1 trust link on a donor chunk refuses a context-less
        re-point — the door's computed write context carries it."""
        a = _seed(store, "mvtrusta2020", year=2020)
        b = _seed(store, "mvtrustb2020", year=2020)
        mem = _memory_ref(store)
        _body(store, a, 2)
        link = _v1_link(store, src_ref_id=mem, dst_ref_id=a, dst_pos=0)
        assert _link_stamp(store, link.id) == ("generated", "hunch", 1)
        resp = _edit(handler, a, {"move_body_to": f"pa{b}", "reason": "x"})
        assert "1 chunk link(s) re-pointed" in resp.body
        new = _chunk_ids(store, b)
        row = _link_row(store, link.id)
        assert (int(row[0]), row[1], int(row[2]), int(row[3])) == (
            mem,
            None,
            b,
            new[0],
        )
        # the edge keeps its link_id and its own stamp: the re-anchor
        # moved it onto a byte-identical copy, so the internal door
        # re-applied the prior stamp the revise act recorded
        # (gr514995 option A).
        assert _link_stamp(store, link.id) == ("generated", "hunch", 1)
        acts = store.trust_acts_for("link", link.id)
        assert [act["action"] for act in acts] == ["create", "revise", "revise"]

    def test_link_on_retired_chunk_counted_removed(
        self, store: Store, handler: PaperHandler
    ) -> None:
        """A link anchored on a retired donor chunk has no copy to
        re-point onto — it dies with the delete and is counted."""
        a = _seed(store, "mvretl2020", year=2020)
        b = _seed(store, "mvretlt2020", year=2020)
        other = _seed(store, "mvretlo2020", year=2020)
        _body(store, a, 2)
        link = store.add_link(
            src_ref_id=other, dst_ref_id=a, relation="related-to", dst_pos=1
        )
        with store.pool.connection() as conn:
            conn.execute(
                "UPDATE chunks SET retired_at = now() WHERE ref_id = %s AND ord = 1",
                (a,),
            )
        dry = _edit(
            handler,
            a,
            {"move_body_to": f"pa{b}", "reason": "x"},
            dry_run=True,
        )
        assert "1 retired-chunk link(s) removed" in dry.body
        resp = _edit(handler, a, {"move_body_to": f"pa{b}", "reason": "x"})
        assert "0 chunk link(s) re-pointed" in resp.body
        assert "1 retired-chunk link(s) removed" in resp.body
        assert _link_row(store, link.id) is None
        ev = _events(store, b, "body_moved")
        assert ev[0].payload["chunk_links_repointed"] == 0
        assert ev[0].payload["chunk_links_removed"] == 1

    def test_missing_reason_refused(self, store: Store, handler: PaperHandler) -> None:
        a = _seed(store, "mvra2020", year=2020)
        b = _seed(store, "mvrb2020", year=2020)
        _body(store, a, 1)
        with pytest.raises(BadInput, match="reason"):
            _edit(handler, a, {"move_body_to": b})

    def test_self_move_refused(self, store: Store, handler: PaperHandler) -> None:
        a = _seed(store, "mvself2020", year=2020)
        _body(store, a, 1)
        with pytest.raises(BadInput, match="record itself"):
            _edit(handler, a, {"move_body_to": f"pa{a}", "reason": "x"})

    def test_unknown_target_refused(self, store: Store, handler: PaperHandler) -> None:
        a = _seed(store, "mvunk2020", year=2020)
        _body(store, a, 1)
        with pytest.raises(BadInput, match="resolves to no"):
            _edit(handler, a, {"move_body_to": "pa99999999", "reason": "x"})


class TestSplitBody:
    def test_happy_path(self, store: Store, handler: PaperHandler) -> None:
        a = _seed(store, "splitdonor2019", year=2019)
        _body(store, a, 2)
        _pdf(store, a)
        resp = _edit(
            handler,
            a,
            {
                "split_body": {
                    "title": "The Companion Methods Paper",
                    "year": 2026,
                    "authors": [{"family": "Jaroenram", "given": "W."}],
                    "journal": "MethodsX",
                    "doi": "10.2/companion",
                },
                "reason": "body is a different work gr513878",
            },
        )
        assert _ords(store, a) == []
        # the minted ref carries the body + embeddings + pdf + doi
        with store.pool.connection() as conn:
            row = conn.execute(
                "SELECT r.ref_id FROM refs r JOIN ref_identifiers i "
                "ON i.ref_id = r.ref_id WHERE i.id_kind = 'doi' "
                "AND i.id_value = '10.2/companion'",
            ).fetchone()
        assert row is not None
        b = int(row[0])
        assert b != a
        assert _ords(store, b) == [0, 1]
        assert _emb_count(store, b) == 2
        assert _pdf_sha(store, b) == _SHA
        new_ref = store.fetch_refs_by_ids([b])[b]
        assert new_ref.title == "The Companion Methods Paper"
        assert new_ref.year == 2026
        assert _meta(store, b)["journal"] == "MethodsX"
        ev_a = _events(store, a, "body_split")
        ev_b = _events(store, b, "body_moved")
        assert len(ev_a) == 1 and ev_a[0].payload["new_ref_id"] == b
        assert len(ev_b) == 1 and ev_b[0].payload["moved_from"] == a
        assert f"pa{b}" in resp.body

    def test_minted_ref_stays_in_fk_vocab(
        self, store: Store, handler: PaperHandler
    ) -> None:
        """gr515089/gr515090: the split mint uses FK-vocabulary values —
        ``provider='manual'`` ('agent' is not a provider) and chunk
        ``set_by`` NULL ('edit' is not an actor; the meta.set_by='edit'
        stamp is JSON annotation, not the column)."""
        a = _seed(store, "splitvocab2020", year=2020)
        _body(store, a, 1)
        _edit(handler, a, {"split_body": {"title": "X"}, "reason": "x"})
        ev_a = _events(store, a, "body_split")
        b = int(ev_a[0].payload["new_ref_id"])
        assert store.fetch_refs_by_ids([b])[b].provider == "manual"
        with store.pool.connection() as conn:
            non_null = conn.execute(
                "SELECT count(*) FROM chunks WHERE ref_id = %s AND set_by IS NOT NULL",
                (b,),
            ).fetchone()
        assert non_null is not None and non_null[0] == 0

    def test_refuses_taken_doi(self, store: Store, handler: PaperHandler) -> None:
        a = _seed(store, "splitdup2020", year=2020)
        other = _seed(store, "splitowner2020", year=2020)
        _body(store, a, 1)
        store.set_ref_identifier(other, "doi", "10.3/taken", source="test")
        with pytest.raises(BadInput, match="already belongs") as exc:
            _edit(
                handler,
                a,
                {
                    "split_body": {"title": "X", "doi": "10.3/taken"},
                    "reason": "x",
                },
            )
        assert f"pa{other}" in str(exc.value)
        assert "move_body_to" in str(exc.value.next)
        assert _ords(store, a) == [0]

    def test_requires_title(self, store: Store, handler: PaperHandler) -> None:
        a = _seed(store, "splitnotitle2020", year=2020)
        _body(store, a, 1)
        with pytest.raises(BadInput, match="title"):
            _edit(handler, a, {"split_body": {"year": 2020}, "reason": "x"})

    def test_refuses_bodiless_donor(self, store: Store, handler: PaperHandler) -> None:
        a = _seed(store, "splitempty2020", year=2020)
        with pytest.raises(BadInput, match="no body"):
            _edit(
                handler,
                a,
                {"split_body": {"title": "X"}, "reason": "x"},
            )

    def test_dry_run_writes_nothing(self, store: Store, handler: PaperHandler) -> None:
        a = _seed(store, "splitdry2020", year=2020)
        _body(store, a, 2)
        before = store.fetch_refs_by_ids([a])[a]
        resp = _edit(
            handler,
            a,
            {"split_body": {"title": "New Work", "year": 2021}, "reason": "x"},
            dry_run=True,
        )
        assert "DRY RUN" in resp.body
        assert _ords(store, a) == [0, 1]
        # no new ref minted
        with store.pool.connection() as conn:
            n = conn.execute("SELECT count(*) FROM refs").fetchone()
        assert n is not None and n[0] == 1
        assert store.fetch_refs_by_ids([a])[a].meta == before.meta
        assert _events(store, a, "body_split") == []
        assert _events(store, a, "body_moved") == []

    def test_chunk_links_repointed(self, store: Store, handler: PaperHandler) -> None:
        a = _seed(store, "splitlink2020", year=2020)
        other = _seed(store, "splitlinko2020", year=2020)
        _body(store, a, 2)
        out = store.add_link(
            src_ref_id=a, dst_ref_id=other, relation="related-to", src_pos=1
        )
        resp = _edit(
            handler,
            a,
            {
                "split_body": {"title": "The Real Paper", "doi": "10.4/real"},
                "reason": "x",
            },
        )
        assert "1 chunk link(s) re-pointed" in resp.body
        ev_a = _events(store, a, "body_split")
        b = int(ev_a[0].payload["new_ref_id"])
        new = _chunk_ids(store, b)
        row = _link_row(store, out.id)
        assert (int(row[0]), int(row[1]), int(row[2]), row[3]) == (
            b,
            new[1],
            other,
            None,
        )
        assert ev_a[0].payload["chunk_links_repointed"] == 1

    def test_trust_v1_link_repointed(self, store: Store, handler: PaperHandler) -> None:
        """A version-1 trust link on the donor's chunk survives a split
        under the door's computed write context."""
        a = _seed(store, "splittrust2020", year=2020)
        mem = _memory_ref(store)
        _body(store, a, 2)
        link = _v1_link(store, src_ref_id=mem, dst_ref_id=a, dst_pos=1)
        assert _link_stamp(store, link.id) == ("generated", "hunch", 1)
        resp = _edit(
            handler,
            a,
            {"split_body": {"title": "The Real Paper"}, "reason": "x"},
        )
        assert "1 chunk link(s) re-pointed" in resp.body
        ev_a = _events(store, a, "body_split")
        b = int(ev_a[0].payload["new_ref_id"])
        new = _chunk_ids(store, b)
        row = _link_row(store, link.id)
        assert (int(row[0]), row[1], int(row[2]), int(row[3])) == (
            mem,
            None,
            b,
            new[1],
        )
        assert _link_stamp(store, link.id) == ("generated", "hunch", 1)

    def test_intx_recheck_refuses_retired_donor(
        self, store: Store, handler: PaperHandler, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A donor retired between probe and tx is caught by the locked
        re-check before any mint."""
        import contextlib

        a = _seed(store, "splitrace2020", year=2020)
        _body(store, a, 1)
        real_tx = store.tx

        @contextlib.contextmanager
        def tx_with_race() -> Any:
            with real_tx() as conn:
                conn.execute(
                    "UPDATE refs SET retired_at = now() WHERE ref_id = %s", (a,)
                )
                yield conn

        monkeypatch.setattr(store, "tx", tx_with_race)
        with pytest.raises(BadInput, match="retired or merged") as exc:
            _edit(
                handler,
                a,
                {"split_body": {"title": "New Work"}, "reason": "x"},
            )
        assert exc.value.next is not None
        # rolled back: no mint, the donor is live again
        with store.pool.connection() as conn:
            n = conn.execute("SELECT count(*) FROM refs").fetchone()
        assert n is not None and n[0] == 1
        live = store.fetch_refs_by_ids([a])
        assert live[a].retired_at is None
        assert _ords(store, a) == [0]

    def test_missing_reason_refused(self, store: Store, handler: PaperHandler) -> None:
        a = _seed(store, "splitnr2020", year=2020)
        _body(store, a, 1)
        with pytest.raises(BadInput, match="reason"):
            _edit(handler, a, {"split_body": {"title": "X"}})

    def test_rollback_on_failure(
        self, store: Store, handler: PaperHandler, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """An injected failure mid-tx leaves every table untouched."""
        from precis.ingest import body_ops

        a = _seed(store, "splitfail2020", year=2020)
        _body(store, a, 2)
        _pdf(store, a)
        real_move = body_ops.move_body

        def boom(**kw: Any) -> Any:
            real_move(**kw)
            raise RuntimeError("injected mid-tx failure")

        monkeypatch.setattr(body_ops, "move_body", boom)
        with pytest.raises(RuntimeError, match="injected"):
            _edit(
                handler,
                a,
                {"split_body": {"title": "New Work"}, "reason": "x"},
            )
        # donor unchanged: body, pdf and identifiers all still his; no mint
        assert _ords(store, a) == [0, 1]
        assert _emb_count(store, a) == 2
        assert _pdf_sha(store, a) == _SHA
        with store.pool.connection() as conn:
            n_refs = conn.execute("SELECT count(*) FROM refs").fetchone()
            n_ev = conn.execute("SELECT count(*) FROM ref_events").fetchone()
        assert n_refs is not None and n_refs[0] == 1
        assert _events(store, a, "body_split") == []
        assert n_ev is not None and n_ev[0] == 0


class TestLinkStampDoor:
    """gr514995 option A: a chunk re-anchor keeps a link's own trust
    stamp, re-applied through the schema's internal door only when the
    anchored chunk text is provably unchanged."""

    def test_considered_stamp_preserved_on_move(
        self, store: Store, handler: PaperHandler
    ) -> None:
        """A pure move re-applies each link's own prior stamp — a
        ``considered`` link stays considered, and a plainer link does
        not ride up to ``considered`` on a sibling's stamp."""
        from precis.store.revision_context import TrustAct

        a = _seed(store, "mvconsa2020", year=2020)
        b = _seed(store, "mvconsb2020", year=2020)
        mem = _memory_ref(store)
        _body(store, a, 2)
        hunch = _v1_link(store, src_ref_id=mem, dst_ref_id=a, dst_pos=0)
        considered = _v1_link(store, src_ref_id=mem, dst_ref_id=a, dst_pos=1)
        state = store.trust_state("link", considered.id)
        assert state is not None
        store.record_trust_act(
            TrustAct(
                action="consider",
                target_kind="link",
                target_id=considered.id,
                expected_sha=state.sha,
                reason="checked against the pdf",
            ),
            actor="reto",
            actor_type="human",
        )
        assert _link_stamp(store, considered.id) == ("generated", "considered", 1)
        _edit(handler, a, {"move_body_to": f"pa{b}", "reason": "x"})
        # each link re-applied its OWN prior — the hunch link did not
        # take the considered stamp
        assert _link_stamp(store, hunch.id) == ("generated", "hunch", 1)
        assert _link_stamp(store, considered.id) == ("generated", "considered", 1)
        acts = store.trust_acts_for("link", considered.id)
        assert [x["action"] for x in acts] == [
            "create",
            "consider",
            "revise",
            "revise",
        ]

    def test_demotes_when_anchor_text_changed(self, store: Store) -> None:
        """Re-point onto a copy whose text differs keeps the
        ``computed/hunch`` demotion — the door's hash check refuses
        the restore."""
        from precis.ingest import body_ops
        from precis.store.revision_context import revision_context

        a = _seed(store, "mvdeltaa2020", year=2020)
        b = _seed(store, "mvdeltab2020", year=2020)
        mem = _memory_ref(store)
        _body(store, a, 2)
        link = _v1_link(store, src_ref_id=mem, dst_ref_id=a, dst_pos=0)
        # stage "copies" whose text differs from the donor's
        store.chunks.insert_chunks(
            b,
            [ChunkInsert(ord=i, text=f"rewritten block {i}") for i in range(2)],
        )
        with revision_context(trust_origin="computed", trust_actor_type="worker"):
            with store.tx() as conn:
                n = body_ops._repoint_chunk_links(conn, src_ref_id=a, dst_ref_id=b)
        assert n == 1
        new = _chunk_ids(store, b)
        assert int(_link_row(store, link.id)[3]) == new[0]
        # the demotion stands and no restore act was written
        assert _link_stamp(store, link.id) == ("computed", "hunch", 1)
        acts = store.trust_acts_for("link", link.id)
        assert [x["action"] for x in acts] == ["create", "revise"]
        assert acts[-1]["before"] == {
            "origin": "generated",
            "confidence": "hunch",
            "version": 1,
        }
        # and a later transaction cannot retro-apply the stamp — the
        # door only sees its own re-point's revision row
        with store.tx() as conn:
            assert body_ops._restore_link_stamp(conn, link.id) is False
        assert _link_stamp(store, link.id) == ("computed", "hunch", 1)

    def test_audit_act_written_per_use(
        self, store: Store, handler: PaperHandler
    ) -> None:
        """Every use of the door leaves an act row naming it — the
        demote's revise, then the restore's revise via body-anchor."""
        a = _seed(store, "mvaudita2020", year=2020)
        b = _seed(store, "mvauditb2020", year=2020)
        mem = _memory_ref(store)
        _body(store, a, 2)
        link = _v1_link(store, src_ref_id=mem, dst_ref_id=a, dst_pos=0)
        _edit(handler, a, {"move_body_to": f"pa{b}", "reason": "x"})
        acts = store.trust_acts_for("link", link.id)
        assert [x["action"] for x in acts] == ["create", "revise", "revise"]
        demote, restore = acts[-2], acts[-1]
        assert demote["after"] == {
            "origin": "computed",
            "confidence": "hunch",
            "version": 1,
        }
        assert restore["via"] == "body-anchor"
        assert restore["before"] == {
            "origin": "computed",
            "confidence": "hunch",
            "version": 1,
        }
        assert restore["after"] == {
            "origin": "generated",
            "confidence": "hunch",
            "version": 1,
        }

    def test_door_cannot_set_another_stamp(self, store: Store) -> None:
        """The door takes no stamp value and sees only this
        transaction's recorded ``revise`` — on an untouched link it
        does nothing, and mints nothing."""
        from precis.ingest import body_ops

        a = _seed(store, "mvnoopa2020", year=2020)
        mem = _memory_ref(store)
        _body(store, a, 1)
        link = _v1_link(store, src_ref_id=mem, dst_ref_id=a, dst_pos=0)
        with store.tx() as conn:
            assert body_ops._restore_link_stamp(conn, link.id) is False
        assert _link_stamp(store, link.id) == ("generated", "hunch", 1)
        assert [x["action"] for x in store.trust_acts_for("link", link.id)] == [
            "create"
        ]

    def test_rollback_leaves_no_stamp_change_or_act(self, store: Store) -> None:
        """A rollback of the re-point leaves the stamp and the audit
        log exactly as they were — the door writes nothing outside
        the transaction."""
        from precis.ingest import body_ops

        a = _seed(store, "mvrolla2020", year=2020)
        b = _seed(store, "mvrollb2020", year=2020)
        mem = _memory_ref(store)
        _body(store, a, 2)
        link = _v1_link(store, src_ref_id=mem, dst_ref_id=a, dst_pos=0)
        donor_chunks = _chunk_ids(store, a)
        with pytest.raises(RuntimeError, match="roll it back"):
            with store.tx() as conn:
                body_ops.move_body(src_ref_id=a, dst_ref_id=b, conn=conn)
                raise RuntimeError("roll it back")
        row = _link_row(store, link.id)
        assert int(row[3]) == donor_chunks[0]
        assert _link_stamp(store, link.id) == ("generated", "hunch", 1)
        assert [x["action"] for x in store.trust_acts_for("link", link.id)] == [
            "create"
        ]
        assert _ords(store, a) == [0, 1]
        assert _ords(store, b) == []
        with store.pool.connection() as conn:
            n = conn.execute(
                "SELECT count(*) FROM revisions "
                "WHERE target_kind = 'link' AND target_id = %s",
                (link.id,),
            ).fetchone()
        assert n is not None and n[0] == 1  # the create row only

    def test_stamp_restore_failure_keeps_badinput_next(
        self, store: Store, handler: PaperHandler, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A trust-guard refusal on the restore UPDATE must reach the
        caller as the CheckViolation's BadInput + next= — the internal
        door's set_config is transaction-local, so nothing resets it on
        the aborted tx (a finally-block reset would mask the refusal
        behind InFailedSqlTransaction and the next= would be lost)."""
        import contextlib

        a = _seed(store, "mvmaska2020", year=2020)
        b = _seed(store, "mvmaskb2020", year=2020)
        mem = _memory_ref(store)
        _body(store, a, 2)
        link = _v1_link(store, src_ref_id=mem, dst_ref_id=a, dst_pos=0)
        real_tx = store.tx

        @contextlib.contextmanager
        def tx_with_trap() -> Any:
            with real_tx() as conn:
                # Fires only on a stamp-only links UPDATE (endpoints
                # unchanged) — the restore's shape, not the re-point's.
                conn.execute(
                    "CREATE OR REPLACE FUNCTION pg_temp.zz_bodyops_trap() "
                    "RETURNS trigger LANGUAGE plpgsql AS $t$ BEGIN "
                    "IF OLD.src_chunk_id IS NOT DISTINCT FROM NEW.src_chunk_id "
                    "AND OLD.dst_chunk_id IS NOT DISTINCT FROM NEW.dst_chunk_id "
                    "AND OLD.src_ref_id IS NOT DISTINCT FROM NEW.src_ref_id "
                    "AND OLD.dst_ref_id IS NOT DISTINCT FROM NEW.dst_ref_id "
                    "AND (OLD.trust_origin IS DISTINCT FROM NEW.trust_origin "
                    "OR OLD.trust_confidence IS DISTINCT FROM "
                    "NEW.trust_confidence) THEN "
                    "RAISE EXCEPTION 'trust: injected stamp refusal' "
                    "USING ERRCODE = 'check_violation', "
                    "HINT = 'the test next step'; END IF; "
                    "RETURN NEW; END $t$"
                )
                conn.execute(
                    "CREATE TRIGGER zz_bodyops_trap BEFORE UPDATE ON links "
                    "FOR EACH ROW "
                    "EXECUTE FUNCTION pg_temp.zz_bodyops_trap()"
                )
                yield conn

        monkeypatch.setattr(store, "tx", tx_with_trap)
        with pytest.raises(BadInput, match="injected stamp refusal") as exc:
            _edit(handler, a, {"move_body_to": f"pa{b}", "reason": "x"})
        assert exc.value.next == "the test next step"
        # rolled back: donor keeps its body, the link its anchor + stamp
        assert _ords(store, a) == [0, 1]
        assert _ords(store, b) == []
        assert int(_link_row(store, link.id)[2]) == a
        assert _link_stamp(store, link.id) == ("generated", "hunch", 1)

    def test_door_has_no_public_path(self) -> None:
        """The internal door is module-private: unexported, no public
        alias, no stamp parameter to smuggle a value through, and no
        verb/args key reaches it."""
        import inspect

        from precis.handlers import paper as paper_handler
        from precis.ingest import body_ops

        assert "_restore_link_stamp" not in body_ops.__all__
        assert "_restore_link_stamp" not in [
            n for n in dir(body_ops) if not n.startswith("_")
        ]
        sig = inspect.signature(body_ops._restore_link_stamp)
        assert set(sig.parameters) == {"conn", "link_id"}
        assert not any(
            "trust" in a or "stamp" in a for a in paper_handler._EDIT_DOOR_ARGS
        )
        # no MCP path: the handler layer never names the door or the
        # internal GUC it rides on
        handler_src = inspect.getsource(paper_handler)
        assert "_restore_link_stamp" not in handler_src
        assert "trust_internal" not in handler_src


class TestClearAbstract:
    def test_happy_path(self, store: Store, handler: PaperHandler) -> None:
        ref = _seed(
            store,
            "clearabs2020",
            year=2020,
            meta={"abstract": "publisher garbage text"},
        )
        with store.pool.connection() as conn:
            conn.execute(
                "INSERT INTO chunks (ref_id, ord, chunk_kind, text) "
                "VALUES (%s, -1, 'card_abstract', 'publisher garbage text'), "
                "(%s, -2, 'card_combined', 'Title publisher garbage text')",
                (ref, ref),
            )
        resp = _edit(handler, ref, {"clear_abstract": True, "reason": "junk abstract"})
        assert "abstract" not in _meta(store, ref)
        with store.pool.connection() as conn:
            kinds = {
                r[0]
                for r in conn.execute(
                    "SELECT chunk_kind FROM chunks WHERE ref_id = %s", (ref,)
                )
            }
            combined = conn.execute(
                "SELECT text FROM chunks WHERE ref_id = %s "
                "AND chunk_kind = 'card_combined'",
                (ref,),
            ).fetchone()
        assert "card_abstract" not in kinds
        assert combined is not None and "garbage" not in combined[0]
        assert "cleared" in resp.body
        ev = _events(store, ref, "abstract_cleared")
        assert len(ev) == 1
        assert ev[0].payload["reason"] == "junk abstract"
        assert ev[0].payload["card_abstract_removed"] == 1

    def test_retired_card_abstract_untouched(
        self, store: Store, handler: PaperHandler
    ) -> None:
        """The delete matches what the probe and ack count — live
        ``card_abstract`` rows only; an already-retired row stays."""
        ref = _seed(store, "absretmix2020", year=2020, meta={"abstract": "junk"})
        with store.pool.connection() as conn:
            conn.execute(
                "INSERT INTO chunks (ref_id, ord, chunk_kind, text) "
                "VALUES (%s, -1, 'card_abstract', 'live junk'), "
                "(%s, -2, 'card_abstract', 'already retired')",
                (ref, ref),
            )
            conn.execute(
                "UPDATE chunks SET retired_at = now() WHERE ref_id = %s AND ord = -2",
                (ref,),
            )
        resp = _edit(handler, ref, {"clear_abstract": True, "reason": "x"})
        assert "1 card_abstract chunk(s) deleted" in resp.body
        ev = _events(store, ref, "abstract_cleared")
        assert ev[0].payload["card_abstract_removed"] == 1
        with store.pool.connection() as conn:
            rows = conn.execute(
                "SELECT ord, retired_at IS NOT NULL FROM chunks "
                "WHERE ref_id = %s AND chunk_kind = 'card_abstract'",
                (ref,),
            ).fetchall()
        assert [(int(r[0]), r[1]) for r in rows] == [(-2, True)]

    def test_noop_without_abstract(self, store: Store, handler: PaperHandler) -> None:
        ref = _seed(store, "noabstract2020", year=2020)
        resp = _edit(handler, ref, {"clear_abstract": True, "reason": "x"})
        assert "no change" in resp.body

    def test_dry_run_writes_nothing(self, store: Store, handler: PaperHandler) -> None:
        ref = _seed(store, "absdry2020", year=2020, meta={"abstract": "x"})
        resp = _edit(
            handler, ref, {"clear_abstract": True, "reason": "x"}, dry_run=True
        )
        assert "DRY RUN" in resp.body
        assert _meta(store, ref)["abstract"] == "x"

    def test_missing_reason_refused(self, store: Store, handler: PaperHandler) -> None:
        ref = _seed(store, "absnr2020", year=2020, meta={"abstract": "x"})
        with pytest.raises(BadInput, match="reason"):
            _edit(handler, ref, {"clear_abstract": True})

    def test_blank_abstract_kwarg_points_at_door(
        self, store: Store, handler: PaperHandler
    ) -> None:
        ref = _seed(store, "absblank2020", year=2020, meta={"abstract": "x"})
        with pytest.raises(BadInput, match="does not clear") as exc:
            handler.edit(id=ref, abstract="")
        assert "clear_abstract" in str(exc.value.next)
        assert _meta(store, ref)["abstract"] == "x"


class TestDoorDispatch:
    def test_doors_cannot_combine(self, store: Store, handler: PaperHandler) -> None:
        ref = _seed(store, "doorcombo2020", year=2020)
        _body(store, ref, 1)
        with pytest.raises(BadInput, match="cannot be combined"):
            _edit(
                handler, ref, {"drop_body": True, "clear_abstract": True, "reason": "x"}
            )
        with pytest.raises(BadInput, match="cannot be combined"):
            handler.edit(id=ref, title="X", args={"drop_body": True, "reason": "x"})

    def test_unknown_id_notfound(self, handler: PaperHandler) -> None:
        with pytest.raises(NotFound):
            _edit(handler, "pa99999999", {"drop_body": True, "reason": "x"})


def test_move_body_carries_article_start_marker(
    store: Store, handler: PaperHandler
) -> None:
    """gr515439: ``article_start_ord`` names ords in *this* body — it is
    body-owned meta, so a move carries it to the target and strips it
    from the donor."""
    a = _seed(store, "fpdonor2020", meta={"article_start_ord": 2})
    b = _seed(store, "fptarget2020")
    _body(store, a, 3)

    resp = _edit(handler, a, {"move_body_to": f"pa{b}", "reason": "body is B's"})
    assert "moved" in resp.body
    assert _meta(store, b)["article_start_ord"] == 2
    assert "article_start_ord" not in _meta(store, a)


class TestBodyOpTxTimeout:
    """gr516498: the body doors lift the role's defensive
    statement_timeout — the chunk_embeddings copy feeds one HNSW index
    insert per chunk, so a several-thousand-chunk body can exceed the
    120 s agent_rw role bound inside a single statement."""

    def test_body_op_tx_lifts_statement_timeout(self, store: Store) -> None:
        from precis.handlers.paper import _body_op_tx

        with _body_op_tx(store, "split_body") as conn:
            row = conn.execute(
                "SELECT setting FROM pg_settings WHERE name = 'statement_timeout'"
            ).fetchone()
        assert row is not None
        assert int(row[0]) >= 600_000

    def test_body_op_tx_maps_query_canceled_to_upstream(self, store: Store) -> None:
        import psycopg

        from precis.errors import Upstream
        from precis.handlers.paper import _body_op_tx

        with pytest.raises(Upstream, match="statement timeout") as exc:
            with _body_op_tx(store, "split_body"):
                raise psycopg.errors.QueryCanceled(
                    "canceling statement due to statement timeout"
                )
        assert exc.value.next and "retry" in exc.value.next

    @pytest.mark.parametrize("door", ["drop_body", "move_body_to", "split_body"])
    def test_oversized_body_refused_up_front(
        self,
        store: Store,
        handler: PaperHandler,
        monkeypatch: pytest.MonkeyPatch,
        door: str,
    ) -> None:
        """The gripe's second arm: a body too large for even the lifted
        bound is refused at probe time with ``next=``, not left to die
        on the statement timeout mid-transaction."""
        from precis.handlers.paper import _BODY_OP_MAX_CHUNKS
        from precis.ingest import body_ops

        src = _seed(store, f"big{door.replace('_', '')}2020", year=2020)
        _body(store, src, 2)
        tgt = _seed(store, f"big{door.replace('_', '')}tgt2020", year=2020)
        args: dict[str, Any]
        if door == "drop_body":
            args = {"drop_body": True, "reason": "x"}
        elif door == "move_body_to":
            args = {"move_body_to": tgt, "reason": "x"}
        else:
            args = {"split_body": {"title": "Split-off work"}, "reason": "x"}
        monkeypatch.setattr(
            body_ops,
            "probe_body",
            lambda conn, ref_id: body_ops.BodyReport(chunks=_BODY_OP_MAX_CHUNKS + 1),
        )
        # Real run and dry run both refuse — the door can't do this at
        # all, so a dry report promising it would mislead.
        for kw in ({}, {"dry_run": True}):
            with pytest.raises(BadInput, match="body chunk") as exc:
                _edit(handler, src, args, **kw)
            assert exc.value.next is not None
        assert _ords(store, src) == [0, 1]


class TestBodyOpLockContention:
    """gr516772: a second body op on a ref whose row lock another session
    still holds dies on ``lock_timeout`` — the dispatcher's boundary map
    turns it into a typed ``Upstream`` retry refusal, never a raw
    internal error (regression cover for the gripe's claimed raw die)."""

    def test_drop_body_lock_maps_through_dispatch(self, store: Store) -> None:
        import psycopg
        from psycopg.conninfo import make_conninfo

        from precis.config import PrecisConfig
        from precis.dispatch import boot
        from precis.runtime import PrecisRuntime

        ref = _seed(store, "lockheld2020", year=2020)
        _body(store, ref, 2)
        _pdf(store, ref)

        # The contending call runs on a second store whose sessions carry
        # a tight lock_timeout — prod's agent_rw 5 s bound, compressed.
        contending_dsn = make_conninfo(
            store.dsn or "",
            options="-c statement_timeout=120s -c lock_timeout=300ms",
        )
        store2 = Store.connect(contending_dsn, min_size=1, max_size=2)
        try:
            runtime = PrecisRuntime(config=PrecisConfig(), hub=boot(store=store2))
            with psycopg.connect(store.dsn or "") as blocker:
                blocker.execute(
                    "SELECT ref_id FROM refs WHERE ref_id = %s FOR NO KEY UPDATE",
                    (ref,),
                )
                body, is_error = runtime.dispatch_with_status(
                    "edit",
                    {
                        "kind": "paper",
                        "id": f"pa{ref}",
                        "__extras__": {"drop_body": True, "reason": "probe"},
                    },
                )
        finally:
            store2.close()
        assert is_error, body
        assert "[error:Upstream]" in body
        assert "internal error" not in body
        assert "retry" in body
        # The lock-specific boundary-map text — a QueryCanceled row would
        # also render Upstream+retry, so this pins the LockNotAvailable arm.
        assert "locked by another writer" in body


_SHA_B = "b" * 64


def _pdf_id_values(store: Store, ref_id: int) -> list[str]:
    with store.pool.connection() as conn:
        rows = conn.execute(
            "SELECT id_value FROM ref_identifiers WHERE ref_id = %s "
            "AND id_kind = 'pdf_sha256' ORDER BY id_value",
            (ref_id,),
        ).fetchall()
    return [str(r[0]).strip() for r in rows]


def _add_pdf_id(store: Store, ref_id: int, sha: str) -> None:
    with store.pool.connection() as conn:
        conn.execute(
            "INSERT INTO ref_identifiers (id_kind, id_value, ref_id, source) "
            "VALUES ('pdf_sha256', %s, %s, 'test') ON CONFLICT DO NOTHING",
            (sha, ref_id),
        )


def _pdfs_row(store: Store, sha: str, storage: str = "y.pdf") -> None:
    """A corpus-ledger row for ``sha`` — ``refs.pdf_sha256`` FKs to it, so
    a keep_pdf pointer switch/promotion needs it."""
    with store.pool.connection() as conn:
        conn.execute(
            "INSERT INTO pdfs (pdf_sha256, content_hash, page_count, "
            "size_bytes, storage_path) VALUES (%s, %s, 3, 10, %s) "
            "ON CONFLICT DO NOTHING",
            (sha, sha, storage),
        )


def _pdf_pages(store: Store, ref_id: int) -> str | None:
    with store.pool.connection() as conn:
        row = conn.execute(
            "SELECT pdf_pages::text FROM refs WHERE ref_id = %s", (ref_id,)
        ).fetchone()
    return str(row[0]) if row and row[0] is not None else None


def _id_pairs(store: Store, ref_id: int) -> set[tuple[str, str]]:
    with store.pool.connection() as conn:
        rows = conn.execute(
            "SELECT id_kind, id_value FROM ref_identifiers WHERE ref_id = %s",
            (ref_id,),
        ).fetchall()
    return {(str(r[0]), str(r[1])) for r in rows}


class TestDropBodyKeepPdf:
    """gr513878: ``keep_pdf`` drops the wrong body but keeps the stored
    pdf attachment, so the bodiless-heal re-extracts the right file."""

    def test_happy_path(self, store: Store, handler: PaperHandler) -> None:
        ref = _seed(store, "keeppdf2020", year=2020, meta={"source_format": "pdf"})
        _body(store, ref, 3)
        _pdf(store, ref)
        store.set_ref_identifier(ref, "doi", "10.1/abc", source="test")
        store.set_ref_identifier(ref, "content_hash", "f" * 64, source="test")
        store.set_ref_identifier(ref, "paper_id", "doi:10.1/abc", source="test")
        store.set_ref_identifier(ref, "pub_id", "54am6h", source="test")
        resp = _edit(
            handler,
            ref,
            {
                "drop_body": True,
                "keep_pdf": True,
                "reason": "body is another paper's, pdf is right",
            },
        )
        assert _ords(store, ref) == []
        assert _emb_count(store, ref) == 0
        # pointer + its identifier row survive; other content ids dropped
        assert _pdf_sha(store, ref) == _SHA
        assert _pdf_id_values(store, ref) == [_SHA]
        # paper_id/pub_id stay — they name the paper, not the dropped file
        assert _id_kinds(store, ref) == {
            "cite_key",
            "doi",
            "pdf_sha256",
            "paper_id",
            "pub_id",
        }
        assert "source_format" not in _meta(store, ref)
        assert "kept" in resp.body and "bodiless" in resp.body
        ev = _events(store, ref, "body_dropped")
        assert len(ev) == 1
        assert ev[0].payload["pdf_cleared"] is False
        assert ev[0].payload["pdf_kept"] == _SHA
        assert ev[0].payload["pdf_repointed"] is False
        removed = ev[0].payload["identifiers_removed"]
        assert "pdf_sha256:" + _SHA not in removed
        assert "content_hash:" + "f" * 64 in removed
        assert "paper_id:doi:10.1/abc" not in removed
        assert "pub_id:54am6h" not in removed

    def test_kept_ref_is_a_bodiless_heal_candidate(
        self, store: Store, handler: PaperHandler
    ) -> None:
        """The heal's predicate (``pdf_sha256 IS NOT NULL AND NOT
        has_body``) picks the kept ref up — a plain-dropped twin is
        invisible to it."""
        from precis.ingest.paper_hygiene import _bodiless_candidates

        kept = _seed(store, "healkeep2020", year=2020)
        gone = _seed(store, "healgone2020", year=2020)
        _body(store, kept, 1)
        _pdf(store, kept)
        _body(store, gone, 1)
        _pdf(store, gone, sha="d" * 64)
        _edit(
            handler,
            kept,
            {"drop_body": True, "keep_pdf": True, "reason": "x"},
        )
        _edit(handler, gone, {"drop_body": True, "reason": "x"})
        ids = {c.ref_id for c in _bodiless_candidates(store, limit=None)}
        assert kept in ids
        assert gone not in ids

    def test_named_sha_picks_current_pointer(
        self, store: Store, handler: PaperHandler
    ) -> None:
        ref = _seed(store, "keeppick2020", year=2020)
        _body(store, ref, 1)
        _pdf(store, ref)
        _add_pdf_id(store, ref, _SHA_B)
        resp = _edit(
            handler,
            ref,
            {
                "drop_body": True,
                "keep_pdf": True,
                "pdf_sha256": _SHA,
                "reason": "keep the ref's own file",
            },
        )
        assert _pdf_sha(store, ref) == _SHA
        assert _pdf_id_values(store, ref) == [_SHA]
        ev = _events(store, ref, "body_dropped")
        assert ev[0].payload["pdf_kept"] == _SHA

    def test_named_sha_switches_pointer(
        self, store: Store, handler: PaperHandler
    ) -> None:
        """Naming an attached sha that is not the pointer re-points
        ``refs.pdf_sha256`` at it and clears the dropped file's span."""
        ref = _seed(store, "keepswitch2020", year=2020)
        _body(store, ref, 1)
        _pdf(store, ref)
        _add_pdf_id(store, ref, _SHA_B)
        _pdfs_row(store, _SHA_B, "clary.pdf")
        with store.pool.connection() as conn:
            conn.execute(
                "UPDATE refs SET pdf_pages = '[1,11)' WHERE ref_id = %s", (ref,)
            )
        resp = _edit(
            handler,
            ref,
            {
                "drop_body": True,
                "keep_pdf": True,
                "pdf_sha256": _SHA_B,
                "reason": "the pointer was the wrong file",
            },
        )
        assert _pdf_sha(store, ref) == _SHA_B
        assert _pdf_pages(store, ref) is None
        assert _pdf_id_values(store, ref) == [_SHA_B]
        # the ack and the event both record the re-point + span clear
        assert "re-pointed" in resp.body and "pdf_pages" in resp.body
        ev = _events(store, ref, "body_dropped")
        assert ev[0].payload["pdf_kept"] == _SHA_B
        assert ev[0].payload["pdf_repointed"] is True

    def test_several_attached_without_name_refused(
        self, store: Store, handler: PaperHandler
    ) -> None:
        ref = _seed(store, "keepamb2020", year=2020)
        _body(store, ref, 1)
        _pdf(store, ref)
        _add_pdf_id(store, ref, _SHA_B)
        with pytest.raises(BadInput, match="pdf_sha256") as exc:
            _edit(
                handler,
                ref,
                {"drop_body": True, "keep_pdf": True, "reason": "x"},
            )
        assert _SHA_B in str(exc.value.next)
        assert _ords(store, ref) == [0]
        assert _pdf_sha(store, ref) == _SHA

    def test_named_sha_not_on_ref_refused(
        self, store: Store, handler: PaperHandler
    ) -> None:
        ref = _seed(store, "keepmiss2020", year=2020)
        _body(store, ref, 1)
        _pdf(store, ref)
        with pytest.raises(BadInput, match="not attached") as exc:
            _edit(
                handler,
                ref,
                {
                    "drop_body": True,
                    "keep_pdf": True,
                    "pdf_sha256": _SHA_B,
                    "reason": "x",
                },
            )
        assert _SHA in str(exc.value.next)
        assert _ords(store, ref) == [0]

    def test_nothing_to_keep_refused(self, store: Store, handler: PaperHandler) -> None:
        ref = _seed(store, "keepnone2020", year=2020)
        _body(store, ref, 1)
        with pytest.raises(BadInput, match="nothing to keep"):
            _edit(
                handler,
                ref,
                {"drop_body": True, "keep_pdf": True, "reason": "x"},
            )
        assert _ords(store, ref) == [0]

    def test_pdf_sha256_without_keep_pdf_refused(
        self, store: Store, handler: PaperHandler
    ) -> None:
        ref = _seed(store, "keepnosha2020", year=2020)
        _body(store, ref, 1)
        _pdf(store, ref)
        with pytest.raises(BadInput, match="keep_pdf") as exc:
            _edit(
                handler,
                ref,
                {"drop_body": True, "pdf_sha256": _SHA, "reason": "x"},
            )
        assert exc.value.next is not None
        assert _pdf_sha(store, ref) == _SHA

    def test_keep_pdf_off_drop_body_refused(
        self, store: Store, handler: PaperHandler
    ) -> None:
        """``keep_pdf`` on any other door — or on no door — must refuse,
        never be silently ignored while the pdf drops."""
        a = _seed(store, "keepoffa2020", year=2020)
        b = _seed(store, "keepoffb2020", year=2020)
        _body(store, a, 1)
        _pdf(store, a)
        with pytest.raises(BadInput, match="drop_body"):
            _edit(
                handler,
                a,
                {
                    "move_body_to": f"pa{b}",
                    "keep_pdf": True,
                    "reason": "x",
                },
            )
        with pytest.raises(BadInput, match="drop_body"):
            _edit(handler, a, {"keep_pdf": True, "reason": "x"})
        assert _ords(store, a) == [0]
        assert _pdf_sha(store, a) == _SHA

    def test_identifier_only_sha_becomes_pointer(
        self, store: Store, handler: PaperHandler
    ) -> None:
        """A pdf_sha256 identifier with no pointer still counts as
        attached — keep_pdf promotes it so the heal can find the file."""
        ref = _seed(store, "keepidonly2020", year=2020)
        _add_pdf_id(store, ref, _SHA)
        _pdfs_row(store, _SHA)
        store.set_ref_identifier(ref, "content_hash", "f" * 64, source="test")
        _edit(
            handler,
            ref,
            {"drop_body": True, "keep_pdf": True, "reason": "x"},
        )
        assert _pdf_sha(store, ref) == _SHA
        assert _pdf_id_values(store, ref) == [_SHA]

    def test_switch_to_unstored_sha_refused(
        self, store: Store, handler: PaperHandler
    ) -> None:
        """A pdf_sha256 identifier without a ``pdfs`` ledger row names
        no stored file — re-pointing at it would violate the FK and
        leave the heal nothing to find."""
        ref = _seed(store, "keepnostore2020", year=2020)
        _body(store, ref, 1)
        _pdf(store, ref)
        _add_pdf_id(store, ref, _SHA_B)  # identifier row only, no pdfs row
        with pytest.raises(BadInput, match="no pdfs row") as exc:
            _edit(
                handler,
                ref,
                {
                    "drop_body": True,
                    "keep_pdf": True,
                    "pdf_sha256": _SHA_B,
                    "reason": "x",
                },
            )
        assert exc.value.next is not None
        assert _ords(store, ref) == [0]
        assert _pdf_sha(store, ref) == _SHA

    def test_dry_run_reports_kept_and_dropped(
        self, store: Store, handler: PaperHandler
    ) -> None:
        ref = _seed(store, "keepdry2020", year=2020)
        _body(store, ref, 2)
        _pdf(store, ref)
        _add_pdf_id(store, ref, _SHA_B)
        store.set_ref_identifier(ref, "content_hash", "f" * 64, source="test")
        resp = _edit(
            handler,
            ref,
            {
                "drop_body": True,
                "keep_pdf": True,
                "pdf_sha256": _SHA,
                "reason": "x",
            },
            dry_run=True,
        )
        assert "DRY RUN" in resp.body
        assert _SHA in resp.body
        assert "kept" in resp.body
        assert f"pdf_sha256:{_SHA_B}" in resp.body
        assert "bodiless-heal" in resp.body
        # nothing written
        assert _ords(store, ref) == [0, 1]
        assert _pdf_sha(store, ref) == _SHA
        assert sorted(_pdf_id_values(store, ref)) == sorted([_SHA, _SHA_B])
        assert _events(store, ref, "body_dropped") == []


class TestDropIdentifier:
    """gr513878: surgical one-row ``ref_identifiers`` removal — the door
    the donor-merge cleanup needed (pubmed/openalex/s2 rows have no
    ``doi=''``-style clear)."""

    def test_happy_path(self, store: Store, handler: PaperHandler) -> None:
        ref = _seed(store, "dropid2020", year=2020)
        store.set_ref_identifier(ref, "pubmed", "24970080", source="test")
        store.set_ref_identifier(ref, "doi", "10.1/abc", source="test")
        resp = _edit(
            handler,
            ref,
            {
                "drop_identifier": {"scheme": "pubmed", "value": "24970080"},
                "reason": "donor's pmid, gr513878",
            },
        )
        assert "removed identifier pubmed:24970080" in resp.body
        assert _id_kinds(store, ref) == {"cite_key", "doi"}
        ev = _events(store, ref, "identifier_dropped")
        assert len(ev) == 1
        payload = ev[0].payload
        assert payload["scheme"] == "pubmed"
        assert payload["value"] == "24970080"
        assert payload["reason"] == "donor's pmid, gr513878"
        # no cleared marker — the scheme stays open for a correct re-add
        assert "cleared_identifiers" not in _meta(store, ref)

    def test_one_of_several_same_scheme(
        self, store: Store, handler: PaperHandler
    ) -> None:
        ref = _seed(store, "dropidtwo2020", year=2020)
        s2a, s2b = "a" * 40, "b" * 40
        store.insert_ref_identifiers(ref, [("s2", s2a, "test"), ("s2", s2b, "test")])
        _edit(
            handler,
            ref,
            {
                "drop_identifier": {"scheme": "s2", "value": s2b},
                "reason": "x",
            },
        )
        pairs = _id_pairs(store, ref)
        assert ("s2", s2b) not in pairs
        assert ("s2", s2a) in pairs

    def test_value_is_normalised(self, store: Store, handler: PaperHandler) -> None:
        ref = _seed(store, "dropidnorm2020", year=2020)
        store.set_ref_identifier(ref, "pubmed", "24970080", source="test")
        _edit(
            handler,
            ref,
            {
                "drop_identifier": {"scheme": "pubmed", "value": "PMID:24970080"},
                "reason": "x",
            },
        )
        assert ("pubmed", "24970080") not in _id_pairs(store, ref)

    def test_string_spec_form(self, store: Store, handler: PaperHandler) -> None:
        ref = _seed(store, "dropidstr2020", year=2020)
        store.set_ref_identifier(ref, "openalex", "w2057701641", source="test")
        resp = _edit(
            handler,
            ref,
            {"drop_identifier": "openalex:W2057701641", "reason": "x"},
        )
        assert "removed identifier openalex:w2057701641" in resp.body
        assert "openalex" not in _id_kinds(store, ref)

    def test_absent_row_refused(self, store: Store, handler: PaperHandler) -> None:
        ref = _seed(store, "dropidmiss2020", year=2020)
        store.set_ref_identifier(ref, "pubmed", "11111111", source="test")
        with pytest.raises(BadInput, match="99999999") as exc:
            _edit(
                handler,
                ref,
                {
                    "drop_identifier": {"scheme": "pubmed", "value": "99999999"},
                    "reason": "x",
                },
            )
        assert "11111111" in str(exc.value.next)
        assert ("pubmed", "11111111") in _id_pairs(store, ref)

    def test_absent_scheme_refused(self, store: Store, handler: PaperHandler) -> None:
        ref = _seed(store, "dropidnone2020", year=2020)
        with pytest.raises(BadInput, match="pubmed") as exc:
            _edit(
                handler,
                ref,
                {
                    "drop_identifier": {"scheme": "pubmed", "value": "24970080"},
                    "reason": "x",
                },
            )
        assert exc.value.next is not None

    def test_cite_key_refused(self, store: Store, handler: PaperHandler) -> None:
        ref = _seed(store, "dropidcite2020", year=2020)
        with pytest.raises(BadInput, match="cite_key"):
            _edit(
                handler,
                ref,
                {
                    "drop_identifier": {
                        "scheme": "cite_key",
                        "value": "dropidcite2020",
                    },
                    "reason": "x",
                },
            )

    def test_bad_spec_shape_refused(self, store: Store, handler: PaperHandler) -> None:
        ref = _seed(store, "dropidshape2020", year=2020)
        with pytest.raises(BadInput, match="scheme"):
            _edit(
                handler,
                ref,
                {"drop_identifier": {"scheme": "pubmed"}, "reason": "x"},
            )
        with pytest.raises(BadInput, match="scheme"):
            _edit(handler, ref, {"drop_identifier": True, "reason": "x"})

    def test_missing_reason_refused(self, store: Store, handler: PaperHandler) -> None:
        ref = _seed(store, "dropidnor2020", year=2020)
        store.set_ref_identifier(ref, "pubmed", "24970080", source="test")
        with pytest.raises(BadInput, match="reason"):
            _edit(
                handler,
                ref,
                {"drop_identifier": {"scheme": "pubmed", "value": "24970080"}},
            )
        assert ("pubmed", "24970080") in _id_pairs(store, ref)

    def test_retired_refused(self, store: Store, handler: PaperHandler) -> None:
        ref = _seed(store, "dropiddead2020", year=2020)
        store.set_ref_identifier(ref, "pubmed", "24970080", source="test")
        store.retire_ref(ref)
        with pytest.raises(BadInput, match="retired"):
            _edit(
                handler,
                ref,
                {
                    "drop_identifier": {"scheme": "pubmed", "value": "24970080"},
                    "reason": "x",
                },
            )
        assert ("pubmed", "24970080") in _id_pairs(store, ref)

    def test_dry_run_writes_nothing(self, store: Store, handler: PaperHandler) -> None:
        ref = _seed(store, "dropiddry2020", year=2020)
        store.set_ref_identifier(ref, "pubmed", "24970080", source="test")
        resp = _edit(
            handler,
            ref,
            {
                "drop_identifier": {"scheme": "pubmed", "value": "24970080"},
                "reason": "x",
            },
            dry_run=True,
        )
        assert "DRY RUN" in resp.body
        assert "pubmed:24970080" in resp.body
        assert ("pubmed", "24970080") in _id_pairs(store, ref)
        assert _events(store, ref, "identifier_dropped") == []

    def test_meta_doi_ghost_removed_even_with_surviving_row(
        self, store: Store, handler: PaperHandler
    ) -> None:
        """Ruling: the displayed meta.doi drops with its row — never
        re-pointed at a surviving row, which could show a different
        wrong DOI."""
        ref = _seed(store, "dropidghost2020", year=2020, meta={"doi": "10.9/bad"})
        store.set_ref_identifier(ref, "doi", "10.9/bad", source="test")
        store.insert_ref_identifiers(ref, [("doi", "10.1/good", "test")])
        _edit(
            handler,
            ref,
            {
                "drop_identifier": {"scheme": "doi", "value": "10.9/bad"},
                "reason": "x",
            },
        )
        assert _id_pairs(store, ref) >= {("doi", "10.1/good")}
        assert ("doi", "10.9/bad") not in _id_pairs(store, ref)
        assert "doi" not in _meta(store, ref)

    def test_mixed_case_stored_row_removable(
        self, store: Store, handler: PaperHandler
    ) -> None:
        """A legacy non-canonical (uppercase) id_value still matches —
        the DELETE compares lower(id_value)."""
        ref = _seed(store, "dropidcase2020", year=2020)
        with store.pool.connection() as conn:
            conn.execute(
                "INSERT INTO ref_identifiers (id_kind, id_value, ref_id, "
                "source) VALUES ('arxiv', 'Astro-PH/0211262', %s, 'test')",
                (ref,),
            )
        _edit(
            handler,
            ref,
            {
                "drop_identifier": {"scheme": "arxiv", "value": "astro-ph/0211262"},
                "reason": "x",
            },
        )
        assert "arxiv" not in _id_kinds(store, ref)

    def test_pointer_pdf_sha256_refused(
        self, store: Store, handler: PaperHandler
    ) -> None:
        """The pointer's own pdf_sha256 row is the body doors' — removing
        it would orphan the attachment."""
        ref = _seed(store, "dropidptr2020", year=2020)
        _pdf(store, ref)
        with pytest.raises(BadInput, match="pdf pointer") as exc:
            _edit(
                handler,
                ref,
                {
                    "drop_identifier": {"scheme": "pdf_sha256", "value": _SHA},
                    "reason": "x",
                },
            )
        assert "keep_pdf" in str(exc.value.next)
        assert ("pdf_sha256", _SHA) in _id_pairs(store, ref)

    def test_nonpointer_pdf_sha256_row_droppable(
        self, store: Store, handler: PaperHandler
    ) -> None:
        ref = _seed(store, "dropidnp2020", year=2020)
        _pdf(store, ref)
        _add_pdf_id(store, ref, _SHA_B)
        _edit(
            handler,
            ref,
            {
                "drop_identifier": {"scheme": "pdf_sha256", "value": _SHA_B},
                "reason": "x",
            },
        )
        assert _pdf_id_values(store, ref) == [_SHA]
        assert _pdf_sha(store, ref) == _SHA

    def test_system_derived_schemes_refused(
        self, store: Store, handler: PaperHandler
    ) -> None:
        """content_hash / paper_id / pub_id are content-derived — the
        body doors own them, drop_identifier refuses."""
        ref = _seed(store, "dropidsys2020", year=2020)
        for scheme, value in (
            ("content_hash", "f" * 64),
            ("paper_id", "doi:10.1/abc"),
            ("pub_id", "54am6h"),
        ):
            store.set_ref_identifier(ref, scheme, value, source="test")
            with pytest.raises(BadInput, match="content-derived"):
                _edit(
                    handler,
                    ref,
                    {
                        "drop_identifier": {"scheme": scheme, "value": value},
                        "reason": "x",
                    },
                )
            assert (scheme, value) in _id_pairs(store, ref)

    def test_keep_pdf_string_literals_parse(
        self, store: Store, handler: PaperHandler
    ) -> None:
        """``keep_pdf`` parses strictly: bool True or truthy literals
        keep; "false"/"0"/"no" mean the plain drop."""
        a = _seed(store, "keepfalse2020", year=2020)
        _body(store, a, 1)
        _pdf(store, a)
        _edit(
            handler,
            a,
            {"drop_body": True, "keep_pdf": "false", "reason": "x"},
        )
        assert _pdf_sha(store, a) is None  # "false" = plain drop
        b = _seed(store, "keeptrue2020", year=2020)
        _body(store, b, 1)
        _pdf(store, b, sha="d" * 64)
        _edit(
            handler,
            b,
            {"drop_body": True, "keep_pdf": "true", "reason": "x"},
        )
        assert _pdf_sha(store, b) == "d" * 64

    def test_meta_doi_ghost_removed_with_last_row(
        self, store: Store, handler: PaperHandler
    ) -> None:
        ref = _seed(store, "dropidlast2020", year=2020, meta={"doi": "10.9/bad"})
        store.set_ref_identifier(ref, "doi", "10.9/bad", source="test")
        _edit(
            handler,
            ref,
            {
                "drop_identifier": {"scheme": "doi", "value": "10.9/bad"},
                "reason": "x",
            },
        )
        assert "doi" not in _id_kinds(store, ref)
        assert "doi" not in _meta(store, ref)


class TestDroppedIdentifierSuppression:
    """gr517591: ``drop_identifier`` stamps
    ``refs.meta.dropped_identifiers.<scheme>.<value>`` — a machine-sourced
    write of the suppressed pair is skipped, an explicit human/agent add
    of the same pair lands and lifts it."""

    def _dropped(self, store: Store, ref_id: int) -> dict[str, Any]:
        return dict(_meta(store, ref_id).get("dropped_identifiers") or {})

    def _drop(self, handler: PaperHandler, ref: int, scheme: str, value: str) -> None:
        _edit(
            handler,
            ref,
            {
                "drop_identifier": {"scheme": scheme, "value": value},
                "reason": "x",
            },
        )

    def test_drop_stamps_marker(self, store: Store, handler: PaperHandler) -> None:
        ref = _seed(store, "dropmark2020", year=2020)
        store.set_ref_identifier(ref, "pubmed", "24970080", source="test")
        self._drop(handler, ref, "pubmed", "24970080")
        mark = self._dropped(store, ref)
        assert set(mark) == {"pubmed"}
        entry = mark["pubmed"]["24970080"]
        assert entry["by"] == "drop_identifier"
        assert entry["at"]

    def test_machine_insert_skipped(self, store: Store, handler: PaperHandler) -> None:
        """The gripe's bug: an enrichment pass re-writing the dropped
        value must not resurrect it."""
        ref = _seed(store, "dropskip2020", year=2020)
        store.set_ref_identifier(ref, "pubmed", "24970080", source="test")
        self._drop(handler, ref, "pubmed", "24970080")
        written = store.insert_ref_identifiers(
            ref, [("pubmed", "24970080", "s2-enrich")]
        )
        assert written == 0
        assert "pubmed" not in _id_kinds(store, ref)
        assert set(self._dropped(store, ref)["pubmed"]) == {"24970080"}

    def test_machine_set_refused(self, store: Store, handler: PaperHandler) -> None:
        ref = _seed(store, "dropsetref2020", year=2020)
        s2v = "a" * 40
        store.set_ref_identifier(ref, "s2", s2v, source="test")
        self._drop(handler, ref, "s2", s2v)
        assert (
            store.set_ref_identifier(ref, "s2", s2v, source="openalex-enrich") is False
        )
        assert ("s2", s2v) not in _id_pairs(store, ref)

    def test_explicit_set_lifts(self, store: Store, handler: PaperHandler) -> None:
        """An agent-sourced re-add of the same pair lands and clears the
        suppression (the ``_HUMAN_EDIT_SOURCES`` rule)."""
        ref = _seed(store, "droplift2020", year=2020)
        store.set_ref_identifier(ref, "pubmed", "24970080", source="test")
        self._drop(handler, ref, "pubmed", "24970080")
        assert (
            store.set_ref_identifier(ref, "pubmed", "24970080", source="mcp-edit")
            is True
        )
        assert ("pubmed", "24970080") in _id_pairs(store, ref)
        assert "dropped_identifiers" not in _meta(store, ref)

    def test_explicit_insert_lifts(self, store: Store, handler: PaperHandler) -> None:
        ref = _seed(store, "dropinslift2020", year=2020)
        store.set_ref_identifier(ref, "pubmed", "24970080", source="test")
        self._drop(handler, ref, "pubmed", "24970080")
        written = store.insert_ref_identifiers(ref, [("pubmed", "24970080", "edit")])
        assert written == 1
        assert ("pubmed", "24970080") in _id_pairs(store, ref)
        assert "dropped_identifiers" not in _meta(store, ref)

    def test_other_value_same_scheme_lands(
        self, store: Store, handler: PaperHandler
    ) -> None:
        """Suppression is per-VALUE: the scheme stays open for a later
        correct write."""
        ref = _seed(store, "dropother2020", year=2020)
        store.set_ref_identifier(ref, "pubmed", "24970080", source="test")
        self._drop(handler, ref, "pubmed", "24970080")
        assert store.set_ref_identifier(ref, "pubmed", "11111111", source="s2") is True
        assert ("pubmed", "11111111") in _id_pairs(store, ref)
        assert set(self._dropped(store, ref)["pubmed"]) == {"24970080"}

    def test_merge_skips_suppressed_loser_row_and_unions_markers(
        self, store: Store
    ) -> None:
        """merge_duplicate: a loser row the survivor suppressed can't
        ride across, and the loser's own dropped markers union onto the
        survivor."""
        from precis.ingest.dedup import merge_duplicate

        survivor = _seed(store, "dropsurv2020", year=2020)
        loser = _seed(store, "droploser2020", year=2020)
        store.set_ref_identifier(survivor, "doi", "10.9/bad", source="test")
        store.drop_ref_identifier(survivor, "doi", "10.9/bad")
        store.set_ref_identifier(survivor, "doi", "10.9/other", source="test")
        store.drop_ref_identifier(survivor, "doi", "10.9/other")
        store.set_ref_identifier(loser, "doi", "10.9/bad", source="test")
        store.set_ref_identifier(loser, "pubmed", "24970080", source="test")
        store.set_ref_identifier(loser, "doi", "10.9/loser", source="test")
        store.drop_ref_identifier(loser, "doi", "10.9/loser")
        s2v = "c" * 40
        store.set_ref_identifier(loser, "s2", s2v, source="test")
        store.drop_ref_identifier(loser, "s2", s2v)
        with store.tx() as conn:
            merge_duplicate(
                store,
                survivor_ref_id=survivor,
                duplicate_ref_id=loser,
                source="test",
                reason="unit-test",
                conn=conn,
            )
        pairs = _id_pairs(store, survivor)
        assert ("doi", "10.9/bad") not in pairs
        assert ("pubmed", "24970080") in pairs
        mark = self._dropped(store, survivor)
        # Same-scheme union keeps BOTH sides' suppressed values — a
        # shallow ``||`` would lose the survivor's "10.9/other".
        assert set(mark["doi"]) == {"10.9/bad", "10.9/other", "10.9/loser"}
        assert set(mark["s2"]) == {s2v}

    def test_legacy_mixed_case_row_suppresses_normalised(
        self, store: Store, handler: PaperHandler
    ) -> None:
        """A legacy non-canonical row (``W…`` openalex — only doi has the
        lowercasing trigger) drops fine, but the marker must record the
        NORMALISED value: a machine write of ``w…`` would miss a
        ``W…``-cased stamp and resurrect the pair."""
        ref = _seed(store, "dropcase2020", year=2020)
        with store.pool.connection() as conn:
            conn.execute(
                "INSERT INTO ref_identifiers (id_kind, id_value, ref_id, source) "
                "VALUES ('openalex', 'W2741809807', %s, 'legacy')",
                (ref,),
            )
        self._drop(handler, ref, "openalex", "W2741809807")
        assert set(self._dropped(store, ref)["openalex"]) == {"w2741809807"}
        written = store.insert_ref_identifiers(
            ref, [("openalex", "w2741809807", "openalex-enrich")]
        )
        assert written == 0
        assert "openalex" not in _id_kinds(store, ref)

    def test_merge_skips_mixed_case_suppressed_loser_row(self, store: Store) -> None:
        """The merge-side twin of the case bug: the loser's mixed-case
        row must be deleted BEFORE migration even outside doi — a
        case-sensitive compare lets it ride across."""
        from precis.ingest.dedup import merge_duplicate

        survivor = _seed(store, "dropcasesurv2020", year=2020)
        loser = _seed(store, "dropcaseloser2020", year=2020)
        store.set_ref_identifier(survivor, "openalex", "w555", source="test")
        store.drop_ref_identifier(survivor, "openalex", "w555")
        with store.pool.connection() as conn:
            conn.execute(
                "INSERT INTO ref_identifiers (id_kind, id_value, ref_id, source) "
                "VALUES ('openalex', 'W555', %s, 'legacy')",
                (loser,),
            )
        with store.tx() as conn:
            merge_duplicate(
                store,
                survivor_ref_id=survivor,
                duplicate_ref_id=loser,
                source="test",
                reason="unit-test",
                conn=conn,
            )
        assert "openalex" not in _id_kinds(store, survivor)
        assert set(self._dropped(store, survivor)["openalex"]) == {"w555"}
