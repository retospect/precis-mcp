"""``get(kind='paper', view='ops')`` + ``precis paper dump`` — the
``precis-paper-dump/1`` export (gr517423, slice 1 of
docs/backlog/paper-backup-pdf-transfer.md).

The dump is the undo artifact the ``drop_body`` / ``move_body_to`` /
``split_body`` / ``clear_abstract`` dry runs point at, and the payload
slice 2's restore door consumes. Tests run against real Postgres —
the ref/identifier/link/card rows a dump must carry are all store-side.

Fixture text is generated at runtime (``test_paper_text_gate``): no
paper prose ever enters the tree.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

import pytest

from precis.dispatch import Hub
from precis.errors import BadInput
from precis.handlers.paper import PaperHandler
from precis.ingest import paper_dump
from precis.store import ChunkInsert, Store
from precis.store.types import Tag


def _seed(store: Store, slug: str, **kw: Any) -> int:
    return store.insert_ref(
        kind="paper",
        slug=slug,
        title=kw.pop("title", f"Title of {slug}"),
        provider="manual",
        meta=kw.pop("meta", {}),
        **kw,
    ).id


def _body(store: Store, ref_id: int, n: int) -> None:
    store.chunks.insert_chunks(
        ref_id,
        [
            ChunkInsert(ord=i, text=f"generated block {i} of ref {ref_id}")
            for i in range(n)
        ],
    )


def _card(store: Store, ref_id: int, *, ord_: int, kind: str, text: str) -> None:
    """A negative-ord card chunk — the shape ``card_abstract`` etc. take
    (``insert_chunks`` is body-only, so seed these directly)."""
    with store.pool.connection() as conn:
        conn.execute(
            "INSERT INTO chunks (ref_id, set_by, ord, chunk_kind, text, "
            "meta) VALUES (%s, 'agent', %s, %s, %s, '{}'::jsonb)",
            (ref_id, ord_, kind, text),
        )


def _handle(ref_id: int) -> str:
    return f"pa{ref_id}"


def _fenced(body: str) -> dict[str, Any]:
    return json.loads(body.split("```json")[1].split("```")[0])


def _all_chunks(store: Store, ref_id: int) -> list[tuple[int, str, str]]:
    """``(ord, chunk_kind, text)`` for every chunk — the recipe the
    manifest's ``body_sha`` documents, applied straight off the rows."""
    with store.pool.connection() as conn:
        rows = conn.execute(
            "SELECT ord, chunk_kind, text FROM chunks WHERE ref_id = %s "
            "ORDER BY ord, chunk_id",
            (ref_id,),
        ).fetchall()
    return [(int(r[0]), str(r[1]), str(r[2])) for r in rows]


def _body_sha_of(rows: list[tuple[int, str, str]]) -> str:
    h = hashlib.sha256()
    for ord_, kind, text in rows:
        h.update(json.dumps([ord_, kind, text], separators=(",", ":")).encode("utf-8"))
        h.update(b"\n")
    return h.hexdigest()


@pytest.fixture
def handler(hub: Hub) -> PaperHandler:
    return PaperHandler(hub=hub)


def _dump_pages(
    handler: PaperHandler, slug: str, page_size: int
) -> list[dict[str, Any]]:
    """Walk the ``next slice:`` calls to exhaustion; return every page."""
    pages: list[dict[str, Any]] = []
    page = 1
    while True:
        body = handler.get(
            id=slug, view="ops", args={"page": page, "page_size": page_size}
        ).body
        payload = _fenced(body)
        pages.append(payload)
        if "next slice:" not in body:
            return pages
        page += 1


class TestDumpShape:
    def test_first_slice_carries_the_whole_ref_state(
        self, store: Store, handler: PaperHandler
    ) -> None:
        ref = _seed(store, "dumpshape2020", year=2020)
        _body(store, ref, 3)
        _card(store, ref, ord_=-1, kind="card_combined", text="card one")
        store.set_ref_identifier(ref, "doi", "10.1/shape", source="test")
        store.add_tag(ref, Tag.open("topic-x"), set_by="agent")
        store.set_paper_authors(
            ref, [{"family": "Curie", "given": "Marie"}], source="human"
        )
        with store.pool.connection() as conn:
            conn.execute(
                "INSERT INTO paper_bib_entries (ref_id, marker, raw_text, "
                "parse_version) VALUES (%s, 1, 'Someone 1999', 1)",
                (ref,),
            )
        body = handler.get(id="dumpshape2020", view="ops").body
        payload = _fenced(body)
        assert payload["format"] == "precis-paper-dump/1"
        assert payload["ref"]["ref_id"] == ref
        assert payload["ref"]["title"] == "Title of dumpshape2020"
        assert payload["manifest"]["ref_id"] == ref
        assert payload["manifest"]["handle"] == _handle(ref)
        assert payload["manifest"]["chunks_total"] == 4
        assert payload["manifest"]["window"] == [0, 4]
        kinds = {c["chunk_kind"] for c in payload["chunks"]}
        assert kinds == {"card_combined", "paragraph"}
        assert payload["chunks"][0]["ord"] == -1
        assert {i["id_kind"] for i in payload["identifiers"]} >= {"doi"}
        assert payload["tags"][0]["value"] == "topic-x"
        assert payload["paper_authors"][0]["family"] == "Curie"
        assert payload["bib_entries"][0]["raw_text"] == "Someone 1999"
        for gap in paper_dump.NOT_CARRIED:
            assert gap in body

    def test_later_slices_carry_manifest_and_chunks_only(
        self, store: Store, handler: PaperHandler
    ) -> None:
        ref = _seed(store, "dumplate2020", year=2020)
        _body(store, ref, 5)
        body = handler.get(
            id="dumplate2020", view="ops", args={"page": 2, "page_size": 3}
        ).body
        payload = _fenced(body)
        assert set(payload) == {"format", "manifest", "chunks"}
        assert payload["manifest"]["window"] == [3, 5]
        assert [c["ord"] for c in payload["chunks"]] == [3, 4]

    def test_slices_concatenate_to_the_full_chunk_list(
        self, store: Store, handler: PaperHandler
    ) -> None:
        """A paper with more than one page of chunks: the paged walk is
        the only way the whole body leaves the corpus."""
        ref = _seed(store, "dumppaged2020", year=2020)
        _body(store, ref, 120)
        _card(store, ref, ord_=-1, kind="card_combined", text="card")
        pages = _dump_pages(handler, "dumppaged2020", page_size=50)
        assert len(pages) == 3
        merged = [c for p in pages for c in p["chunks"]]
        stored = _all_chunks(store, ref)
        assert [(c["ord"], c["chunk_kind"], c["text"]) for c in merged] == stored

    def test_body_sha_matches_the_sql_recompute(
        self, store: Store, handler: PaperHandler
    ) -> None:
        ref = _seed(store, "dumpsha2020", year=2020)
        _body(store, ref, 4)
        _card(store, ref, ord_=-1, kind="card_abstract", text="an abstract card")
        payload = _fenced(handler.get(id="dumpsha2020", view="ops").body)
        assert payload["manifest"]["body_sha"] == _body_sha_of(_all_chunks(store, ref))
        # …and it is stable across slices — every page reports the
        # whole-body checksum, not a per-window one.
        p2 = _fenced(
            handler.get(
                id="dumpsha2020", view="ops", args={"page": 2, "page_size": 3}
            ).body
        )
        assert p2["manifest"]["body_sha"] == payload["manifest"]["body_sha"]

    def test_a_slice_names_its_window_and_hands_the_next_call(
        self, store: Store, handler: PaperHandler
    ) -> None:
        ref = _seed(store, "dumpnext2020", year=2020)
        _body(store, ref, 7)
        body = handler.get(
            id="dumpnext2020", view="ops", args={"page": 2, "page_size": 3}
        ).body
        assert "chunks 3–6" in body
        assert (
            f"next slice: get(kind='paper', id='{_handle(ref)}', view='ops', "
            "args={'page': 3, 'page_size': 3})" in body
        )

    def test_a_chunk_anchored_link_carries_ords_and_the_far_handle(
        self, store: Store, handler: PaperHandler
    ) -> None:
        a = _seed(store, "dumplinks2020", year=2020)
        b = _seed(store, "dumplinkb2020", year=2021)
        _body(store, a, 3)
        _body(store, b, 2)
        store.add_link(
            src_ref_id=a, dst_ref_id=b, relation="cites", src_pos=1, dst_pos=0
        )
        store.add_link(src_ref_id=b, dst_ref_id=a, relation="related-to", dst_pos=2)
        payload = _fenced(handler.get(id="dumplinks2020", view="ops").body)
        (out,) = payload["links_out"]
        assert out["relation"] == "cites"
        assert out["src_chunk_ord"] == 1 and out["dst_chunk_ord"] == 0
        assert out["src_chunk_id"] and out["dst_chunk_id"]
        assert out["dst_ref_handle"] == _handle(b)
        (inc,) = payload["links_in"]
        assert inc["relation"] == "related-to"
        assert inc["dst_chunk_ord"] == 2 and inc["src_chunk_ord"] is None
        assert inc["src_ref_handle"] == _handle(b)

    def test_page_size_above_the_cap_refuses(
        self, store: Store, handler: PaperHandler
    ) -> None:
        _seed(store, "dumpcap2020", year=2020)
        with pytest.raises(BadInput, match="page_size") as exc:
            handler.get(
                id="dumpcap2020",
                view="ops",
                args={"page_size": paper_dump.MAX_PAGE_SIZE + 1},
            )
        assert exc.value.next

    def test_an_offset_past_the_end_is_an_honest_empty_slice(
        self, store: Store, handler: PaperHandler
    ) -> None:
        ref = _seed(store, "dumpempty2020", year=2020)
        _body(store, ref, 2)
        body = handler.get(id="dumpempty2020", view="ops", args={"offset": 99}).body
        assert "past the end" in body
        payload = _fenced(body)
        assert payload["chunks"] == []
        assert payload["manifest"]["window"] == [99, 99]

    def test_deprecated_slice_aliases_still_parse(
        self, store: Store, handler: PaperHandler
    ) -> None:
        ref = _seed(store, "dumpalias2020", year=2020)
        _body(store, ref, 5)
        body = handler.get(
            id="dumpalias2020", view="ops", args={"offset": 2, "limit": 2}
        ).body
        assert "interpreted:" in body
        assert [c["ord"] for c in _fenced(body)["chunks"]] == [2, 3]


class TestUndoLines:
    """Each destructive door's dry run names the export that undoes it."""

    def test_drop_body_dry_run_carries_the_undo_line(
        self, store: Store, handler: PaperHandler
    ) -> None:
        ref = _seed(store, "undodrop2020", year=2020)
        _body(store, ref, 2)
        resp = handler.edit(
            id=ref, args={"drop_body": True, "reason": "x"}, dry_run=True
        )
        assert (
            f"undo: save get(kind='paper', id='{_handle(ref)}', view='ops') "
            "first (1 slice(s) at page_size 100)" in resp.body
        )

    def test_move_body_to_dry_run_carries_the_undo_line(
        self, store: Store, handler: PaperHandler
    ) -> None:
        a = _seed(store, "undomovea2020", year=2020)
        b = _seed(store, "undomoveb2020", year=2020)
        _body(store, a, 150)
        resp = handler.edit(
            id=a,
            args={"move_body_to": f"pa{b}", "reason": "x"},
            dry_run=True,
        )
        # the donor's handle, and the count covers cards + body
        assert (
            f"undo: save get(kind='paper', id='{_handle(a)}', view='ops') "
            "first (2 slice(s) at page_size 100)" in resp.body
        )

    def test_split_body_dry_run_carries_the_undo_line(
        self, store: Store, handler: PaperHandler
    ) -> None:
        ref = _seed(store, "undosplit2020", year=2020)
        _body(store, ref, 2)
        resp = handler.edit(
            id=ref,
            args={"split_body": {"title": "The body's real work"}, "reason": "x"},
            dry_run=True,
        )
        assert (
            f"undo: save get(kind='paper', id='{_handle(ref)}', view='ops') "
            "first" in resp.body
        )

    def test_clear_abstract_dry_run_carries_the_undo_line(
        self, store: Store, handler: PaperHandler
    ) -> None:
        ref = _seed(store, "undoabs2020", year=2020, meta={"abstract": "junk"})
        _body(store, ref, 1)
        resp = handler.edit(
            id=ref, args={"clear_abstract": True, "reason": "x"}, dry_run=True
        )
        assert (
            f"undo: save get(kind='paper', id='{_handle(ref)}', view='ops') "
            "first" in resp.body
        )


class TestCliDump:
    def test_the_cli_merges_every_slice_into_one_dump(
        self, store: Store, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``precis paper dump`` walks the slices at the cap and writes
        the union — same ``format``, one JSON, the whole chunk list."""
        from precis.cli.paper import dump_paper

        monkeypatch.setattr(paper_dump, "MAX_PAGE_SIZE", 50)
        ref = _seed(store, "dumpcli2020", year=2020)
        _body(store, ref, 120)
        _card(store, ref, ord_=-1, kind="card_combined", text="card")
        dump = dump_paper(store, "dumpcli2020")
        assert dump["format"] == "precis-paper-dump/1"
        assert dump["manifest"]["window"] == [0, 121]
        assert len(dump["chunks"]) == 121
        assert dump["manifest"]["body_sha"] == _body_sha_of(_all_chunks(store, ref))
        assert dump["ref"]["ref_id"] == ref


# ── totality: no column may be added without a verdict ──────────────────
#
# The failure this guards is silent and delayed: someone adds a column to
# one of the dumped tables, the dump quietly stops being a faithful copy,
# and no fixture written before the column existed can exercise it — the
# same gap the se export's ``_VERDICTS`` table closes
# (tests/test_se_ops_export.py).
#
# ``"carried"`` the emitted rows name the column (or its listed alias).
# ``"gap"``     deliberately not carried; named in ``NOT_CARRIED``.
_VERDICTS: dict[str, dict[str, str]] = {
    "refs": {c: "carried" for c in paper_dump.REF_COLUMNS},
    "chunks": {
        **{c: "carried" for c in paper_dump.CHUNK_COLUMNS},
        "tsv": "gap",
        "keywords": "gap",
        "keywords_meta": "gap",
    },
    "ref_identifiers": {c: "carried" for c in paper_dump.IDENTIFIER_COLUMNS},
    "ref_tags": {
        # the ref scoping is manifest.ref_id; the tag itself carries
        # (namespace, value), its tag_id staying a local FK
        "ref_id": "carried",
        "tag_id": "gap",
        **{c: "carried" for c in ("set_by", "created_at", "expires_at")},
    },
    "paper_authors": {c: "carried" for c in paper_dump.AUTHOR_COLUMNS},
    "paper_bib_entries": {c: "carried" for c in paper_dump.BIB_COLUMNS},
    "links": {c: "carried" for c in paper_dump.LINK_COLUMNS},
}

#: Column → what its emitted key is called, when it is not the column
#: name verbatim (the far-ref handle joins add ``*_ref_handle`` and the
#: ``*_chunk_ord`` anchors; those are additive, not renames).
_RENAMED: dict[tuple[str, str], tuple[str, ...]] = {
    ("ref_tags", "ref_id"): ("manifest.ref_id",),
}

#: Tables the dump deliberately leaves to the workers, named in
#: ``NOT_CARRIED`` at table granularity.
_GAP_TABLES: tuple[str, ...] = (
    "chunk_embeddings",
    "chunk_summaries",
    "chunk_citations",
    "chunk_blobs",
    "chunk_tags",
    "chunk_events",
    "chunk_review",
)

#: Where each table's rows land in the first-slice payload.
_PAYLOAD_KEY: dict[str, str] = {
    "refs": "ref",
    "chunks": "chunks",
    "ref_identifiers": "identifiers",
    "ref_tags": "tags",
    "paper_authors": "paper_authors",
    "paper_bib_entries": "bib_entries",
    "links": "links_out",
}


def _live_columns(store: Store, table: str) -> set[str]:
    with store.pool.connection() as conn:
        rows = conn.execute(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_name = %s AND table_schema = 'public'",
            (table,),
        ).fetchall()
    return {str(r[0]) for r in rows}


def test_every_column_of_every_dumped_table_has_a_verdict(
    store: Store,
) -> None:
    """Adding a column to a dumped table forces a decision here — carry
    it (extend the module's column list) or name it in NOT_CARRIED."""
    for table, verdicts in _VERDICTS.items():
        live = _live_columns(store, table)
        recorded = set(verdicts)
        assert live == recorded, (
            f"{table} columns changed. For each one decide: carry it in "
            f"precis.ingest.paper_dump (verdict 'carried'), or declare it "
            f"uncarried in NOT_CARRIED (verdict 'gap') — then record it "
            f"in _VERDICTS. added={sorted(live - recorded)} "
            f"removed={sorted(recorded - live)}"
        )


def test_carried_columns_appear_in_the_emitted_rows(
    store: Store, handler: PaperHandler
) -> None:
    ref = _seed(store, "dumpverd2020", year=2020)
    _body(store, ref, 1)
    other = _seed(store, "dumpverdb2020", year=2020)
    store.add_link(src_ref_id=ref, dst_ref_id=other, relation="related-to")
    store.add_tag(ref, Tag.open("verdict-tag"), set_by="agent")
    store.set_ref_identifier(ref, "doi", "10.1/verdict", source="test")
    store.set_paper_authors(ref, [{"family": "Doe", "given": "J"}], source="human")
    with store.pool.connection() as conn:
        conn.execute(
            "INSERT INTO paper_bib_entries (ref_id, marker, raw_text, "
            "parse_version) VALUES (%s, 1, 'Someone 1999', 1)",
            (ref,),
        )
    payload = _fenced(handler.get(id="dumpverd2020", view="ops").body)
    for table, verdicts in _VERDICTS.items():
        rows = payload[_PAYLOAD_KEY[table]]
        rows = rows if isinstance(rows, list) else [rows]
        assert rows, f"{table} emitted no row to check against"
        emitted = set().union(*(set(r) for r in rows))
        for col, verdict in verdicts.items():
            if verdict != "carried":
                continue
            for key in _RENAMED.get((table, col), (col,)):
                if "." in key:  # manifest.ref_id — the scoping, not a row key
                    continue
                assert key in emitted, (
                    f"{table}.{col} is 'carried' but no emitted {table} "
                    f"row names {key!r}"
                )


def test_every_declared_gap_is_named_in_not_carried() -> None:
    blob = " ".join(paper_dump.NOT_CARRIED)
    tokens = {
        ("chunks", "tsv"): "tsv",
        ("chunks", "keywords"): "keywords",
        ("chunks", "keywords_meta"): "keywords_meta",
        ("ref_tags", "tag_id"): "tag_id",
    }
    for table, verdicts in _VERDICTS.items():
        for col, verdict in verdicts.items():
            if verdict != "gap":
                continue
            token = tokens[(table, col)]
            assert token in blob, (
                f"{table}.{col} is declared a gap but NOT_CARRIED never "
                f"mentions {token!r}"
            )
    for table in _GAP_TABLES:
        assert table in blob, f"{table} is a gap table but NOT_CARRIED omits it"
