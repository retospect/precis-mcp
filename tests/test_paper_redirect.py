"""``edit(args={'redirect_to': ...})`` — the merged-stub redirect door.

gr501446: a dedup merge leaves the loser retired with
``meta.superseded_by`` and a ``survivor -supersedes-> loser`` edge; when
the merge picked the wrong survivor there was no MCP door to move the
pointer (a merged handle resolves through to the survivor at dispatch).
These tests pin the door's contract: the source must be a retired merge,
the target must be live and unmerged, cycles refuse, and one transaction
moves edge + meta + ref_event (undoable by the inverse call).
"""

from __future__ import annotations

from typing import Any

import pytest

from precis.dispatch import Hub
from precis.errors import BadInput, NotFound
from precis.handlers.paper import PaperHandler
from precis.response import Response
from precis.runtime import PrecisRuntime
from precis.store import Store


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


def _edge_count(store: Store, src: int, dst: int) -> int:
    with store.pool.connection() as conn:
        row = conn.execute(
            "SELECT count(*) FROM links WHERE src_ref_id = %s "
            "AND dst_ref_id = %s AND relation = 'supersedes'",
            (src, dst),
        ).fetchone()
    assert row is not None
    return int(row[0])


@pytest.fixture
def handler(hub: Hub) -> PaperHandler:
    return PaperHandler(hub=hub)


def _call(handler: PaperHandler, id: str | int, target: Any, **kw: Any) -> Response:
    return handler.edit(
        id=id, args={"redirect_to": target, "reason": "bad merge"}, **kw
    )


class TestRepointHappyPath:
    def test_moves_edge_meta_and_resolution(
        self, store: Store, handler: PaperHandler
    ) -> None:
        old = _seed_paper(store, "oldsurv2020")
        new = _seed_paper(store, "newsurv2020")
        stub = _seed_paper(store, "stub2020")
        _merge(store, stub, old)
        resp = _call(handler, f"pa{stub}", f"pa{new}")
        ref = store.fetch_refs_by_ids([stub])[stub]
        assert ref.meta["superseded_by"] == new
        assert _edge_count(store, old, stub) == 0
        assert _edge_count(store, new, stub) == 1
        resolved = store.resolve_handle(f"pa{stub}")
        assert resolved is not None and resolved.ref_id == new
        assert "Undo" in resp.body and f"pa{old}" in resp.body

    def test_writes_ref_event(self, store: Store, handler: PaperHandler) -> None:
        old = _seed_paper(store, "oldsurv2021")
        new = _seed_paper(store, "newsurv2021")
        stub = _seed_paper(store, "stub2021")
        _merge(store, stub, old)
        _call(handler, stub, f"pa{new}")
        events = [e for e in store.events_for(stub) if e.event == "redirect_repointed"]
        assert len(events) == 1
        payload = events[0].payload
        assert payload["old_survivor"] == old
        assert payload["new_survivor"] == new
        assert payload["reason"] == "bad merge"

    def test_undo_restores_old_target(
        self, store: Store, handler: PaperHandler
    ) -> None:
        old = _seed_paper(store, "oldsurv2022")
        new = _seed_paper(store, "newsurv2022")
        stub = _seed_paper(store, "stub2022")
        _merge(store, stub, old)
        _call(handler, f"pa{stub}", f"pa{new}")
        _call(handler, f"pa{stub}", old)
        ref = store.fetch_refs_by_ids([stub])[stub]
        assert ref.meta["superseded_by"] == old
        assert _edge_count(store, old, stub) == 1
        assert _edge_count(store, new, stub) == 0

    def test_dry_run_writes_nothing(self, store: Store, handler: PaperHandler) -> None:
        old = _seed_paper(store, "oldsurv2023")
        new = _seed_paper(store, "newsurv2023")
        stub = _seed_paper(store, "stub2023")
        _merge(store, stub, old)
        resp = _call(handler, f"pa{stub}", f"pa{new}", dry_run=True)
        assert "DRY RUN" in resp.body
        ref = store.fetch_refs_by_ids([stub])[stub]
        assert ref.meta["superseded_by"] == old
        assert _edge_count(store, old, stub) == 1
        assert _edge_count(store, new, stub) == 0

    def test_noop_when_already_pointed(
        self, store: Store, handler: PaperHandler
    ) -> None:
        old = _seed_paper(store, "oldsurv2024")
        stub = _seed_paper(store, "stub2024")
        _merge(store, stub, old)
        resp = _call(handler, f"pa{stub}", f"pa{old}")
        assert "no change" in resp.body


class TestRepointRefusals:
    def test_source_live_refused(self, store: Store, handler: PaperHandler) -> None:
        live = _seed_paper(store, "live2020")
        tgt = _seed_paper(store, "tgt2020")
        with pytest.raises(BadInput, match="is live"):
            _call(handler, f"pa{live}", f"pa{tgt}")

    def test_source_deleted_no_pointer_refused(
        self, store: Store, handler: PaperHandler
    ) -> None:
        dead = _seed_paper(store, "dead2020")
        store.retire_ref(dead)
        tgt = _seed_paper(store, "tgt2021")
        with pytest.raises(BadInput, match="no meta.superseded_by"):
            _call(handler, f"pa{dead}", f"pa{tgt}")

    def test_source_unknown_notfound(self, store: Store, handler: PaperHandler) -> None:
        tgt = _seed_paper(store, "tgt2022")
        with pytest.raises(NotFound):
            _call(handler, "pa99999999", f"pa{tgt}")

    def test_target_retired_refused(self, store: Store, handler: PaperHandler) -> None:
        old = _seed_paper(store, "oldsurv2025")
        stub = _seed_paper(store, "stub2025")
        _merge(store, stub, old)
        dead = _seed_paper(store, "deadtgt2025")
        store.retire_ref(dead)
        with pytest.raises(BadInput, match="deleted"):
            _call(handler, f"pa{stub}", f"pa{dead}")

    def test_target_merged_refused_naming_survivor(
        self, store: Store, handler: PaperHandler
    ) -> None:
        old = _seed_paper(store, "oldsurv2026")
        stub = _seed_paper(store, "stub2026")
        _merge(store, stub, old)
        merged = _seed_paper(store, "mergedtgt2026")
        chain_end = _seed_paper(store, "chainend2026")
        _merge(store, merged, chain_end)
        with pytest.raises(BadInput, match="itself merged") as exc:
            _call(handler, f"pa{stub}", f"pa{merged}")
        assert f"pa{chain_end}" in str(exc.value.next)

    def test_cycle_refused(self, store: Store, handler: PaperHandler) -> None:
        old = _seed_paper(store, "oldsurv2027")
        stub = _seed_paper(store, "stub2027")
        _merge(store, stub, old)
        # A live target still stamped superseded_by=<stub> would close a
        # pointer loop (restored ref keeping its merge stamp).
        loop = _seed_paper(store, "looptgt2027")
        store.stamp_ref_meta(loop, {"superseded_by": stub})
        with pytest.raises(BadInput, match="cycle"):
            _call(handler, f"pa{stub}", f"pa{loop}")

    def test_target_is_source_refused(
        self, store: Store, handler: PaperHandler
    ) -> None:
        old = _seed_paper(store, "oldsurv2028")
        stub = _seed_paper(store, "stub2028")
        _merge(store, stub, old)
        with pytest.raises(BadInput, match="record itself"):
            _call(handler, f"pa{stub}", f"pa{stub}")

    def test_missing_reason_refused(self, store: Store, handler: PaperHandler) -> None:
        old = _seed_paper(store, "oldsurv2029")
        stub = _seed_paper(store, "stub2029")
        tgt = _seed_paper(store, "tgt2029")
        _merge(store, stub, old)
        with pytest.raises(BadInput, match="reason"):
            handler.edit(id=f"pa{stub}", args={"redirect_to": f"pa{tgt}"})

    def test_combined_field_refused(self, store: Store, handler: PaperHandler) -> None:
        old = _seed_paper(store, "oldsurv2030")
        stub = _seed_paper(store, "stub2030")
        tgt = _seed_paper(store, "tgt2030")
        _merge(store, stub, old)
        with pytest.raises(BadInput, match="cannot be combined"):
            handler.edit(
                id=f"pa{stub}",
                year=2001,
                args={"redirect_to": f"pa{tgt}", "reason": "x"},
            )


class TestRepointDispatch:
    """End-to-end through the runtime: a merged handle in id= must reach
    the tombstone, not be rewritten to the survivor."""

    def test_handle_id_lands_on_tombstone(
        self, runtime_with_store: PrecisRuntime
    ) -> None:
        store = runtime_with_store.store
        assert store is not None
        old = _seed_paper(store, "oldsurv2031")
        new = _seed_paper(store, "newsurv2031")
        stub = _seed_paper(store, "stub2031")
        _merge(store, stub, old)
        body, is_error = runtime_with_store.dispatch_with_status(
            "edit",
            {
                "kind": "paper",
                "id": f"pa{stub}",
                "__extras__": {"redirect_to": f"pa{new}", "reason": "bad merge"},
            },
        )
        assert not is_error, body
        ref = store.fetch_refs_by_ids([stub])[stub]
        assert ref.meta["superseded_by"] == new

    def test_plain_edit_still_redirects_to_survivor(
        self, runtime_with_store: PrecisRuntime
    ) -> None:
        """Control: without redirect_to, a merged handle keeps the old
        transparent-redirect behaviour — the metadata edit lands on the
        survivor."""
        store = runtime_with_store.store
        assert store is not None
        old = _seed_paper(store, "oldsurv2032")
        stub = _seed_paper(store, "stub2032")
        _merge(store, stub, old)
        body, is_error = runtime_with_store.dispatch_with_status(
            "edit",
            {"kind": "paper", "id": f"pa{stub}", "year": 2001},
        )
        assert not is_error, body
        survivor = store.fetch_refs_by_ids([old])[old]
        assert survivor.year == 2001
