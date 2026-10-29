"""``put(args={'restore': …})`` — the ``precis-paper-dump/1`` restore
door (gr517423, slice 2 of docs/backlog/paper-backup-pdf-transfer.md).

``paper_dump.restore_slice`` is called directly here, inside the test's
own ``store.tx()`` — one transaction per slice, as the handler drives
it. Real Postgres: the per-ord rules, the append-only trigger and the
link remap are all store-side.

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
    with store.pool.connection() as conn:
        conn.execute(
            "INSERT INTO chunks (ref_id, set_by, ord, chunk_kind, text, "
            "meta) VALUES (%s, 'agent', %s, %s, %s, '{}'::jsonb)",
            (ref_id, ord_, kind, text),
        )


def _note(store: Store, ref_id: int, *, ord_: int, text: str) -> None:
    """A ``paper_note`` row — the ord<0 note band (gr521094), same
    shape ``_append_note`` writes."""
    with store.pool.connection() as conn:
        conn.execute(
            "INSERT INTO chunks (ref_id, set_by, ord, chunk_kind, text, "
            "meta) VALUES (%s, 'agent', %s, 'paper_note', %s, '{}'::jsonb)",
            (ref_id, ord_, text),
        )


def _all_chunks(store: Store, ref_id: int) -> list[tuple[int, str, str]]:
    """``(ord, chunk_kind, text)`` for every chunk — the natural key a
    restore round-trips against (chunk_ids themselves do not survive)."""
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


def _dump(store: Store, ref_id: int) -> dict[str, Any]:
    """A whole-ref dump as one slice (window [0, total]) — what the CLI
    merge emits and what a small ref's restore sends in one call."""
    with store.pool.connection() as conn:
        return paper_dump.dump_ref(conn, ref_id, offset=0, size=0)


def _restore(store: Store, dump: dict[str, Any], **kw: Any) -> paper_dump.RestoreReport:
    with store.tx() as conn:
        return paper_dump.restore_slice(conn, dump, **kw)


def _drop_body(store: Store, ref_id: int) -> None:
    """The store-level half of ``drop_body``: delete the ord>=0 rows
    (the links' FK cascade takes chunk-anchored links with them)."""
    with store.pool.connection() as conn:
        conn.execute("DELETE FROM chunks WHERE ref_id = %s AND ord >= 0", (ref_id,))


def _meta_restore(store: Store, ref_id: int) -> Any:
    with store.pool.connection() as conn:
        row = conn.execute(
            "SELECT meta->'restore' FROM refs WHERE ref_id = %s", (ref_id,)
        ).fetchone()
    return None if row is None else row[0]


def _slice(store: Store, ref_id: int, offset: int, size: int) -> dict[str, Any]:
    """One paged dump slice — the multi-slice tests' unit of sending."""
    with store.pool.connection() as conn:
        return paper_dump.dump_ref(conn, ref_id, offset=offset, size=size)


def _idents(store: Store, ref_id: int) -> list[tuple[str, str]]:
    with store.pool.connection() as conn:
        rows = conn.execute(
            "SELECT id_kind, id_value FROM ref_identifiers WHERE ref_id = %s "
            "ORDER BY id_kind, id_value",
            (ref_id,),
        ).fetchall()
    return [(str(k), str(v)) for k, v in rows]


def _add_ident(store: Store, ref_id: int, kind: str, value: str) -> None:
    with store.pool.connection() as conn:
        conn.execute(
            "INSERT INTO ref_identifiers (id_kind, id_value, ref_id, source) "
            "VALUES (%s, %s, %s, 'manual')",
            (kind, value, ref_id),
        )


def _cite_key_owner(store: Store, slug: str) -> int | None:
    with store.pool.connection() as conn:
        row = conn.execute(
            "SELECT ref_id FROM ref_identifiers WHERE id_kind = 'cite_key' "
            "AND id_value = %s",
            (slug,),
        ).fetchone()
    return None if row is None else int(row[0])


def _hard_delete(store: Store, ref_id: int) -> None:
    """The 'gone' shape a merge/reaper casualty leaves: no refs row at
    all — every ref-keyed row goes with it."""
    with store.pool.connection() as conn:
        conn.execute(
            "DELETE FROM links WHERE src_ref_id = %s OR dst_ref_id = %s",
            (ref_id, ref_id),
        )
        conn.execute("DELETE FROM ref_identifiers WHERE ref_id = %s", (ref_id,))
        conn.execute("DELETE FROM ref_events WHERE ref_id = %s", (ref_id,))
        conn.execute("DELETE FROM chunks WHERE ref_id = %s", (ref_id,))
        conn.execute("DELETE FROM refs WHERE ref_id = %s", (ref_id,))


def _ref_retired(store: Store, ref_id: int) -> bool:
    with store.pool.connection() as conn:
        row = conn.execute(
            "SELECT retired_at IS NOT NULL FROM refs WHERE ref_id = %s", (ref_id,)
        ).fetchone()
    return bool(row and row[0])


@pytest.fixture
def handler(hub: Hub) -> PaperHandler:
    return PaperHandler(hub=hub)


def _dump_chunks(dump: dict[str, Any]) -> list[tuple[int, str, str]]:
    """The dump's chunks as ``(ord, chunk_kind, text)`` — the same
    natural key :func:`_all_chunks` reads off the stored rows."""
    return [
        (int(c["ord"]), str(c["chunk_kind"]), str(c["text"])) for c in dump["chunks"]
    ]


def _events(store: Store, ref_id: int, event: str) -> list[dict[str, Any]]:
    with store.pool.connection() as conn:
        rows = conn.execute(
            "SELECT payload FROM ref_events WHERE ref_id = %s AND event = %s "
            "ORDER BY event_id",
            (ref_id, event),
        ).fetchall()
    return [dict(r[0]) for r in rows]


def _links(
    store: Store, ref_id: int
) -> list[tuple[int, int | None, int, int | None, str]]:
    with store.pool.connection() as conn:
        rows = conn.execute(
            "SELECT l.src_ref_id, sc.ord, l.dst_ref_id, dc.ord, l.relation "
            "FROM links l "
            "LEFT JOIN chunks sc ON sc.chunk_id = l.src_chunk_id "
            "LEFT JOIN chunks dc ON dc.chunk_id = l.dst_chunk_id "
            "WHERE l.src_ref_id = %s OR l.dst_ref_id = %s "
            "ORDER BY l.link_id",
            (ref_id, ref_id),
        ).fetchall()
    return [(int(a), b, int(c), d, str(e)) for a, b, c, d, e in rows]


class TestSameRefRoundTrip:
    def test_single_slice_round_trip(self, store: Store) -> None:
        """dump → drop the body → one restore_slice: every chunk's
        (ord, kind, text) is back, the card kept its row, the sha
        matches and the chunk-anchored link re-points at the new ids."""
        ref = _seed(store, "restrip2020", year=2020)
        other = _seed(store, "restripb2020", year=2021)
        _body(store, ref, 4)
        _body(store, other, 2)
        _card(store, ref, ord_=-1, kind="card_combined", text="card text")
        store.add_link(
            src_ref_id=ref, dst_ref_id=other, relation="cites", src_pos=1, dst_pos=0
        )
        before = _all_chunks(store, ref)
        dump = _dump(store, ref)

        _drop_body(store, ref)
        assert _all_chunks(store, ref) == [c for c in before if c[0] < 0]
        assert _links(store, ref) == []

        report = _restore(store, dump)
        assert report.completed
        assert report.inserted == 4
        assert report.kept == 1  # the surviving card
        assert report.links_written == 1
        assert report.links_skipped == []
        assert _all_chunks(store, ref) == before
        assert _body_sha_of(_all_chunks(store, ref)) == dump["manifest"]["body_sha"]
        # the link is back, anchored on the restored ords — new chunk_ids
        # under the same (ref, ord) coordinates.
        assert _links(store, ref) == [(ref, 1, other, 0, "cites")]
        # progress cleared at completion
        assert _meta_restore(store, ref) is None

    def test_dry_run_writes_nothing(self, store: Store) -> None:
        ref = _seed(store, "resdry2020", year=2020)
        _body(store, ref, 3)
        dump = _dump(store, ref)
        _drop_body(store, ref)
        meta_before = _meta_restore(store, ref)

        report = _restore(store, dump, dry=True)
        assert report.dry
        assert report.inserted == 3
        assert report.completed  # the preview ran the completion path too
        # …but none of it persisted.
        assert _all_chunks(store, ref) == []
        assert _meta_restore(store, ref) == meta_before

    def test_resending_a_finished_slice_is_idempotent(self, store: Store) -> None:
        ref = _seed(store, "residem2020", year=2020)
        other = _seed(store, "residemb2020", year=2020)
        _body(store, ref, 3)
        _body(store, other, 1)
        store.add_link(src_ref_id=ref, dst_ref_id=other, relation="related-to")
        dump = _dump(store, ref)
        _drop_body(store, ref)
        _restore(store, dump)
        first = _all_chunks(store, ref)

        again = _restore(store, dump)
        assert again.completed
        assert again.inserted == 0
        assert again.kept == 3
        # a true no-op: the stored body already is the dump's, so links
        # are not re-traversed — the restored one simply stays.
        assert again.links_written == 0
        assert again.links_kept == 0
        assert _links(store, ref) == [(ref, None, other, None, "related-to")]
        assert _all_chunks(store, ref) == first
        assert _meta_restore(store, ref) is None

    def test_a_ref_with_a_different_body_refuses(self, store: Store) -> None:
        ref = _seed(store, "resbodied2020", year=2020)
        _body(store, ref, 2)
        dump = _dump(store, ref)
        # the ref's body diverges from the dump afterwards
        store.chunks.insert_chunks(
            ref, [ChunkInsert(ord=2, text=f"diverged block of ref {ref}")]
        )
        with pytest.raises(BadInput, match="still has a body") as exc:
            _restore(store, dump)
        assert exc.value.next and "drop_body" in exc.value.next
        assert _meta_restore(store, ref) is None

    def test_a_diverged_note_is_never_replaced(self, store: Store) -> None:
        """gr521094: a live ``paper_note`` is append-only agent
        timeline — the card rule (swap on any difference) must not
        reach it. The keep leaves the completion body_sha diverged, so
        the restore refuses and rolls back: the live note is
        untouched, not silently swapped for the dump's."""
        ref = _seed(store, "resnote2020", year=2020)
        _body(store, ref, 2)
        _note(store, ref, ord_=-1000000, text="note as dumped")
        dump = _dump(store, ref)
        _drop_body(store, ref)
        # The note drifts after the dump — DELETE+INSERT under the same
        # ord, what an out-of-band rewrite looks like.
        with store.pool.connection() as conn:
            conn.execute(
                "DELETE FROM chunks WHERE ref_id = %s AND ord = -1000000",
                (ref,),
            )
            conn.execute(
                "INSERT INTO chunks (ref_id, ord, chunk_kind, text, meta) "
                "VALUES (%s, -1000000, 'paper_note', 'note edited since', "
                "'{}'::jsonb)",
                (ref,),
            )
        with pytest.raises(BadInput, match="body_sha does not match"):
            _restore(store, dump)
        # Rolled back wholesale: the note still carries its live text.
        assert _all_chunks(store, ref) == [
            (-1000000, "paper_note", "note edited since")
        ]

    def test_notes_restore_insert_only_the_missing(self, store: Store) -> None:
        """The flip side: a note the ref lacks IS restored (INSERT),
        an identical live one is kept — the same round-trip rule body
        rows follow, minus the replace."""
        ref = _seed(store, "resnoteb2020", year=2020)
        _body(store, ref, 2)
        _note(store, ref, ord_=-1000000, text="kept note")
        _note(store, ref, ord_=-999999, text="lost note")
        dump = _dump(store, ref)
        _drop_body(store, ref)
        # One note survives, the other is missing — the restore
        # re-adds only the missing one.
        with store.pool.connection() as conn:
            conn.execute(
                "DELETE FROM chunks WHERE ref_id = %s AND ord = -999999",
                (ref,),
            )
        report = _restore(store, dump)
        assert report.completed
        assert report.inserted == 3  # 2 body chunks + the missing note
        assert report.kept == 1
        assert _all_chunks(store, ref) == _dump_chunks(dump)


class TestRestoreDoor:
    """``put(args={'restore': …})`` — the handler wiring over the same
    ``restore_slice``: one transaction per slice, events by the handler,
    ``dry_run=`` previewing under the savepoint."""

    def test_put_restore_writes_chunks_and_events(
        self, store: Store, handler: PaperHandler
    ) -> None:
        ref = _seed(store, "resdoor2020", year=2020)
        _body(store, ref, 3)
        dump = _dump(store, ref)
        _drop_body(store, ref)
        resp = handler.put(
            id=ref,
            args={"restore": dump, "reason": "undo the drop"},
        )
        assert "complete" in resp.body
        assert _all_chunks(store, ref) == _dump_chunks(dump)
        (slice_ev,) = _events(store, ref, "paper_restore_slice")
        assert slice_ev["reason"] == "undo the drop"
        assert slice_ev["inserted"] == 3
        (done_ev,) = _events(store, ref, "paper_restored")
        assert done_ev["chunks"] == 3
        assert done_ev["dump_body_sha"] == dump["manifest"]["body_sha"]

    def test_put_restore_dry_run_writes_nothing(
        self, store: Store, handler: PaperHandler
    ) -> None:
        ref = _seed(store, "resdoordry2020", year=2020)
        _body(store, ref, 2)
        dump = _dump(store, ref)
        _drop_body(store, ref)
        resp = handler.put(
            id=ref,
            args={"restore": dump},
            dry_run=True,
        )
        assert "DRY RUN" in resp.body
        assert _all_chunks(store, ref) == []
        assert _events(store, ref, "paper_restore_slice") == []

    def test_put_restore_refuses_stub_field_combos(
        self, store: Store, handler: PaperHandler
    ) -> None:
        ref = _seed(store, "resdoorcombo2020", year=2020)
        _body(store, ref, 1)
        dump = _dump(store, ref)
        _drop_body(store, ref)
        with pytest.raises(BadInput, match="does not combine"):
            handler.put(
                id=ref,
                doi="10.1/x",
                args={"restore": dump, "reason": "x"},
            )
        assert _all_chunks(store, ref) == []

    def test_put_restore_pin_mismatch_refuses(
        self, store: Store, handler: PaperHandler
    ) -> None:
        ref = _seed(store, "resdoorpin2020", year=2020)
        other = _seed(store, "resdoorpinb2020", year=2020)
        _body(store, ref, 1)
        dump = _dump(store, ref)
        _drop_body(store, ref)
        with pytest.raises(BadInput, match="pins"):
            handler.put(
                id=other,
                args={"restore": dump, "reason": "x"},
            )
        assert _all_chunks(store, ref) == []

    def test_put_restore_needs_a_reason_to_write(
        self, store: Store, handler: PaperHandler
    ) -> None:
        ref = _seed(store, "resdoorwhy2020", year=2020)
        _body(store, ref, 1)
        dump = _dump(store, ref)
        _drop_body(store, ref)
        with pytest.raises(BadInput, match="reason"):
            handler.put(id=ref, args={"restore": dump})
        assert _all_chunks(store, ref) == []

    def test_cli_restore_writes_body_and_events(self, store: Store) -> None:
        """``precis paper restore``'s lib: the merged dump as one slice,
        ``source='cli'`` events — the same two the put door writes."""
        from precis.cli.paper import restore_paper

        ref = _seed(store, "rescli2020", year=2020)
        _body(store, ref, 2)
        dump = _dump(store, ref)
        _drop_body(store, ref)

        report = restore_paper(store, dump, reason="cli undo", dry=False)
        assert report.completed
        assert _all_chunks(store, ref) == _dump_chunks(dump)
        (slice_ev,) = _events(store, ref, "paper_restore_slice")
        assert slice_ev["reason"] == "cli undo"
        assert slice_ev["inserted"] == 2
        (done_ev,) = _events(store, ref, "paper_restored")
        assert done_ev["dump_body_sha"] == dump["manifest"]["body_sha"]

    def test_cli_restore_dry_writes_nothing(self, store: Store) -> None:
        from precis.cli.paper import restore_paper

        ref = _seed(store, "resclidry2020", year=2020)
        _body(store, ref, 1)
        dump = _dump(store, ref)
        _drop_body(store, ref)

        report = restore_paper(store, dump, dry=True)
        assert report.dry
        assert _all_chunks(store, ref) == []
        assert _events(store, ref, "paper_restore_slice") == []


class TestMintTarget:
    """``target='new'`` — and ``target='same'`` when the dumped ref_id is
    gone — mints a fresh ref row under the dump's cite_key (or
    ``handle=``), then restores the body onto it."""

    def test_mint_new_ref_round_trip(self, store: Store) -> None:
        ref = _seed(store, "resmint2020", year=2020)
        _body(store, ref, 3)
        dump = _dump(store, ref)

        report = _restore(store, dump, target="new", handle="resmintcopy2020")
        assert report.minted
        assert report.slug == "resmintcopy2020"
        assert report.completed
        new_ref = report.ref_id
        assert new_ref != ref
        # the minted ref carries the dump's body and the requested slug;
        # the dumped cite_key stays with the live original.
        assert _all_chunks(store, new_ref) == _dump_chunks(dump)
        assert ("cite_key", "resmintcopy2020") in _idents(store, new_ref)
        assert _cite_key_owner(store, "resmint2020") == ref
        # the original is untouched.
        assert _all_chunks(store, ref) == _dump_chunks(dump)

    def test_chunkless_stub_mints_with_identifiers_and_links(
        self, store: Store
    ) -> None:
        """N2: an empty stored body hashes equal to a chunkless dump's
        sha, so the finished-resend shortcut would return completed=True
        without ever running the identifier/link restore — the mint path
        must not take it."""
        ref = _seed(store, "resstub2020", year=2020)  # a stub: no chunks
        other = _seed(store, "resstubb2020", year=2020)
        _add_ident(store, ref, "doi", "10.5555/stub-rt")
        store.add_link(src_ref_id=ref, dst_ref_id=other, relation="related-to")
        dump = _dump(store, ref)
        assert dump["manifest"]["chunks_total"] == 0
        _hard_delete(store, ref)

        r = _restore(store, dump)
        assert r.minted and r.completed
        assert not _ref_retired(store, r.ref_id)
        assert ("doi", "10.5555/stub-rt") in _idents(store, r.ref_id)
        assert _links(store, r.ref_id) == [(r.ref_id, None, other, None, "related-to")]

    def test_mint_refuses_a_slug_held_by_an_in_flight_mint(self, store: Store) -> None:
        """A minted ref lands retired holding its cite_key — it must not
        be mistaken for a reclaimable retired owner while its own
        restore is still in flight."""
        a = _seed(store, "resinflighta2020", year=2020)
        _body(store, a, 4)
        first = _slice(store, a, 0, 2)
        b = _seed(store, "resinflightb2020", year=2020)
        _body(store, b, 2)
        dump_b = _dump(store, b)

        r1 = _restore(store, first, target="new", handle="rescontested2020")
        assert r1.minted and _ref_retired(store, r1.ref_id)

        with pytest.raises(BadInput, match="in-flight restore") as exc:
            _restore(store, dump_b, target="new", handle="rescontested2020")
        assert exc.value.next and "restore_abort" in exc.value.next

    def test_same_target_mints_when_the_ref_is_gone(self, store: Store) -> None:
        ref = _seed(store, "resgone2020", year=2020)
        _body(store, ref, 3)
        dump = _dump(store, ref)
        _hard_delete(store, ref)

        report = _restore(store, dump)
        assert report.minted
        assert report.completed
        assert report.slug == "resgone2020"
        assert report.ref_id != ref
        assert _cite_key_owner(store, "resgone2020") == report.ref_id
        assert _all_chunks(store, report.ref_id) == _dump_chunks(dump)

    def test_mint_refuses_a_taken_slug(self, store: Store) -> None:
        ref = _seed(store, "restaken2020", year=2020)
        _body(store, ref, 2)
        dump = _dump(store, ref)
        # the dump's cite_key is still owned by the live original.
        with pytest.raises(BadInput, match="already taken") as exc:
            _restore(store, dump, target="new")
        assert exc.value.next and "restaken2020-restored" in exc.value.next
        assert _cite_key_owner(store, "restaken2020") == ref
        # and a slug another live ref owns refuses the same way.
        _seed(store, "restakenb2020", year=2020)
        with pytest.raises(BadInput, match="already taken"):
            _restore(store, dump, target="new", handle="restakenb2020")

    def test_mint_refuses_a_foreign_owned_doi(self, store: Store) -> None:
        ref = _seed(store, "resdoi2020", year=2020)
        _add_ident(store, ref, "doi", "10.5555/restore-test")
        _body(store, ref, 2)
        dump = _dump(store, ref)
        _hard_delete(store, ref)
        other = _seed(store, "resdoib2020", year=2020)
        _add_ident(store, other, "doi", "10.5555/restore-test")

        with pytest.raises(BadInput, match="already belongs to live") as exc:
            _restore(store, dump)
        assert exc.value.next and "get(kind='paper'" in exc.value.next
        # the refusal rolled the mint back: no ref holds the dump's slug
        # and the doi never moved.
        assert _cite_key_owner(store, "resdoi2020") is None
        assert ("doi", "10.5555/restore-test") in _idents(store, other)

    def test_mint_reclaims_an_identifier_from_a_retired_owner(
        self, store: Store
    ) -> None:
        ref = _seed(store, "resrecl2020", year=2020)
        _add_ident(store, ref, "doi", "10.5555/reclaim-test")
        _body(store, ref, 2)
        dump = _dump(store, ref)
        _hard_delete(store, ref)
        other = _seed(store, "resreclb2020", year=2020)
        _add_ident(store, other, "doi", "10.5555/reclaim-test")
        store.retire_ref(other)

        report = _restore(store, dump)
        assert report.minted and report.completed
        assert ("doi", "10.5555/reclaim-test") in _idents(store, report.ref_id)
        assert ("doi", "10.5555/reclaim-test") not in _idents(store, other)

    def test_dry_run_mint_writes_nothing(self, store: Store) -> None:
        ref = _seed(store, "resdrymint2020", year=2020)
        _body(store, ref, 2)
        dump = _dump(store, ref)
        _hard_delete(store, ref)

        report = _restore(store, dump, dry=True)
        assert report.dry and report.minted and report.completed
        # the whole mint preview rolled back: no ref row, no cite_key.
        assert _cite_key_owner(store, "resdrymint2020") is None
        with store.pool.connection() as conn:
            gone = conn.execute(
                "SELECT count(*) FROM refs WHERE ref_id = %s", (report.ref_id,)
            ).fetchone()
            orphans = conn.execute(
                "SELECT count(*) FROM chunks WHERE ref_id = %s", (report.ref_id,)
            ).fetchone()
        assert gone and gone[0] == 0
        assert orphans and orphans[0] == 0


class TestMultiSlice:
    """A ref paged over several dump slices restores in window order,
    tracking progress on ``meta.restore`` between calls."""

    def test_two_slices_progress_then_complete(self, store: Store) -> None:
        ref = _seed(store, "resmulti2020", year=2020)
        _body(store, ref, 5)
        _card(store, ref, ord_=-1, kind="card_combined", text="card")
        other = _seed(store, "resmultib2020", year=2020)
        store.add_link(src_ref_id=ref, dst_ref_id=other, relation="cites")
        first = _slice(store, ref, 0, 3)
        rest = _slice(store, ref, 3, 0)
        before = _all_chunks(store, ref)
        _drop_body(store, ref)

        r1 = _restore(store, first)
        assert not r1.completed
        assert r1.chunks_done == 3
        progress = _meta_restore(store, ref)
        assert progress["chunks_done"] == 3
        assert progress["chunks_total"] == 6

        r2 = _restore(store, rest)
        assert r2.completed
        assert r2.chunks_done == 6
        assert _all_chunks(store, ref) == before
        assert _links(store, ref) == [(ref, None, other, None, "cites")]
        assert _meta_restore(store, ref) is None

    def test_a_later_slice_first_refuses(self, store: Store) -> None:
        ref = _seed(store, "resorder2020", year=2020)
        _body(store, ref, 4)
        later = _slice(store, ref, 2, 0)
        _drop_body(store, ref)
        with pytest.raises(BadInput, match="in flight"):
            _restore(store, later)
        assert _meta_restore(store, ref) is None

    def test_a_slice_jumping_ahead_of_the_frontier_refuses(self, store: Store) -> None:
        ref = _seed(store, "resjump2020", year=2020)
        _body(store, ref, 6)
        slices = [
            _slice(store, ref, 0, 2),
            _slice(store, ref, 2, 2),
            _slice(store, ref, 4, 0),
        ]
        _drop_body(store, ref)
        _restore(store, slices[0])
        with pytest.raises(BadInput, match="out of order") as exc:
            _restore(store, slices[2])
        assert exc.value.next and "starts at 2" in exc.value.next
        # the expected window still lands, then the last slice completes.
        _restore(store, slices[1])
        assert _restore(store, slices[2]).completed

    def test_a_body_ord_rewritten_mid_restore_refuses(self, store: Store) -> None:
        """The per-ord rule near ``chunks_forbid_body_text_update``: a body
        ord that changed between slices refuses — the door never UPDATEs,
        so the trigger is never in play."""
        ref = _seed(store, "resmutate2020", year=2020)
        _body(store, ref, 4)
        first = _slice(store, ref, 0, 2)
        _drop_body(store, ref)
        _restore(store, first)
        # a concurrent write lands DELETE+INSERT (the only legal way to
        # change body text) on an ord the next resend covers.
        with store.pool.connection() as conn:
            conn.execute("DELETE FROM chunks WHERE ref_id = %s AND ord = 1", (ref,))
            conn.execute(
                "INSERT INTO chunks (ref_id, set_by, ord, chunk_kind, text, "
                "meta) VALUES (%s, 'agent', 1, 'paragraph', 'rewritten "
                "while the restore was in flight', '{}'::jsonb)",
                (ref,),
            )
        with pytest.raises(BadInput, match="ord 1 already holds a different"):
            _restore(store, first)
        # the failed resend leaves the frontier where it was.
        assert _meta_restore(store, ref)["chunks_done"] == 2


class TestCardAndRetiredCases:
    """Cards ride along in the same dump: a deleted card comes back, a
    drifted one swaps DELETE+INSERT; a retired ref un-retires."""

    def test_clear_abstract_undo_brings_the_card_back(self, store: Store) -> None:
        ref = _seed(store, "resabs2020", year=2020)
        _body(store, ref, 2)
        _card(store, ref, ord_=-3, kind="card_abstract", text="abstract text")
        dump = _dump(store, ref)
        before = _all_chunks(store, ref)
        with store.pool.connection() as conn:
            conn.execute(
                "DELETE FROM chunks WHERE ref_id = %s AND chunk_kind = 'card_abstract'",
                (ref,),
            )

        report = _restore(store, dump)
        assert report.completed
        assert report.cards == 1
        assert _all_chunks(store, ref) == before

    def test_a_drifted_card_swaps_delete_insert(self, store: Store) -> None:
        ref = _seed(store, "rescardswap2020", year=2020)
        _body(store, ref, 1)
        _card(store, ref, ord_=-1, kind="card_combined", text="dumped card")
        dump = _dump(store, ref)
        with store.pool.connection() as conn:
            # ord < 0 is outside the append-only trigger's scope.
            conn.execute(
                "UPDATE chunks SET text = 'live card rewrite' "
                "WHERE ref_id = %s AND ord = -1",
                (ref,),
            )
        report = _restore(store, dump)
        assert report.completed
        assert report.replaced == 1
        assert report.cards == 1
        assert _all_chunks(store, ref) == _dump_chunks(dump)

    def test_restore_into_a_retired_ref_unretires_it(self, store: Store) -> None:
        ref = _seed(store, "resretired2020", year=2020)
        _body(store, ref, 2)
        dump = _dump(store, ref)
        _drop_body(store, ref)
        store.retire_ref(ref)

        report = _restore(store, dump)
        assert report.unretired
        assert report.completed
        assert not _ref_retired(store, ref)
        assert _all_chunks(store, ref) == _dump_chunks(dump)

    def test_no_embeddings_after_restore(self, store: Store) -> None:
        """Restore never writes the derived tables: the worker pass
        re-earns embeddings on the fresh rows."""
        ref = _seed(store, "resemb2020", year=2020)
        _body(store, ref, 3)
        dump = _dump(store, ref)
        _drop_body(store, ref)
        _restore(store, dump)
        with store.pool.connection() as conn:
            n = conn.execute(
                "SELECT count(*) FROM chunk_embeddings WHERE ref_id = %s",
                (ref,),
            ).fetchone()
            live = conn.execute(
                "SELECT count(*) FROM chunks WHERE ref_id = %s", (ref,)
            ).fetchone()
        assert live and live[0] == 3
        assert n and n[0] == 0


class TestStampedLinks:
    """A v1-stamped link (touching a converted kind) must restamp through
    the trust door, not fail: raw ``trust_*`` values are refused by the
    links guard, and 514 prod papers carry one (gr517423 review,
    finding 1)."""

    def test_stamped_link_restores_and_the_change_is_recorded(
        self, store: Store, handler: PaperHandler
    ) -> None:
        from precis.store.revision_context import revision_context

        ref = _seed(store, "restamp2020", year=2020)
        finding = store.insert_ref(kind="finding", slug=None, title="f", meta={}).id
        _body(store, ref, 2)
        # a human-stamped finding→paper link anchored on the paper's
        # ord 1 — human/hunch v1 because the finding end is converted.
        with revision_context("t", trust_origin="human", trust_actor_type="human"):
            store.add_link(
                src_ref_id=finding,
                dst_ref_id=ref,
                dst_pos=1,
                relation="related-to",
            )
        dump = _dump(store, ref)
        _drop_body(store, ref)  # the cascade takes the chunk-anchored link

        resp = handler.put(id=ref, args={"restore": dump, "reason": "undo the drop"})
        assert "complete" in resp.body
        assert _links(store, ref) == [(finding, None, ref, 1, "related-to")]
        with store.pool.connection() as conn:
            stamp = conn.execute(
                "SELECT trust_origin, trust_confidence, trust_write_version "
                "FROM links WHERE src_ref_id = %s AND dst_ref_id = %s",
                (finding, ref),
            ).fetchone()
        # the door restamps, it does not copy the dump's stamp.
        assert stamp == ("computed", "hunch", 1)
        # …and the event records what the original carried.
        (done_ev,) = _events(store, ref, "paper_restored")
        assert done_ev["restamped"] == 1
        (restamped,) = done_ev["restamped_links"]
        assert restamped["was"] == "human/hunch/v1"
        assert restamped["now"] == "computed/hunch/v1"


class TestRestoreAbort:
    """``put(args={'restore_abort': True})`` — clears ``meta.restore`` so
    an abandoned or superseded-dump restore can start over, and deletes
    the chunk rows the run inserted (``inserted_ords``) so no truncated
    body stays live; a retired target stays retired, a minted one was
    never live (gr517423 review, finding 2 + re-review N1)."""

    def test_abort_clears_the_stamp_and_removes_inserted_chunks(
        self, store: Store, handler: PaperHandler
    ) -> None:
        ref = _seed(store, "resabort2020", year=2020)
        _body(store, ref, 4)
        first = _slice(store, ref, 0, 2)
        _drop_body(store, ref)
        _restore(store, first)
        assert _meta_restore(store, ref)["chunks_done"] == 2

        resp = handler.put(
            id=ref,
            args={"restore_abort": True, "reason": "dump went stale"},
        )
        assert "aborted" in resp.body and "2 chunk(s)" in resp.body
        assert _meta_restore(store, ref) is None
        # no partial body stays — the run's inserted rows are deleted.
        assert _all_chunks(store, ref) == []
        (ev,) = _events(store, ref, "paper_restore_aborted")
        assert ev["chunks_done"] == 2
        assert ev["chunks_total"] == 4
        assert ev["chunks_removed"] == 2
        assert ev["reason"] == "dump went stale"

    def test_abort_with_no_restore_in_flight_refuses(
        self, store: Store, handler: PaperHandler
    ) -> None:
        ref = _seed(store, "resabortb2020", year=2020)
        with pytest.raises(BadInput, match="no restore in flight"):
            handler.put(id=ref, args={"restore_abort": True, "reason": "x"})

    def test_abort_dry_run_keeps_the_stamp(
        self, store: Store, handler: PaperHandler
    ) -> None:
        ref = _seed(store, "resabortdry2020", year=2020)
        _body(store, ref, 4)
        first = _slice(store, ref, 0, 2)
        _drop_body(store, ref)
        _restore(store, first)

        resp = handler.put(
            id=ref,
            args={"restore_abort": True, "reason": "x"},
            dry_run=True,
        )
        assert "DRY RUN" in resp.body
        assert "2 chunk(s) the run inserted would be removed" in resp.body
        assert _meta_restore(store, ref)["chunks_done"] == 2
        assert len(_all_chunks(store, ref)) == 2
        assert _events(store, ref, "paper_restore_aborted") == []

    def test_abort_on_a_minted_restore_leaves_a_retired_stub(
        self, store: Store, handler: PaperHandler
    ) -> None:
        """A mint lands retired and un-retires at completion, so aborting
        one leaves a retired stub with its inserted chunks gone — never
        a live ref wearing a truncated body (N1)."""
        ref = _seed(store, "resabortmint2020", year=2020)
        _body(store, ref, 4)
        first = _slice(store, ref, 0, 2)

        r1 = _restore(store, first, target="new", handle="resabmintb2020")
        assert r1.minted
        assert _ref_retired(store, r1.ref_id)
        assert len(_all_chunks(store, r1.ref_id)) == 2

        resp = handler.put(
            id=r1.ref_id,
            args={"restore_abort": True, "reason": "wrong paper"},
        )
        assert "stays retired" in resp.body
        assert _all_chunks(store, r1.ref_id) == []
        assert _ref_retired(store, r1.ref_id)
        assert _meta_restore(store, r1.ref_id) is None

    def test_a_reexported_dump_refuses_and_names_abort(
        self, store: Store, handler: PaperHandler
    ) -> None:
        ref = _seed(store, "resstale2020", year=2020)
        _body(store, ref, 3)
        stale = _slice(store, ref, 0, 2)
        # the source changed after the dump: a re-export carries a new
        # body_sha for the same ref_id.
        store.chunks.insert_chunks(
            ref, [ChunkInsert(ord=3, text=f"new block of ref {ref}")]
        )
        fresh = _dump(store, ref)
        _drop_body(store, ref)
        _restore(store, stale)

        with pytest.raises(BadInput, match="different restore") as exc:
            _restore(store, fresh)
        assert exc.value.next and "restore_abort" in exc.value.next
        # abort + resend is the way out.
        handler.put(id=ref, args={"restore_abort": True, "reason": "stale dump"})
        assert _restore(store, fresh).completed
        assert _all_chunks(store, ref) == _dump_chunks(fresh)

    def test_a_half_restored_retired_ref_stays_retired(self, store: Store) -> None:
        """The un-retire lands only on the completing slice — an
        abandoned restore of a retired ref can't leave it live with a
        truncated body."""
        ref = _seed(store, "reshalfret2020", year=2020)
        _body(store, ref, 4)
        first = _slice(store, ref, 0, 2)
        rest = _slice(store, ref, 2, 0)
        _drop_body(store, ref)
        store.retire_ref(ref)

        r1 = _restore(store, first)
        assert not r1.unretired
        assert _ref_retired(store, ref)
        r2 = _restore(store, rest)
        assert r2.completed and r2.unretired
        assert not _ref_retired(store, ref)


class TestInFlightPinning:
    """The progress stamp's ``mode``/``slug`` pin a continuation to its
    own run (gr517423 review, finding 3): a ``target='new'`` call never
    joins the original's ``'same'`` restore, and a mismatched
    ``handle=`` refuses instead of silently continuing into a ref
    minted under another name."""

    def test_new_resumes_its_own_minted_ref(self, store: Store) -> None:
        ref = _seed(store, "resnewres2020", year=2020)
        _body(store, ref, 4)
        first = _slice(store, ref, 0, 2)
        rest = _slice(store, ref, 2, 0)
        before = _all_chunks(store, ref)

        r1 = _restore(store, first, target="new", handle="resnewcopy2020")
        assert r1.minted and not r1.completed
        # a mint lands retired; only the completing slice un-retires it.
        assert _ref_retired(store, r1.ref_id)
        progress = _meta_restore(store, r1.ref_id)
        assert progress["mode"] == "new"
        assert progress["slug"] == "resnewcopy2020"

        r2 = _restore(store, rest, target="new")
        assert r2.ref_id == r1.ref_id and r2.completed
        assert not _ref_retired(store, r1.ref_id)
        assert _all_chunks(store, r1.ref_id) == before
        assert _all_chunks(store, ref) == before  # the original is untouched

    def test_new_with_a_different_handle_refuses(self, store: Store) -> None:
        ref = _seed(store, "resnewmm2020", year=2020)
        _body(store, ref, 4)
        first = _slice(store, ref, 0, 2)
        rest = _slice(store, ref, 2, 0)
        r1 = _restore(store, first, target="new", handle="resnewa2020")

        with pytest.raises(BadInput, match="already in flight under slug") as exc:
            _restore(store, rest, target="new", handle="resnewb2020")
        assert exc.value.next and "restore_abort" in exc.value.next
        # the minted ref's frontier is unmoved.
        assert _meta_restore(store, r1.ref_id)["chunks_done"] == 2

    def test_new_does_not_join_a_same_restore_in_flight(self, store: Store) -> None:
        ref = _seed(store, "reshijack2020", year=2020)
        _body(store, ref, 4)
        first = _slice(store, ref, 0, 2)
        _drop_body(store, ref)
        _restore(store, first)  # 'same' now in flight on the original

        r = _restore(store, first, target="new", handle="resownslug2020")
        assert r.minted and r.ref_id != ref
        # the 'same' run is still in flight, unmoved.
        progress = _meta_restore(store, ref)
        assert progress["mode"] == "same"
        assert progress["chunks_done"] == 2


class TestCopySemantics:
    """What a minted copy (``target='new'``) carries and what it leaves
    (gr517423 review, findings 5, 10, 11): byline, tags and the
    retraction state ride along; bib entries do not; identifiers a live
    ref owns and the original's chunk handles stay with the original."""

    def test_minted_skips_identifiers_a_live_ref_owns(self, store: Store) -> None:
        ref = _seed(store, "rescopyskip2020", year=2020)
        _add_ident(store, ref, "doi", "10.5555/copy-skip")
        _body(store, ref, 2)
        dump = _dump(store, ref)

        r = _restore(store, dump, target="new", handle="rescopyb2020")
        assert r.minted and r.completed
        skipped = {(i["id_kind"], i["id_value"]) for i in r.skipped_identifiers}
        assert ("doi", "10.5555/copy-skip") in skipped
        # a copy cannot hold the original's identifiers.
        assert ("doi", "10.5555/copy-skip") not in _idents(store, r.ref_id)
        assert ("doi", "10.5555/copy-skip") in _idents(store, ref)

    def test_minted_drops_chunk_handles(self, store: Store) -> None:
        ref = _seed(store, "reschandle2020", year=2020)
        _body(store, ref, 2)
        with store.pool.connection() as conn:
            conn.execute(
                "UPDATE chunks SET handle = 'ressec-1' WHERE ref_id = %s AND ord = 0",
                (ref,),
            )
        dump = _dump(store, ref)

        r = _restore(store, dump, target="new", handle="reschandleb2020")
        assert r.minted and r.completed
        with store.pool.connection() as conn:
            handles = conn.execute(
                "SELECT handle FROM chunks WHERE ref_id = %s ORDER BY ord",
                (r.ref_id,),
            ).fetchall()
            orig = conn.execute(
                "SELECT handle FROM chunks WHERE ref_id = %s AND ord = 0",
                (ref,),
            ).fetchone()
        # the globally-unique handle stays with the original's rows.
        assert [h[0] for h in handles] == [None, None]
        assert orig and orig[0] == "ressec-1"

    def test_minted_carries_byline_tags_and_retraction_not_bib(
        self, store: Store
    ) -> None:
        from precis.store.types import Tag

        ref = _seed(store, "rescarry2020", year=2020)
        _body(store, ref, 1)
        store.set_paper_authors(
            ref, [{"family": "Curie", "given": "Marie"}], source="human"
        )
        store.add_tag(ref, Tag.open("carry-tag"), set_by="agent")
        store.set_retraction_status(ref, status="retracted", reason="fabricated data")
        with store.pool.connection() as conn:
            conn.execute(
                "INSERT INTO paper_bib_entries (ref_id, marker, raw_text, "
                "parse_version) VALUES (%s, 1, 'Someone 1999', 1)",
                (ref,),
            )
        dump = _dump(store, ref)

        r = _restore(store, dump, target="new", handle="rescarryb2020")
        assert r.minted and r.completed
        with store.pool.connection() as conn:
            authors = conn.execute(
                "SELECT family, given FROM paper_authors WHERE ref_id = %s",
                (r.ref_id,),
            ).fetchall()
            tags = conn.execute(
                "SELECT t.namespace, t.value FROM ref_tags rt "
                "JOIN tags t ON t.tag_id = rt.tag_id WHERE rt.ref_id = %s",
                (r.ref_id,),
            ).fetchall()
            retraction = conn.execute(
                "SELECT retraction_status, retraction_reason FROM refs "
                "WHERE ref_id = %s",
                (r.ref_id,),
            ).fetchone()
            bib = conn.execute(
                "SELECT count(*) FROM paper_bib_entries WHERE ref_id = %s",
                (r.ref_id,),
            ).fetchone()
        assert authors == [("Curie", "Marie")]
        assert ("OPEN", "carry-tag") in tags
        assert retraction == ("retracted", "fabricated data")
        assert bib and bib[0] == 0
        assert r.not_carried == ["1 bib_entry"]

    def test_same_skips_the_dumped_cite_key_on_a_renamed_ref(
        self, store: Store
    ) -> None:
        """R7: renamed after the dump, the ref keeps its new slug — a
        second live cite_key would make ``LIMIT 1`` slug lookups
        nondeterministic."""
        ref = _seed(store, "resrename2020", year=2020)
        _body(store, ref, 2)
        dump = _dump(store, ref)
        with store.pool.connection() as conn:
            conn.execute(
                "DELETE FROM ref_identifiers WHERE ref_id = %s "
                "AND id_kind = 'cite_key'",
                (ref,),
            )
            conn.execute(
                "INSERT INTO ref_identifiers (id_kind, id_value, ref_id, "
                "source) VALUES ('cite_key', 'resrenamed2020', %s, 'manual')",
                (ref,),
            )
        _drop_body(store, ref)

        r = _restore(store, dump)
        assert r.completed
        assert [i["id_value"] for i in r.skipped_identifiers] == ["resrename2020"]
        assert _cite_key_owner(store, "resrenamed2020") == ref
        assert _cite_key_owner(store, "resrename2020") is None


class TestCompletionWiring:
    """What lands at — or is checked before — the completing slice
    (gr517423 review, findings 9, 10, 12, 13): the event counts the
    whole restore, ``parent_chunk_id`` resolves across slices, a dumped
    ``superseded_by`` never merges onto a live ref, and a malformed
    link refuses on slice 0."""

    def test_parent_chunk_id_resolves_across_slices(self, store: Store) -> None:
        ref = _seed(store, "resparent2020", year=2020)
        _body(store, ref, 4)
        with store.pool.connection() as conn:
            parent = conn.execute(
                "SELECT chunk_id FROM chunks WHERE ref_id = %s AND ord = 2",
                (ref,),
            ).fetchone()
            assert parent is not None
            conn.execute(
                "UPDATE chunks SET parent_chunk_id = %s WHERE ref_id = %s AND ord = 0",
                (parent[0], ref),
            )
        first = _slice(store, ref, 0, 2)  # the child; its parent is in `rest`
        rest = _slice(store, ref, 2, 0)
        _drop_body(store, ref)

        _restore(store, first)
        with store.pool.connection() as conn:
            dangling = conn.execute(
                "SELECT parent_chunk_id FROM chunks WHERE ref_id = %s AND ord = 0",
                (ref,),
            ).fetchone()
        assert dangling and dangling[0] is None  # not resolvable yet
        r2 = _restore(store, rest)
        assert r2.completed
        with store.pool.connection() as conn:
            wired = conn.execute(
                "SELECT p.ord FROM chunks c JOIN chunks p "
                "ON p.chunk_id = c.parent_chunk_id "
                "WHERE c.ref_id = %s AND c.ord = 0",
                (ref,),
            ).fetchone()
        assert wired and wired[0] == 2

    def test_paper_restored_counts_the_whole_restore(
        self, store: Store, handler: PaperHandler
    ) -> None:
        ref = _seed(store, "restotals2020", year=2020)
        _body(store, ref, 4)
        _card(store, ref, ord_=-1, kind="card_combined", text="a card")
        _add_ident(store, ref, "doi", "10.5555/totals")
        first = _slice(store, ref, 0, 3)  # card + ords 0-1, + identifiers
        rest = _slice(store, ref, 3, 0)
        _drop_body(store, ref)
        with store.pool.connection() as conn:
            # so the restore re-adds them rather than keeping them.
            conn.execute("DELETE FROM chunks WHERE ref_id = %s AND ord < 0", (ref,))
            conn.execute(
                "DELETE FROM ref_identifiers WHERE ref_id = %s AND id_kind = 'doi'",
                (ref,),
            )

        r1 = _restore(store, first)
        assert r1.cards == 1
        assert r1.identifiers == 1
        handler.put(id=ref, args={"restore": rest, "reason": "finish"})
        (ev,) = _events(store, ref, "paper_restored")
        assert ev["cards"] == 1
        assert ev["identifiers"] == 1

    def test_a_dumped_tombstone_stamp_never_merges_onto_a_live_ref(
        self, store: Store
    ) -> None:
        ref = _seed(store, "ressupmeta2020", year=2020)
        survivor = _seed(store, "ressupmetab2020", year=2020)
        _body(store, ref, 2)
        dump = _dump(store, ref)
        dump["ref"]["meta"]["superseded_by"] = survivor
        _drop_body(store, ref)

        r = _restore(store, dump)
        assert r.completed
        with store.pool.connection() as conn:
            meta = conn.execute(
                "SELECT meta FROM refs WHERE ref_id = %s", (ref,)
            ).fetchone()
        assert meta is not None
        assert "superseded_by" not in dict(meta[0])

    def test_a_malformed_link_refuses_on_slice_0(self, store: Store) -> None:
        ref = _seed(store, "resbadlink2020", year=2020)
        other = _seed(store, "resbadlinkb2020", year=2020)
        _body(store, ref, 4)
        store.add_link(src_ref_id=ref, dst_ref_id=other, relation="cites")
        first = _slice(store, ref, 0, 2)
        first["links_out"] = [{"src_ref_id": ref}]  # no dst_ref_id/relation
        _drop_body(store, ref)

        with pytest.raises(BadInput, match="malformed") as exc:
            _restore(store, first)
        assert exc.value.next and "view='ops'" in exc.value.next
        assert _meta_restore(store, ref) is None
        assert _all_chunks(store, ref) == []


class TestDropBodyDoorRoundTrip:
    """Restore through the real ``edit(drop_body)`` door — it NULLs the
    pdf pointer and deletes the ``pdf_sha256``/``content_hash``
    identifier rows, so the round trip proves the fix: the pdf columns
    and content identifiers come back only while the ``pdfs`` row
    exists (gr517423 review, finding 4)."""

    def test_pdf_pointer_and_identifiers_round_trip(
        self, store: Store, handler: PaperHandler
    ) -> None:
        sha = "ab" * 32
        ref = _seed(store, "respdfrt2020", year=2020)
        _body(store, ref, 2)
        with store.pool.connection() as conn:
            conn.execute(
                "INSERT INTO pdfs (pdf_sha256, content_hash, page_count, "
                "size_bytes, storage_path) VALUES (%s, %s, 3, 10, 'x.pdf') "
                "ON CONFLICT DO NOTHING",
                (sha, sha),
            )
            conn.execute(
                "UPDATE refs SET pdf_sha256 = %s, "
                "pdf_pages = '[2,5)'::int4range, pdf_role = 'main' "
                "WHERE ref_id = %s",
                (sha, ref),
            )
            conn.execute(
                "INSERT INTO ref_identifiers (id_kind, id_value, ref_id, "
                "source) VALUES ('pdf_sha256', %s, %s, 'test') "
                "ON CONFLICT DO NOTHING",
                (sha, ref),
            )
        _add_ident(store, ref, "doi", "10.5555/pdf-rt")
        idents_before = _idents(store, ref)
        dump = _dump(store, ref)

        handler.edit(id=ref, args={"drop_body": True, "reason": "wrong pdf"})
        # the door NULLed the pointer and dropped the content identifiers.
        assert ("pdf_sha256", sha) not in _idents(store, ref)

        r = _restore(store, dump)
        assert r.completed
        with store.pool.connection() as conn:
            pdf = conn.execute(
                "SELECT pdf_sha256, pdf_pages::text, pdf_role FROM refs "
                "WHERE ref_id = %s",
                (ref,),
            ).fetchone()
        assert pdf == (sha, "[2,5)", "main")
        assert _idents(store, ref) == idents_before


class TestRestoreDroppedIdentifierSuppression:
    """gr517591 fix round: a dump restore is NOT an edit — restoring a
    dump taken before a drop must not resurrect the dropped pair,
    whatever ``source`` the dumped identifier row recorded. Only a live
    edit/mcp-edit/web-edit lifts ``meta.dropped_identifiers``."""

    def _mark(self, store: Store, ref_id: int) -> dict[str, Any]:
        with store.pool.connection() as conn:
            row = conn.execute(
                "SELECT meta->'dropped_identifiers' FROM refs WHERE ref_id = %s",
                (ref_id,),
            ).fetchone()
        return dict(row[0]) if row and isinstance(row[0], dict) else {}

    def test_restore_skips_dropped_identifier(self, store: Store) -> None:
        ref = _seed(store, "resdrop2020", year=2020)
        _add_ident(store, ref, "doi", "10.9/resdrop")
        _body(store, ref, 2)
        dump = _dump(store, ref)
        store.drop_ref_identifier(ref, "doi", "10.9/resdrop")
        _drop_body(store, ref)

        report = _restore(store, dump)
        assert report.completed
        assert ("doi", "10.9/resdrop") not in _idents(store, ref)
        assert any(
            s["id_kind"] == "doi" and s["id_value"] == "10.9/resdrop"
            for s in report.skipped_identifiers
        ), report.skipped_identifiers
        assert set(self._mark(store, ref)["doi"]) == {"10.9/resdrop"}

    def test_restore_edit_sourced_row_still_suppressed(self, store: Store) -> None:
        """The ruling case: the dumped row recorded ``source='edit'``
        (the ORIGINAL add was a human edit) — that describes the old
        write, not this restore, so it must not lift the drop."""
        ref = _seed(store, "resdropedit2020", year=2020)
        store.set_ref_identifier(ref, "doi", "10.9/resdropedit", source="edit")
        _body(store, ref, 2)
        dump = _dump(store, ref)
        assert any(
            i["id_kind"] == "doi" and i.get("source") == "edit"
            for i in dump.get("identifiers") or []
        )
        store.drop_ref_identifier(ref, "doi", "10.9/resdropedit")
        _drop_body(store, ref)

        report = _restore(store, dump)
        assert report.completed
        assert ("doi", "10.9/resdropedit") not in _idents(store, ref)
        assert any(
            s["id_kind"] == "doi" and s["id_value"] == "10.9/resdropedit"
            for s in report.skipped_identifiers
        ), report.skipped_identifiers
        assert set(self._mark(store, ref)["doi"]) == {"10.9/resdropedit"}
