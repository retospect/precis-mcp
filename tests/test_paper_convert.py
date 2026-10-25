"""``edit(args={'convert_to': 'datasheet'})`` — paper → datasheet kind flip.

gr515477: datasheets/manuals imported as papers stayed papers — junk
authors, junk-title flags, academic search pollution. The door flips
``refs.kind``; every ``ref_id``-keyed row (body chunks, embeddings,
links incl. chunk anchors, identifiers, pdf, authors, trust stamps)
follows untouched. dry_run previews per-table counts and writes
nothing; SI records, parents with SI children and kind-constrained
links refuse with ``next=``.
"""

from __future__ import annotations

from typing import Any

import pytest

from precis.dispatch import Hub
from precis.errors import BadInput
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


def _emb_kind_count(store: Store, ref_id: int, kind: str) -> int:
    with store.pool.connection() as conn:
        row = conn.execute(
            "SELECT count(*) FROM chunk_embeddings WHERE ref_id = %s AND kind = %s",
            (ref_id, kind),
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


def _kind(store: Store, ref_id: int) -> str:
    with store.pool.connection() as conn:
        row = conn.execute(
            "SELECT kind FROM refs WHERE ref_id = %s", (ref_id,)
        ).fetchone()
    assert row is not None
    return str(row[0])


def _link_row(store: Store, link_id: int) -> Any:
    with store.pool.connection() as conn:
        return conn.execute(
            "SELECT src_ref_id, src_chunk_id, dst_ref_id, dst_chunk_id "
            "FROM links WHERE link_id = %s",
            (link_id,),
        ).fetchone()


def _convert(
    handler: PaperHandler, id: Any, args: dict[str, Any], **kw: Any
) -> Response:
    merged = {"convert_to": "datasheet", "reason": "misfiled", **args}
    return handler.edit(id=id, args=merged, **kw)


@pytest.fixture
def handler(hub: Hub) -> PaperHandler:
    return PaperHandler(hub=hub)


def _chunk_ids(store: Store, ref_id: int) -> dict[int, int]:
    """``ord -> chunk_id`` for the ref's live body chunks."""
    with store.pool.connection() as conn:
        rows = conn.execute(
            "SELECT ord, chunk_id FROM chunks WHERE ref_id = %s "
            "AND ord >= 0 AND retired_at IS NULL",
            (ref_id,),
        ).fetchall()
    return {int(r[0]): int(r[1]) for r in rows}


def _trust(store: Store, ref_id: int) -> tuple[Any, ...]:
    with store.pool.connection() as conn:
        row = conn.execute(
            "SELECT trust_origin, trust_confidence, trust_write_version "
            "FROM refs WHERE ref_id = %s",
            (ref_id,),
        ).fetchone()
    assert row is not None
    return tuple(row)


def _author_count(store: Store, ref_id: int) -> int:
    with store.pool.connection() as conn:
        row = conn.execute(
            "SELECT count(*) FROM paper_authors WHERE ref_id = %s", (ref_id,)
        ).fetchone()
    return int((row or (0,))[0])


def _tag_names(store: Store, ref_id: int) -> set[str]:
    with store.pool.connection() as conn:
        rows = conn.execute(
            "SELECT t.namespace || ':' || t.value FROM ref_tags rt "
            "JOIN tags t ON t.tag_id = rt.tag_id WHERE rt.ref_id = %s",
            (ref_id,),
        ).fetchall()
    return {str(r[0]) for r in rows}


def _add_ref_tag(store: Store, ref_id: int, ns: str, value: str) -> None:
    with store.pool.connection() as conn:
        row = conn.execute(
            "INSERT INTO tags (namespace, value) VALUES (%s, %s) RETURNING tag_id",
            (ns, value),
        ).fetchone()
        assert row is not None
        conn.execute(
            "INSERT INTO ref_tags (ref_id, tag_id, set_by) VALUES (%s, %s, 'agent')",
            (ref_id, int(row[0])),
        )


class TestConvertToDatasheet:
    def test_happy_path(self, store: Store, handler: PaperHandler) -> None:
        ref = _seed(store, "convds2018", year=2018)
        other = _seed(store, "convother2020", year=2020)
        _body(store, ref, 3)
        _pdf(store, ref)
        store.set_ref_identifier(ref, "doi", "10.1/conv", source="test")
        out = store.add_link(
            src_ref_id=ref, dst_ref_id=other, relation="related-to", src_pos=0
        )
        inc = store.add_link(
            src_ref_id=other, dst_ref_id=ref, relation="related-to", dst_pos=1
        )
        with store.pool.connection() as conn:
            conn.execute(
                "INSERT INTO paper_authors (ref_id, position, family, "
                "name_raw, source) VALUES (%s, 1, 'Doe', 'Doe', 'pdf')",
                (ref,),
            )
        anchors = (_link_row(store, out.id), _link_row(store, inc.id))
        # both edges really are chunk-anchored before the convert — the
        # post-convert equality would otherwise compare NULL to NULL
        assert anchors[0][1] is not None
        assert anchors[1][3] is not None
        trust_before = _trust(store, ref)
        chunk_id = _chunk_ids(store, ref)[0]
        resp = _convert(handler, ref, {})
        assert "converted" in resp.body
        # the kind flipped; the cite_key slug resolves as a datasheet
        assert _kind(store, ref) == "datasheet"
        assert store.get_ref(kind="datasheet", id="convds2018") is not None
        assert store.get_ref(kind="paper", id="convds2018") is None
        # the computed handle re-mints under the new kind, and the OLD
        # kind's handle redirects to it via converted_from_kind
        assert store.resolve_handle(f"da{ref}") is not None
        old_ref = store.resolve_handle(f"pa{ref}")
        assert old_ref is not None
        assert old_ref.kind == "datasheet"
        assert old_ref.redirected_from == f"pa{ref}"
        old_chunk = store.resolve_handle(f"pc{chunk_id}")
        assert old_chunk is not None
        assert old_chunk.kind == "datasheet"
        assert old_chunk.chunk_id == chunk_id
        assert old_chunk.redirected_from == f"pc{chunk_id}"
        # body, embeddings, links (incl. chunk anchors), ids, pdf kept
        assert _ords(store, ref) == [0, 1, 2]
        assert _emb_count(store, ref) == 3
        assert _emb_kind_count(store, ref, "datasheet") == 3
        assert _emb_kind_count(store, ref, "paper") == 0
        assert _link_row(store, out.id) == anchors[0]
        assert _link_row(store, inc.id) == anchors[1]
        assert _id_kinds(store, ref) == {"cite_key", "doi", "pdf_sha256"}
        assert _pdf_sha(store, ref) == _SHA
        # authors and trust stamps ride the same row
        assert _author_count(store, ref) == 1
        assert _trust(store, ref) == trust_before
        meta = _meta(store, ref)
        assert meta["subtype"] == "datasheet"
        assert meta["converted_from_kind"] == "paper"
        ev = _events(store, ref, "converted")
        assert len(ev) == 1
        assert ev[0].payload["to_kind"] == "datasheet"
        assert ev[0].payload["body_chunks"] == 3
        assert ev[0].payload["pdf_kept"] is True

    def test_read_via_datasheet_handler(
        self, store: Store, handler: PaperHandler, hub: Hub
    ) -> None:
        from precis.handlers.datasheet import DatasheetHandler

        ref = _seed(store, "convread2020", year=2020)
        _body(store, ref, 2)
        _convert(handler, ref, {})
        resp = DatasheetHandler(hub=hub).get(id="convread2020")
        assert "convread2020" in resp.body or "Title of convread2020" in resp.body

    def test_paper_only_tags_stripped(
        self, store: Store, handler: PaperHandler
    ) -> None:
        """Categorizer axes that apply to paper but not datasheet (JUNK)
        and the dream-acquire stamp (DREAM) go; open tags and the
        multi-kind SRC axis stay. A stripped axis's ``<NS>CASCADE``
        sweep marker goes with it."""
        ref = _seed(store, "convtags2020", year=2020)
        _add_ref_tag(store, ref, "JUNK", "junk-title")
        _add_ref_tag(store, ref, "JUNKCASCADE", "v3")
        _add_ref_tag(store, ref, "DREAM", "acquire")
        _add_ref_tag(store, ref, "SRC", "primary")
        _add_ref_tag(store, ref, "OPEN", "freeform-note")
        dry = _convert(handler, ref, {}, dry_run=True)
        assert "JUNK:junk-title" in dry.body
        assert "JUNKCASCADE:v3" in dry.body
        assert "DREAM:acquire" in dry.body
        # dry run stripped nothing
        assert "JUNK:junk-title" in _tag_names(store, ref)
        resp = _convert(handler, ref, {})
        assert "JUNK:junk-title" in resp.body
        assert _tag_names(store, ref) == {"SRC:primary", "OPEN:freeform-note"}
        ev = _events(store, ref, "converted")
        assert sorted(ev[0].payload["stripped_tags"]) == [
            "DREAM:acquire",
            "JUNK:junk-title",
            "JUNKCASCADE:v3",
        ]

    def test_explicit_kind_get_follows_convert_redirect(
        self, store: Store, runtime_with_store: Any
    ) -> None:
        """``get(kind='paper', id='pa<id>')`` / ``id='pc<chunk>'`` after
        convert: the explicit kind names the handle's decoded — old —
        kind, so dispatch follows the converted_from_kind redirect and
        the datasheet renders, hint included (gr515477 R1). Only the
        runtime's hub is built here: a second hub fixture would rebind
        ``store.hint_bus`` and the hint would land off-bus."""
        ref = _seed(store, "convredir2020", year=2020)
        _body(store, ref, 2)
        chunk_id = _chunk_ids(store, ref)[0]
        _convert(PaperHandler(hub=runtime_with_store.hub), ref, {})
        out = runtime_with_store.dispatch("get", {"kind": "paper", "id": f"pa{ref}"})
        assert "[error:" not in out
        assert "convredir2020" in out or "Title of convredir2020" in out
        assert "converted to" in out
        out = runtime_with_store.dispatch(
            "get", {"kind": "paper", "id": f"pc{chunk_id}"}
        )
        assert "[error:" not in out
        assert "converted to" in out

    def test_merged_handle_to_converted_survivor(
        self, store: Store, handler: PaperHandler
    ) -> None:
        """A merge tombstone whose survivor was later converted still
        resolves — ``pa<loser>`` lands on the datasheet survivor under
        its real kind (gr515477 R2)."""
        survivor = _seed(store, "convsurv2020", year=2020)
        loser = _seed(store, "convloser2020", year=2020)
        store.stamp_ref_meta(loser, {"superseded_by": survivor})
        store.retire_ref(loser)
        _convert(handler, survivor, {})
        resolved = store.resolve_handle(f"pa{loser}")
        assert resolved is not None
        assert resolved.ref_id == survivor
        assert resolved.kind == "datasheet"
        assert resolved.redirected_from == f"pa{loser}"

    def test_tombstone_door_routes_by_handle_kind(
        self, store: Store, runtime_with_store: Any
    ) -> None:
        """``edit(args={'redirect_to': …})`` on a merged ``pa<loser>``
        whose survivor was converted: the door edits the dead paper, so
        dispatch routes the handle's decoded kind ('paper'), not the
        survivor's 'datasheet' (S1)."""
        survivor = _seed(store, "convsurv2030", year=2020)
        new = _seed(store, "convnew2030", year=2020)
        stub = _seed(store, "convstub2030", year=2020)
        store.add_link(
            src_ref_id=survivor,
            dst_ref_id=stub,
            relation="supersedes",
            set_by="system",
        )
        store.stamp_ref_meta(stub, {"superseded_by": survivor})
        store.retire_ref(stub)
        _convert(PaperHandler(hub=runtime_with_store.hub), survivor, {})
        body, is_error = runtime_with_store.dispatch_with_status(
            "edit",
            {
                "kind": "paper",
                "id": f"pa{stub}",
                "__extras__": {
                    "redirect_to": f"pa{new}",
                    "reason": "bad merge",
                },
            },
        )
        assert not is_error, body
        ref = store.fetch_refs_by_ids([stub], include_deleted=True)[stub]
        assert ref.meta["superseded_by"] == new

    def test_unfilled_embedding_row_restamped(
        self, store: Store, handler: PaperHandler
    ) -> None:
        """Prod shape: a chunk_embeddings row still awaiting the 0226
        backfill has ref_id/kind NULL (the filled-check is NOT VALID on
        prod). A validated dev schema can't hold one either — triggers
        and the check are switched off just long enough to write it,
        then put back. The restamp must re-derive ref_id/kind through
        the chunk join, not a ref_id filter."""
        from precis.ingest import kind_convert

        ref = _seed(store, "convnull2020", year=2020)
        _body(store, ref, 2)
        with store.pool.connection() as conn:
            conn.execute("ALTER TABLE chunk_embeddings DISABLE TRIGGER ALL")
            conn.execute(
                "ALTER TABLE chunk_embeddings "
                "DROP CONSTRAINT chunk_embeddings_ref_kind_filled"
            )
            conn.execute(
                "UPDATE chunk_embeddings e SET ref_id = NULL, kind = NULL "
                "FROM chunks c WHERE e.chunk_id = c.chunk_id "
                "AND c.ref_id = %s AND c.ord = 0",
                (ref,),
            )
            row = conn.execute(
                "SELECT e.ref_id, e.kind FROM chunk_embeddings e "
                "JOIN chunks c ON c.chunk_id = e.chunk_id "
                "WHERE c.ref_id = %s AND c.ord = 0",
                (ref,),
            ).fetchone()
            assert row == (None, None)
            # probe counts through the chunk join — the NULL row counts
            probe = kind_convert.probe_convert(conn, ref, "datasheet")
            assert probe.embedding_rows == 2
            applied = kind_convert.apply_convert(
                conn, ref_id=ref, target="datasheet", subtype="datasheet"
            )
            # the refs flip's FK cascade (a parent-side trigger, not
            # affected by the table-level disable) already restamped the
            # filled row; the explicit UPDATE catches exactly the NULL one
            assert applied["embedding_rows"] == 1
            rows = conn.execute(
                "SELECT e.ref_id, e.kind FROM chunk_embeddings e "
                "JOIN chunks c ON c.chunk_id = e.chunk_id "
                "WHERE c.ref_id = %s ORDER BY c.ord",
                (ref,),
            ).fetchall()
            assert rows == [(ref, "datasheet"), (ref, "datasheet")]
            # put the schema back the way the validated test DB had it
            conn.execute("ALTER TABLE chunk_embeddings ENABLE TRIGGER ALL")
            conn.execute(
                "ALTER TABLE chunk_embeddings "
                "ADD CONSTRAINT chunk_embeddings_ref_kind_filled "
                "CHECK (ref_id IS NOT NULL AND kind IS NOT NULL)"
            )
        assert _kind(store, ref) == "datasheet"

    def test_subtype_arg(self, store: Store, handler: PaperHandler) -> None:
        ref = _seed(store, "convsub2021", year=2021)
        _convert(handler, ref, {"subtype": "reference-manual"})
        assert _meta(store, ref)["subtype"] == "reference-manual"

    def test_dry_run_writes_nothing(self, store: Store, handler: PaperHandler) -> None:
        ref = _seed(store, "convdry2022", year=2022)
        _body(store, ref, 2)
        _pdf(store, ref)
        resp = _convert(handler, ref, {}, dry_run=True)
        assert "DRY RUN" in resp.body
        assert "2 chunk(s)" in resp.body
        assert "2 embedding row(s)" in resp.body
        assert _kind(store, ref) == "paper"
        assert _ords(store, ref) == [0, 1]
        assert _emb_kind_count(store, ref, "datasheet") == 0
        assert "subtype" not in _meta(store, ref)
        assert _events(store, ref, "converted") == []

    def test_unsupported_target_refused(
        self, store: Store, handler: PaperHandler
    ) -> None:
        ref = _seed(store, "convtgt2020", year=2020)
        with pytest.raises(BadInput, match="unsupported") as exc:
            handler.edit(id=ref, args={"convert_to": "memory", "reason": "x"})
        assert exc.value.next is not None
        assert _kind(store, ref) == "paper"

    def test_bad_subtype_refused(self, store: Store, handler: PaperHandler) -> None:
        ref = _seed(store, "convbadsub2020", year=2020)
        with pytest.raises(BadInput, match="subtype") as exc:
            _convert(handler, ref, {"subtype": "whitepaper"})
        assert exc.value.next is not None
        assert _kind(store, ref) == "paper"

    def test_missing_reason_refused(self, store: Store, handler: PaperHandler) -> None:
        ref = _seed(store, "convnor2020", year=2020)
        with pytest.raises(BadInput, match="reason") as exc:
            handler.edit(id=ref, args={"convert_to": "datasheet"})
        assert exc.value.next is not None
        assert _kind(store, ref) == "paper"

    def test_retired_refused(self, store: Store, handler: PaperHandler) -> None:
        ref = _seed(store, "convdead2020", year=2020)
        _body(store, ref, 1)
        store.retire_ref(ref)
        with pytest.raises(BadInput, match="retired"):
            _convert(handler, ref, {})
        assert _kind(store, ref) == "paper"
        assert _ords(store, ref) == [0]

    def test_supplement_record_refused(
        self, store: Store, handler: PaperHandler
    ) -> None:
        parent = _seed(store, "convparent2020", year=2020)
        si = _seed(store, "convsi2020", year=2020)
        with store.pool.connection() as conn:
            conn.execute(
                "UPDATE refs SET pdf_role = 'supplement', "
                "meta = meta || jsonb_build_object("
                "'si_parent', jsonb_build_object('ref_id', %s)) "
                "WHERE ref_id = %s",
                (parent, si),
            )
            conn.execute(
                "INSERT INTO links (src_ref_id, dst_ref_id, relation, "
                "set_by, meta) VALUES (%s, %s, 'part-of', 'agent', "
                '\'{"role": "supplement"}\'::jsonb)',
                (si, parent),
            )
        with pytest.raises(BadInput, match="SI record") as exc:
            _convert(handler, si, {})
        assert exc.value.next is not None
        assert _kind(store, si) == "paper"

    def test_parent_with_si_children_refused(
        self, store: Store, handler: PaperHandler
    ) -> None:
        parent = _seed(store, "convwithsi2020", year=2020)
        si = _seed(store, "convsichild2020", year=2020)
        with store.pool.connection() as conn:
            conn.execute(
                "UPDATE refs SET pdf_role = 'supplement' WHERE ref_id = %s",
                (si,),
            )
            conn.execute(
                "INSERT INTO links (src_ref_id, dst_ref_id, relation, "
                "set_by, meta) VALUES (%s, %s, 'part-of', 'agent', "
                '\'{"role": "supplement"}\'::jsonb)',
                (si, parent),
            )
        with pytest.raises(BadInput, match="supplement record") as exc:
            _convert(handler, parent, {})
        assert exc.value.next is not None
        assert _kind(store, parent) == "paper"

    def test_kind_constrained_link_refused(
        self, store: Store, handler: PaperHandler
    ) -> None:
        """A 'contradicts' edge (memory→memory only, migration 0180) on a
        paper is something a datasheet cannot hold — refuse, don't strand
        it violating its own constraint."""
        ref = _seed(store, "convlink2020", year=2020)
        mem = store.insert_ref(kind="memory", slug=None, title="m", meta={}).id
        with store.pool.connection() as conn:
            conn.execute(
                "INSERT INTO links (src_ref_id, dst_ref_id, relation, set_by) "
                "VALUES (%s, %s, 'contradicts', 'agent')",
                (ref, mem),
            )
        with pytest.raises(BadInput, match="contradicts") as exc:
            _convert(handler, ref, {})
        assert exc.value.next is not None
        assert _kind(store, ref) == "paper"

    def test_evidence_link_constraint_passes(
        self, store: Store, handler: PaperHandler
    ) -> None:
        """'establishes' allows 'datasheet' in domain_kinds (0180) — the
        edge converts cleanly."""
        ref = _seed(store, "convest2020", year=2020)
        finding = store.insert_ref(kind="finding", slug=None, title="f", meta={}).id
        with store.pool.connection() as conn:
            conn.execute(
                "INSERT INTO links (src_ref_id, dst_ref_id, relation, set_by) "
                "VALUES (%s, %s, 'establishes', 'agent')",
                (ref, finding),
            )
        _convert(handler, ref, {})
        assert _kind(store, ref) == "datasheet"
        with store.pool.connection() as conn:
            n = conn.execute(
                "SELECT count(*) FROM links WHERE src_ref_id = %s", (ref,)
            ).fetchone()
        assert n is not None and n[0] == 1

    def test_intx_recheck_refuses_kind_drift(
        self, store: Store, handler: PaperHandler, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A kind flip or retire between probe and tx is caught by the
        locked re-check — the convert rolls back."""
        import contextlib

        ref = _seed(store, "convrace2020", year=2020)
        _body(store, ref, 1)
        real_tx = store.tx

        @contextlib.contextmanager
        def tx_with_race() -> Any:
            with real_tx() as conn:
                conn.execute(
                    "UPDATE refs SET retired_at = now() WHERE ref_id = %s",
                    (ref,),
                )
                yield conn

        monkeypatch.setattr(store, "tx", tx_with_race)
        with pytest.raises(BadInput, match="kind or was retired") as exc:
            _convert(handler, ref, {})
        assert exc.value.next is not None
        assert store.fetch_refs_by_ids([ref])[ref].retired_at is None
        assert _events(store, ref, "converted") == []
