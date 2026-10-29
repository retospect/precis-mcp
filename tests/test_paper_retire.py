"""``edit(args={'retire': ...})`` — the phantom-stub retire door.

gr509441: a body-less stub minted by mistake (title-only, no DOI, no
chunks, nothing citing it) had no MCP door to retire — ``delete`` is
Unsupported for papers and ``redirect_to`` only re-points an existing
merge tombstone. These tests pin the door's contract: ``retire: True``
refuses anything that isn't a phantom (body chunks, attached PDF,
external ids, live inbound cites, bib holds, inbound supersedes
redirects) and writes a ``deleted`` ref_event; ``retire: False`` on the
tombstone is the undo (``restored`` ref_event), refusing merge
tombstones.
"""

from __future__ import annotations

from typing import Any

import pytest

from precis.dispatch import Hub
from precis.errors import BadInput, NotFound
from precis.handlers.paper import PaperHandler
from precis.response import Response
from precis.runtime import PrecisRuntime
from precis.store import ChunkInsert, Store


def _seed_paper(store: Store, slug: str) -> int:
    return store.insert_ref(
        kind="paper", slug=slug, title=f"Test paper {slug}", provider="manual", meta={}
    ).id


def _merge(store: Store, loser: int, survivor: int) -> None:
    """Mirror ``ingest/dedup.merge_duplicate``'s tombstone + edge."""
    store.add_link(
        src_ref_id=survivor,
        dst_ref_id=loser,
        relation="supersedes",
        set_by="system",
    )
    store.stamp_ref_meta(loser, {"superseded_by": survivor, "dedup": "test"})
    store.retire_ref(loser)


def _is_retired(store: Store, ref_id: int) -> bool:
    ref = store.fetch_refs_by_ids([ref_id], include_deleted=True)[ref_id]
    return ref.retired_at is not None


def _seed_draft_chunk(store: Store, slug: str, text: str) -> int:
    ref = store.insert_ref(kind="draft", slug=slug, title=f"Draft {slug}")
    store.chunks.insert_chunks(ref.id, [ChunkInsert(ord=0, text=text)])
    return ref.id


def _set_pdf(store: Store, ref_id: int) -> None:
    sha = "ab" * 32
    with store.pool.connection() as conn:
        conn.execute(
            "INSERT INTO pdfs (pdf_sha256, content_hash, page_count, "
            "size_bytes, storage_path) VALUES (%s, %s, 1, 100, '') "
            "ON CONFLICT (pdf_sha256) DO NOTHING",
            (sha, sha),
        )
        conn.execute(
            "UPDATE refs SET pdf_sha256 = %s WHERE ref_id = %s",
            (sha, ref_id),
        )
        conn.commit()


def _seed_bib_hold(store: Store, citing: int, held: int) -> None:
    with store.pool.connection() as conn:
        conn.execute(
            "INSERT INTO paper_bib_entries "
            "(ref_id, marker, raw_text, held_ref_id, parse_version) "
            "VALUES (%s, %s, %s, %s, 1)",
            (citing, 1, "raw", held),
        )
        conn.commit()


@pytest.fixture
def handler(hub: Hub) -> PaperHandler:
    return PaperHandler(hub=hub)


def _retire(handler: PaperHandler, id: str | int, **kw: Any) -> Response:
    return handler.edit(id=id, args={"retire": True, "reason": "phantom"}, **kw)


def _unretire(handler: PaperHandler, id: str | int, **kw: Any) -> Response:
    return handler.edit(id=id, args={"retire": False, "reason": "undo"}, **kw)


class TestRetireHappyPath:
    def test_retires_stub_and_logs_event(
        self, store: Store, handler: PaperHandler
    ) -> None:
        stub = _seed_paper(store, "phantom2020")
        resp = _retire(handler, f"pa{stub}")
        assert _is_retired(store, stub)
        events = [e for e in store.events_for(stub) if e.event == "deleted"]
        assert len(events) == 1
        assert events[0].payload["reason"] == "phantom"
        assert "retire': False" in resp.body

    def test_unretire_restores_and_logs(
        self, store: Store, handler: PaperHandler
    ) -> None:
        stub = _seed_paper(store, "phantom2021")
        _retire(handler, f"pa{stub}")
        resp = _unretire(handler, f"pa{stub}")
        assert not _is_retired(store, stub)
        events = [e for e in store.events_for(stub) if e.event == "restored"]
        assert len(events) == 1
        assert events[0].payload["reason"] == "undo"
        assert "restored" in resp.body

    def test_dry_run_writes_nothing(self, store: Store, handler: PaperHandler) -> None:
        stub = _seed_paper(store, "phantom2022")
        resp = _retire(handler, f"pa{stub}", dry_run=True)
        assert "DRY RUN" in resp.body
        assert not _is_retired(store, stub)
        assert [e for e in store.events_for(stub) if e.event == "deleted"] == []

    def test_unretire_dry_run_writes_nothing(
        self, store: Store, handler: PaperHandler
    ) -> None:
        stub = _seed_paper(store, "phantom2023")
        _retire(handler, f"pa{stub}")
        resp = _unretire(handler, f"pa{stub}", dry_run=True)
        assert "DRY RUN" in resp.body
        assert _is_retired(store, stub)


class TestRetireRefusals:
    def test_body_chunks_refused(self, store: Store, handler: PaperHandler) -> None:
        stub = _seed_paper(store, "bodied2020")
        store.chunks.insert_chunks(stub, [ChunkInsert(ord=0, text="real body")])
        with pytest.raises(BadInput, match="body chunk"):
            _retire(handler, f"pa{stub}")
        assert not _is_retired(store, stub)

    def test_attached_pdf_refused(self, store: Store, handler: PaperHandler) -> None:
        stub = _seed_paper(store, "pdfed2020")
        _set_pdf(store, stub)
        with pytest.raises(BadInput, match="attached PDF"):
            _retire(handler, f"pa{stub}")

    def test_external_id_refused(self, store: Store, handler: PaperHandler) -> None:
        stub = _seed_paper(store, "doied2020")
        store.set_ref_identifier(stub, "doi", "10.1/xyz", source="test")
        with pytest.raises(BadInput, match="external identifier"):
            _retire(handler, f"pa{stub}")

    def test_inbound_cites_edge_refused(
        self, store: Store, handler: PaperHandler
    ) -> None:
        stub = _seed_paper(store, "cited2020")
        citer = _seed_paper(store, "citer2020")
        store.add_link(src_ref_id=citer, dst_ref_id=stub, relation="cites")
        with pytest.raises(BadInput, match="inbound cite edge"):
            _retire(handler, f"pa{stub}")

    def test_retired_citer_does_not_block(
        self, store: Store, handler: PaperHandler
    ) -> None:
        stub = _seed_paper(store, "cited2021")
        citer = _seed_paper(store, "citer2021")
        store.add_link(src_ref_id=citer, dst_ref_id=stub, relation="cites")
        store.retire_ref(citer)
        _retire(handler, f"pa{stub}")
        assert _is_retired(store, stub)

    def test_inbound_supersedes_refused(
        self, store: Store, handler: PaperHandler
    ) -> None:
        survivor = _seed_paper(store, "survivor2020")
        loser = _seed_paper(store, "loser2020")
        _merge(store, loser, survivor)
        with pytest.raises(BadInput, match="redirect here") as exc:
            _retire(handler, f"pa{survivor}")
        assert "redirect_to" in str(exc.value)
        # Once the loser's redirect is moved elsewhere, retire clears.
        other = _seed_paper(store, "other2020")
        handler.edit(
            id=f"pa{loser}",
            args={"redirect_to": f"pa{other}", "reason": "re-point"},
        )
        _retire(handler, f"pa{survivor}")
        assert _is_retired(store, survivor)

    def test_draft_pa_token_refused(self, store: Store, handler: PaperHandler) -> None:
        stub = _seed_paper(store, "tokcite2020")
        _seed_draft_chunk(store, "d2020", f"see the stub [pa{stub}] here")
        with pytest.raises(BadInput, match="cited by token"):
            _retire(handler, f"pa{stub}")

    def test_draft_slug_token_refused(
        self, store: Store, handler: PaperHandler
    ) -> None:
        stub = _seed_paper(store, "slugcite2020")
        _seed_draft_chunk(store, "d2021", "as shown §slugcite2020 earlier")
        with pytest.raises(BadInput, match="cited by token"):
            _retire(handler, stub)

    def test_bib_hold_refused(self, store: Store, handler: PaperHandler) -> None:
        stub = _seed_paper(store, "bibbed2020")
        citer = _seed_paper(store, "bibciter2020")
        _seed_bib_hold(store, citer, stub)
        with pytest.raises(BadInput, match="bib entrie"):
            _retire(handler, f"pa{stub}")

    def test_missing_reason_refused(self, store: Store, handler: PaperHandler) -> None:
        stub = _seed_paper(store, "noreason2020")
        with pytest.raises(BadInput, match="reason"):
            handler.edit(id=f"pa{stub}", args={"retire": True})

    def test_combined_field_refused(self, store: Store, handler: PaperHandler) -> None:
        stub = _seed_paper(store, "combo2020")
        with pytest.raises(BadInput, match="cannot be combined"):
            handler.edit(
                id=f"pa{stub}", year=2001, args={"retire": True, "reason": "x"}
            )

    def test_combined_redirect_refused(
        self, store: Store, handler: PaperHandler
    ) -> None:
        stub = _seed_paper(store, "combo2021")
        tgt = _seed_paper(store, "combo2021tgt")
        with pytest.raises(BadInput, match="cannot be combined"):
            handler.edit(
                id=f"pa{stub}",
                args={
                    "retire": True,
                    "redirect_to": f"pa{tgt}",
                    "reason": "x",
                },
            )

    def test_unknown_id_notfound(self, handler: PaperHandler) -> None:
        with pytest.raises(NotFound):
            _retire(handler, "pa99999999")

    def test_already_retired_refused(self, store: Store, handler: PaperHandler) -> None:
        stub = _seed_paper(store, "gone2020")
        store.retire_ref(stub)
        with pytest.raises(BadInput, match="already retired"):
            _retire(handler, f"pa{stub}")

    def test_merge_tombstone_retire_refused(
        self, store: Store, handler: PaperHandler
    ) -> None:
        survivor = _seed_paper(store, "surv2020")
        loser = _seed_paper(store, "loser2021")
        _merge(store, loser, survivor)
        with pytest.raises(BadInput, match="merge tombstone"):
            _retire(handler, f"pa{loser}")

    def test_merge_alias_address_refused(
        self, store: Store, handler: PaperHandler
    ) -> None:
        """gr524022 follow-up: a merged loser's old key lives on as a
        ``cite_key_alias`` on the survivor — a destructive door addressed
        by that slug would silently land on the SURVIVOR. Refuse, naming
        the survivor handle + the loser's ``garden restore`` path."""
        from precis.ingest.dedup import merge_duplicate

        winner = _seed_paper(store, "survdoorkey1")
        loser = _seed_paper(store, "loserdoorkey1")
        with store.tx() as conn:
            merge_duplicate(
                store,
                survivor_ref_id=winner,
                duplicate_ref_id=loser,
                source="test",
                reason="dup",
                conn=conn,
            )
        with pytest.raises(BadInput, match="merged alias") as exc:
            _retire(handler, "loserdoorkey1")
        assert f"pa{winner}" in exc.value.cause
        nxt = exc.value.next
        assert nxt is not None
        assert f"id='pa{winner}'" in nxt
        assert f"pa{loser}" in nxt and "garden restore" in nxt
        # By explicit pa<id> the door still reaches the real tombstone —
        # the pre-existing merge-tombstone refusal, not the alias guard.
        with pytest.raises(BadInput, match="merge tombstone"):
            _retire(handler, f"pa{loser}")

    def test_handle_shaped_merge_alias_refused(
        self, store: Store, handler: PaperHandler
    ) -> None:
        """gr524004 shape: a loser's cite_key can spell like another
        kind's handle — 'dr00' parses as a draft handle. The door
        guard's handle early-return must not skip the alias check for
        a foreign-kind handle shape, or a destructive door addressed by
        that key would still land on the survivor."""
        from precis.ingest.dedup import merge_duplicate

        winner = _seed_paper(store, "survdoorkey2")
        loser = _seed_paper(store, "dr00")
        with store.tx() as conn:
            merge_duplicate(
                store,
                survivor_ref_id=winner,
                duplicate_ref_id=loser,
                source="test",
                reason="dup",
                conn=conn,
            )
        with pytest.raises(BadInput, match="merged alias") as exc:
            _retire(handler, "dr00")
        nxt = exc.value.next
        assert nxt is not None
        assert f"id='pa{winner}'" in nxt
        assert f"pa{loser}" in nxt and "garden restore" in nxt
        assert not _is_retired(store, winner)

    def test_two_refs_recording_same_key_no_db_error(
        self, store: Store, handler: PaperHandler
    ) -> None:
        """Two retired refs recording the same value in
        meta.rehomed_cite_keys must not break the guard's scalar
        subquery — LIMIT 1 keeps the refusal a clean BadInput, not a
        raw cardinality violation."""
        from precis.ingest.dedup import merge_duplicate

        winner = _seed_paper(store, "survdoorkey3")
        loser = _seed_paper(store, "loserdoorkey3")
        with store.tx() as conn:
            merge_duplicate(
                store,
                survivor_ref_id=winner,
                duplicate_ref_id=loser,
                source="test",
                reason="dup",
                conn=conn,
            )
        # A second retired ref also records the value (corrupt/migrated
        # data) — the subquery must still return at most one row.
        other = _seed_paper(store, "otherkey3")
        store.stamp_ref_meta(other, {"rehomed_cite_keys": ["loserdoorkey3"]})
        store.retire_ref(other)
        with pytest.raises(BadInput, match="merged alias"):
            _retire(handler, "loserdoorkey3")
        assert not _is_retired(store, winner)


class TestUnretireRefusals:
    def test_live_ref_refused(self, store: Store, handler: PaperHandler) -> None:
        live = _seed_paper(store, "live2020")
        with pytest.raises(BadInput, match="is live"):
            _unretire(handler, f"pa{live}")

    def test_merge_tombstone_unretire_refused(
        self, store: Store, handler: PaperHandler
    ) -> None:
        survivor = _seed_paper(store, "surv2021")
        loser = _seed_paper(store, "loser2022")
        _merge(store, loser, survivor)
        with pytest.raises(BadInput, match="merge tombstone"):
            _unretire(handler, f"pa{loser}")
        assert _is_retired(store, loser)

    def test_missing_reason_refused(self, store: Store, handler: PaperHandler) -> None:
        stub = _seed_paper(store, "nr2020")
        store.retire_ref(stub)
        with pytest.raises(BadInput, match="reason"):
            handler.edit(id=f"pa{stub}", args={"retire": False})


class TestRetireDispatch:
    """End-to-end through the runtime: tombstone handles must reach the
    door rather than dying on the unresolved-handle NotFound (undo) or
    being rewritten to the merge survivor."""

    def test_retire_then_unretire_via_dispatch(
        self, runtime_with_store: PrecisRuntime
    ) -> None:
        store = runtime_with_store.store
        assert store is not None
        stub = _seed_paper(store, "disp2020")
        body, is_error = runtime_with_store.dispatch_with_status(
            "edit",
            {
                "kind": "paper",
                "id": f"pa{stub}",
                "__extras__": {"retire": True, "reason": "phantom"},
            },
        )
        assert not is_error, body
        assert _is_retired(store, stub)
        # The tombstone handle no longer resolves — the undo must still
        # reach the handler through the unresolved-handle fallback.
        body, is_error = runtime_with_store.dispatch_with_status(
            "edit",
            {
                "kind": "paper",
                "id": f"pa{stub}",
                "__extras__": {"retire": False, "reason": "undo"},
            },
        )
        assert not is_error, body
        assert not _is_retired(store, stub)

    def test_retire_on_merge_tombstone_hits_tombstone_not_survivor(
        self, runtime_with_store: PrecisRuntime
    ) -> None:
        store = runtime_with_store.store
        assert store is not None
        survivor = _seed_paper(store, "dispsurv2020")
        loser = _seed_paper(store, "disploser2020")
        _merge(store, loser, survivor)
        body, is_error = runtime_with_store.dispatch_with_status(
            "edit",
            {
                "kind": "paper",
                "id": f"pa{loser}",
                "__extras__": {"retire": True, "reason": "x"},
            },
        )
        assert is_error
        assert "merge tombstone" in body
        assert not _is_retired(store, survivor)
