"""``edit(args={'merge_into': ...})`` — the confirmed-pair merge door.

gr509659: folding a known live duplicate pair (e.g. a wanted stub plus
the held twin a user dropped a PDF for) needed an operator-only
``precis.ingest.dedup.merge_duplicate`` call. This door wraps it for the
session MCP, gated on the shared title-compatibility check
(``title_merge_refusal`` + a trigram floor — the cutoffs
``reconcile_by_title_similarity`` and the mint-time stub guard use), so
a caller-asserted pair whose titles disagree still refuses. ``id=`` is
the loser that retires; ``args['merge_into']`` is the survivor.
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

_TITLE = "Analyses of Reaction Schemes Using De Donder Relations"
_OTHER_TITLE = "Surface Science Studies of Heterogeneous Catalyst Films"


def _seed_paper(
    store: Store,
    slug: str,
    *,
    title: str = _TITLE,
    year: int | None = None,
    authors: list[dict[str, Any]] | None = None,
) -> int:
    return store.insert_ref(
        kind="paper",
        slug=slug,
        title=title,
        provider="manual",
        meta={},
        year=year,
        authors=authors,
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


def _id_owner(store: Store, id_kind: str, id_value: str) -> int | None:
    with store.pool.connection() as conn:
        row = conn.execute(
            "SELECT ref_id FROM ref_identifiers WHERE id_kind = %s AND id_value = %s",
            (id_kind, id_value),
        ).fetchone()
    return int(row[0]) if row is not None else None


def _body_ords(store: Store, ref_id: int) -> list[int]:
    with store.pool.connection() as conn:
        rows = conn.execute(
            "SELECT ord FROM chunks WHERE ref_id = %s AND ord >= 0 "
            "AND retired_at IS NULL ORDER BY ord",
            (ref_id,),
        ).fetchall()
    return [int(r[0]) for r in rows]


@pytest.fixture
def handler(hub: Hub) -> PaperHandler:
    return PaperHandler(hub=hub)


def _call(handler: PaperHandler, id: str | int, target: Any, **kw: Any) -> Response:
    return handler.edit(
        id=id, args={"merge_into": target, "reason": "confirmed dup"}, **kw
    )


class TestMergeHappyPath:
    def test_folds_loser_into_winner(self, store: Store, handler: PaperHandler) -> None:
        # The gripe pair's shape: a DOI-bearing stub + a year-less held
        # twin, identical titles (year-unknown relaxes at sim ~1.0).
        loser = _seed_paper(
            store, "dumesic1999stub", year=1999, authors=[{"family": "Dumesic"}]
        )
        winner = _seed_paper(store, "dumesic1999held", authors=[{"family": "Dumesic"}])
        store.set_ref_identifier(loser, "doi", "10.1006/jcat.1999.2523", source="test")
        store.chunks.insert_chunks(winner, [ChunkInsert(ord=0, text="the held body")])
        resp = _call(handler, f"pa{loser}", f"pa{winner}")
        assert _is_retired(store, loser)
        ref = store.fetch_refs_by_ids([loser], include_deleted=True)[loser]
        assert ref.meta["superseded_by"] == winner
        # The loser's DOI migrated to the survivor.
        assert _id_owner(store, "doi", "10.1006/jcat.1999.2523") == winner
        # The handle redirects transparently.
        resolved = store.resolve_handle(f"pa{loser}")
        assert resolved is not None and resolved.ref_id == winner
        assert f"pa{winner}" in resp.body

    def test_moves_body_to_bodiless_winner(
        self, store: Store, handler: PaperHandler
    ) -> None:
        loser = _seed_paper(store, "bodiedloser2020", year=2020)
        winner = _seed_paper(store, "emptywinner2020", year=2020)
        store.chunks.insert_chunks(loser, [ChunkInsert(ord=0, text="dup body")])
        resp = _call(handler, loser, winner)
        assert _body_ords(store, winner) == [0]
        assert _body_ords(store, loser) == []
        assert "body" in resp.body

    def test_writes_ref_events_on_both_sides(
        self, store: Store, handler: PaperHandler
    ) -> None:
        loser = _seed_paper(store, "evloser2020", year=2020)
        winner = _seed_paper(store, "evwinner2020", year=2020)
        _call(handler, loser, winner)
        merged = [e for e in store.events_for(winner) if e.event == "duplicate_merged"]
        assert len(merged) == 1
        assert merged[0].payload["duplicate_ref_id"] == loser
        assert merged[0].payload["reason"] == "confirmed dup"
        deleted = [
            e for e in store.events_for(loser) if e.event == "soft_deleted_duplicate"
        ]
        assert len(deleted) == 1
        assert deleted[0].payload["duplicate_of"] == winner

    def test_dry_run_writes_nothing(self, store: Store, handler: PaperHandler) -> None:
        loser = _seed_paper(store, "dryloser2020", year=2020)
        winner = _seed_paper(store, "drywinner2020", year=2020)
        resp = _call(handler, f"pa{loser}", f"pa{winner}", dry_run=True)
        assert "DRY RUN" in resp.body
        assert not _is_retired(store, loser)


class TestMergeRefusals:
    def test_titles_disagree_refused(self, store: Store, handler: PaperHandler) -> None:
        loser = _seed_paper(store, "diffloser2020", year=2020)
        winner = _seed_paper(store, "diffwinner2020", title=_OTHER_TITLE, year=2020)
        with pytest.raises(BadInput, match="title gate") as exc:
            _call(handler, loser, winner)
        assert exc.value.next is not None
        assert not _is_retired(store, loser)

    def test_year_mismatch_refused(self, store: Store, handler: PaperHandler) -> None:
        loser = _seed_paper(store, "yrloser2020", year=1999)
        winner = _seed_paper(store, "yrwinner2020", year=2010)
        with pytest.raises(BadInput, match="year-mismatch"):
            _call(handler, loser, winner)
        assert not _is_retired(store, loser)

    def test_author_mismatch_refused(self, store: Store, handler: PaperHandler) -> None:
        loser = _seed_paper(
            store, "auloser2020", year=2020, authors=[{"family": "Dumesic"}]
        )
        winner = _seed_paper(
            store, "auwinner2020", year=2020, authors=[{"family": "Iglesia"}]
        )
        with pytest.raises(BadInput, match="author-mismatch"):
            _call(handler, loser, winner)

    def test_short_title_refused(self, store: Store, handler: PaperHandler) -> None:
        loser = _seed_paper(store, "stloser2020", title="Note", year=2020)
        winner = _seed_paper(store, "stwinner2020", title="Note", year=2020)
        with pytest.raises(BadInput, match="short-title"):
            _call(handler, loser, winner)

    def test_self_merge_refused(self, store: Store, handler: PaperHandler) -> None:
        ref = _seed_paper(store, "selfmerge2020", year=2020)
        with pytest.raises(BadInput, match="record itself"):
            _call(handler, f"pa{ref}", f"pa{ref}")

    def test_missing_reason_refused(self, store: Store, handler: PaperHandler) -> None:
        loser = _seed_paper(store, "nrloser2020", year=2020)
        winner = _seed_paper(store, "nrwinner2020", year=2020)
        with pytest.raises(BadInput, match="reason"):
            handler.edit(id=loser, args={"merge_into": winner})

    def test_loser_already_merged_refused(
        self, store: Store, handler: PaperHandler
    ) -> None:
        survivor = _seed_paper(store, "oldsurv2040", year=2020)
        loser = _seed_paper(store, "foldedloser2040", year=2020)
        _merge(store, loser, survivor)
        other = _seed_paper(store, "othertgt2040", year=2020)
        with pytest.raises(BadInput, match="already merged") as exc:
            _call(handler, f"pa{loser}", f"pa{other}")
        assert "redirect_to" in str(exc.value.next)

    def test_loser_plain_retired_refused(
        self, store: Store, handler: PaperHandler
    ) -> None:
        loser = _seed_paper(store, "deadloser2040", year=2020)
        store.retire_ref(loser)
        winner = _seed_paper(store, "tgt2040", year=2020)
        with pytest.raises(BadInput, match="already retired"):
            _call(handler, loser, winner)

    def test_target_retired_refused(self, store: Store, handler: PaperHandler) -> None:
        loser = _seed_paper(store, "loser2041", year=2020)
        dead = _seed_paper(store, "deadtgt2041", year=2020)
        store.retire_ref(dead)
        with pytest.raises(BadInput, match="deleted"):
            _call(handler, loser, f"pa{dead}")

    def test_target_merged_refused_naming_survivor(
        self, store: Store, handler: PaperHandler
    ) -> None:
        loser = _seed_paper(store, "loser2042", year=2020)
        merged = _seed_paper(store, "mergedtgt2042", year=2020)
        chain_end = _seed_paper(store, "chainend2042", year=2020)
        _merge(store, merged, chain_end)
        with pytest.raises(BadInput, match="itself merged") as exc:
            _call(handler, loser, f"pa{merged}")
        assert f"pa{chain_end}" in str(exc.value.next)

    def test_unknown_id_notfound(self, handler: PaperHandler) -> None:
        with pytest.raises(NotFound):
            _call(handler, "pa99999999", "pa1")

    def test_unresolvable_target_refused(
        self, store: Store, handler: PaperHandler
    ) -> None:
        loser = _seed_paper(store, "loser2043", year=2020)
        with pytest.raises(BadInput, match="resolves to no"):
            _call(handler, loser, "pa99999999")

    def test_combined_field_refused(self, store: Store, handler: PaperHandler) -> None:
        loser = _seed_paper(store, "comboloser2020", year=2020)
        winner = _seed_paper(store, "combowinner2020", year=2020)
        with pytest.raises(BadInput, match="cannot be combined"):
            handler.edit(
                id=loser,
                year=2001,
                args={"merge_into": winner, "reason": "x"},
            )

    def test_combined_retire_refused(self, store: Store, handler: PaperHandler) -> None:
        loser = _seed_paper(store, "comboloser2021", year=2020)
        winner = _seed_paper(store, "combowinner2021", year=2020)
        with pytest.raises(BadInput, match="cannot be combined"):
            handler.edit(
                id=loser,
                args={
                    "retire": True,
                    "merge_into": winner,
                    "reason": "x",
                },
            )

    def test_combined_redirect_refused(
        self, store: Store, handler: PaperHandler
    ) -> None:
        loser = _seed_paper(store, "comboloser2022", year=2020)
        winner = _seed_paper(store, "combowinner2022", year=2020)
        other = _seed_paper(store, "comboother2022", year=2020)
        with pytest.raises(BadInput, match="cannot be combined"):
            handler.edit(
                id=loser,
                args={
                    "merge_into": winner,
                    "redirect_to": other,
                    "reason": "x",
                },
            )


class TestMergeDispatch:
    """End-to-end through the runtime: a tombstone ``pa<id>`` in ``id=``
    must reach the handler, not be rewritten to the survivor."""

    def test_merge_via_dispatch(self, runtime_with_store: PrecisRuntime) -> None:
        store = runtime_with_store.store
        assert store is not None
        loser = _seed_paper(store, "disploser2030", year=2020)
        winner = _seed_paper(store, "dispwinner2030", year=2020)
        body, is_error = runtime_with_store.dispatch_with_status(
            "edit",
            {
                "kind": "paper",
                "id": f"pa{loser}",
                "__extras__": {
                    "merge_into": f"pa{winner}",
                    "reason": "confirmed dup",
                },
            },
        )
        assert not is_error, body
        assert _is_retired(store, loser)

    def test_merge_into_on_tombstone_hits_tombstone_not_survivor(
        self, runtime_with_store: PrecisRuntime
    ) -> None:
        store = runtime_with_store.store
        assert store is not None
        survivor = _seed_paper(store, "dispsurv2031", year=2020)
        loser = _seed_paper(store, "disploser2031", year=2020)
        _merge(store, loser, survivor)
        other = _seed_paper(store, "dispother2031", year=2020)
        body, is_error = runtime_with_store.dispatch_with_status(
            "edit",
            {
                "kind": "paper",
                "id": f"pa{loser}",
                "__extras__": {
                    "merge_into": f"pa{other}",
                    "reason": "x",
                },
            },
        )
        assert is_error
        assert "already merged" in body
        # Neither ref got folded by accident.
        assert not _is_retired(store, survivor)
        assert not _is_retired(store, other)
