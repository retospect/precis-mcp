"""gr512234: operator restore revives a merge loser without undoing migration."""

from __future__ import annotations

import re
from contextlib import nullcontext
from typing import Any

import pytest

from precis.errors import BadInput
from precis.graph.garden.apply import apply_merge, undo_merge
from precis.ingest.dedup import merge_duplicate
from precis.runtime import PrecisRuntime
from precis.store import ChunkInsert, Ref, Store
from precis.store.revision_context import revision_context


def _paper(store: Store, slug: str) -> int:
    return store.insert_ref(
        kind="paper",
        slug=slug,
        title="A study of reversible duplicate paper records",
        provider="manual",
        meta={"keep": "original provenance"},
    ).id


def _merged_pair(store: Store) -> tuple[int, int]:
    loser = _paper(store, "unmergeloser2026")
    survivor = _paper(store, "unmergesurvivor2026")
    _merge(store, loser, survivor)
    return loser, survivor


def _merge(store: Store, loser: int, survivor: int) -> None:
    with store.tx() as conn:
        merge_duplicate(
            store,
            survivor_ref_id=survivor,
            duplicate_ref_id=loser,
            source="test-unmerge",
            reason="test duplicate",
            conn=conn,
        )


def _ref(store: Store, ref_id: int) -> Ref:
    return store.fetch_refs_by_ids([ref_id], include_deleted=True)[ref_id]


def _merge_edges(store: Store, loser: int) -> list[tuple[Any, ...]]:
    with store.pool.connection() as conn:
        return conn.execute(
            "SELECT src_ref_id, dst_ref_id, relation FROM links "
            "WHERE (dst_ref_id = %s AND relation = 'supersedes') "
            "OR (src_ref_id = %s AND relation = 'superseded-by') "
            "ORDER BY link_id",
            (loser, loser),
        ).fetchall()


def test_store_restore_preserves_migrated_content_and_history(store: Store) -> None:
    loser = _paper(store, "unmergeloser2026")
    survivor = _paper(store, "unmergesurvivor2026")
    other = _paper(store, "unmergeother2026")
    store.set_ref_identifier(loser, "doi", "10.1000/unmerge-test", source="test")
    store.chunks.insert_chunks(loser, [ChunkInsert(ord=0, text="Original body.")])
    store.add_link(src_ref_id=loser, dst_ref_id=other, relation="related-to")
    _merge(store, loser, survivor)
    events = store.events_for(loser)
    survivor_events = store.events_for(survivor)
    assert _ref(store, loser).retired_at is not None
    assert _merge_edges(store, loser)

    assert store.restore_superseded_ref(loser) is True

    restored = _ref(store, loser)
    assert restored.retired_at is None
    assert restored.meta == {"keep": "original provenance"}
    assert _merge_edges(store, loser) == []
    resolved = store.resolve_handle(f"pa{loser}")
    assert resolved is not None and resolved.ref_id == loser
    assert all(event in store.events_for(loser) for event in events)
    assert all(event in store.events_for(survivor) for event in survivor_events)
    with store.pool.connection() as conn:
        assert conn.execute(
            "SELECT ref_id FROM ref_identifiers WHERE id_kind = 'doi' "
            "AND id_value = '10.1000/unmerge-test'"
        ).fetchall() == [(survivor,)]
        assert conn.execute(
            "SELECT ref_id, text FROM chunks WHERE ref_id = ANY(%s) "
            "AND ord >= 0 AND retired_at IS NULL",
            ([loser, survivor],),
        ).fetchall() == [(survivor, "Original body.")]
        assert conn.execute(
            "SELECT src_ref_id FROM links WHERE dst_ref_id = %s "
            "AND relation = 'related-to'",
            (other,),
        ).fetchall() == [(survivor,)]
    assert store.restore_superseded_ref(loser) is False
    assert _ref(store, survivor).retired_at is None


def test_store_restore_refuses_garden_merge_and_preserves_undo(
    store: Store,
) -> None:
    loser = _paper(store, "gardenunmergeloser2026")
    winner = _paper(store, "gardenunmergewinner2026")
    third = _paper(store, "gardenunmergetarget2026")
    loser_title = _ref(store, loser).title
    store.add_link(src_ref_id=loser, dst_ref_id=third, relation="related-to")
    record = apply_merge(store, loser, winner, actor="operator")
    assert _ref(store, loser).meta["garden_merge"]["merge_id"] == record.merge_id
    assert _ref(store, loser).meta["superseded_by"] == winner

    before = _ref(store, loser)
    with pytest.raises(BadInput, match="recorded full reversal") as exc:
        store.restore_superseded_ref(loser)
    assert exc.value.next == f"precis garden undo {record.merge_id}"
    assert _ref(store, loser) == before
    assert loser_title in _ref(store, winner).meta["aliases"]
    with store.pool.connection() as conn:
        assert conn.execute(
            "SELECT src_ref_id, dst_ref_id, meta->>'garden_merge', "
            "meta->>'prior_ref_id' FROM links WHERE dst_ref_id = %s "
            "AND relation = 'related-to'",
            (third,),
        ).fetchall() == [(winner, third, record.merge_id, str(loser))]
    with revision_context(trust_origin="human", trust_actor_type="operator"):
        undo_merge(store, record.merge_id)
    assert _ref(store, loser).retired_at is None
    with store.pool.connection() as conn:
        assert conn.execute(
            "SELECT src_ref_id FROM links WHERE dst_ref_id = %s "
            "AND relation = 'related-to'",
            (third,),
        ).fetchall() == [(loser,)]


@pytest.mark.parametrize("state", ["live", "retired", "missing"])
def test_store_restore_refuses_non_merge_rows(store: Store, state: str) -> None:
    ref_id = _paper(store, "unmergeplain2026")
    if state == "retired":
        store.retire_ref(ref_id)
    before = _ref(store, ref_id)
    assert (
        store.restore_superseded_ref(999999999 if state == "missing" else ref_id)
        is False
    )
    assert _ref(store, ref_id) == before


def test_store_restore_joins_transaction_and_sets_link_trust(store: Store) -> None:
    loser, survivor = _merged_pair(store)
    other = store.insert_ref(
        kind="memory", slug=None, title="Trust-guarded link target"
    ).id
    with revision_context(trust_origin="computed", trust_actor_type="worker"):
        edge = store.add_link(
            src_ref_id=survivor, dst_ref_id=other, relation="related-to"
        )
    before = _ref(store, loser)
    edges_before = _merge_edges(store, loser)
    with pytest.raises(RuntimeError, match="abort caller transaction"):
        with store.tx() as conn:
            conn.execute(
                "SELECT set_config('precis.trust_origin', '', true), "
                "set_config('precis.trust_actor_type', '', true)"
            )
            assert conn.execute(
                "SELECT trust_write_version FROM links WHERE link_id = %s",
                (edge.id,),
            ).fetchone() == (1,)
            assert store.restore_superseded_ref(loser, conn=conn) is True
            assert conn.execute(
                "SELECT current_setting('precis.trust_origin'), "
                "current_setting('precis.trust_actor_type')"
            ).fetchone() == ("human", "operator")
            # A converted link rejects this mutation without the door's GUCs.
            assert (
                conn.execute(
                    "UPDATE links SET src_ref_id = %s WHERE link_id = %s",
                    (loser, edge.id),
                ).rowcount
                == 1
            )
            assert conn.execute(
                "SELECT retired_at FROM refs WHERE ref_id = %s", (loser,)
            ).fetchone() == (None,)
            raise RuntimeError("abort caller transaction")
    assert _ref(store, loser) == before
    assert _merge_edges(store, loser) == edges_before
    with store.pool.connection() as conn:
        assert conn.execute(
            "SELECT src_ref_id FROM links WHERE link_id = %s", (edge.id,)
        ).fetchone() == (survivor,)
        settings = conn.execute(
            "SELECT current_setting('precis.trust_origin', true), "
            "current_setting('precis.trust_actor_type', true)"
        ).fetchone()
    assert settings is not None and not any(settings)


@pytest.mark.parametrize("actor_type", [None, "agent", "human", "operator"])
@pytest.mark.parametrize("forge_args", [False, True])
def test_runtime_has_no_restore_door_even_with_forged_args(
    runtime_with_store: PrecisRuntime, actor_type: str | None, forge_args: bool
) -> None:
    store = runtime_with_store.live_store
    loser, winner = _merged_pair(store)
    refs_before = [_ref(store, ref_id) for ref_id in (loser, winner)]
    events_before = [store.events_for(ref_id) for ref_id in (loser, winner)]
    edges_before = _merge_edges(store, loser)
    args: dict[str, Any] = {"restore_superseded": True}
    if forge_args:
        args.update(trust_actor_type="operator", trust_origin="human")
    refusal = f"keys {sorted(args)!r} not accepted by paper.edit"
    context = (
        nullcontext()
        if actor_type is None
        else revision_context(trust_origin="human", trust_actor_type=actor_type)
    )
    with context:
        body, is_error = runtime_with_store.dispatch_with_status(
            "edit",
            {
                "kind": "paper",
                "id": f"pa{loser}",
                "__extras__": args,
            },
        )
    assert is_error, body
    assert refusal in body
    assert [_ref(store, ref_id) for ref_id in (loser, winner)] == refs_before
    assert [store.events_for(ref_id) for ref_id in (loser, winner)] == events_before
    assert _merge_edges(store, loser) == edges_before


def test_paper_restore_cannot_restore_foreign_kind(
    runtime_with_store: PrecisRuntime,
) -> None:
    store = runtime_with_store.live_store
    loser = store.insert_ref(kind="memory", slug=None, title="Retired memory").id
    survivor = store.insert_ref(kind="memory", slug=None, title="Surviving memory").id
    store.stamp_ref_meta(loser, {"superseded_by": survivor, "dedup": "test"})
    store.retire_ref(loser)
    with revision_context(trust_origin="human", trust_actor_type="operator"):
        body, is_error = runtime_with_store.dispatch_with_status(
            "edit",
            {"kind": "paper", "id": loser, "__extras__": {"restore_superseded": True}},
        )
    assert is_error, body
    assert _ref(store, loser).retired_at is not None


@pytest.mark.parametrize("verb", ["edit", "link", "tag", "put"])
def test_mutation_redirect_notice_is_once_per_call(
    runtime_with_store: PrecisRuntime, verb: str
) -> None:
    store = runtime_with_store.live_store
    loser, survivor = _merged_pair(store)
    target = _paper(store, "unmergelinktarget2026")
    fields: dict[str, dict[str, Any]] = {
        "edit": {"year": 2026},
        "link": {"target": f"pa{target}", "rel": "related-to"},
        "tag": {"add": ["unmerge-test"]},
        "put": {"record_type": "software"},
    }
    notice = f"[info] resolved pa{loser} -> pa{survivor} (superseded)"
    for _ in range(2):
        body, is_error = runtime_with_store.dispatch_with_status(
            verb,
            {"kind": "paper", "id": f"pa{loser}", "__extras__": fields[verb]},
        )
        assert not is_error, body
        assert body.count(notice) == 1, body
    assert _ref(store, loser).retired_at is not None
    assert _ref(store, survivor).retired_at is None


@pytest.mark.parametrize("verb", ["edit", "link"])
def test_mutation_error_preserves_redirect_notice_without_writes(
    runtime_with_store: PrecisRuntime, verb: str
) -> None:
    store = runtime_with_store.live_store
    loser, survivor = _merged_pair(store)
    target = _paper(store, "unmergeinvalidtarget2026")
    refs_before = [_ref(store, ref_id) for ref_id in (loser, survivor, target)]
    events_before = {ref_id: store.events_for(ref_id) for ref_id in (loser, survivor)}
    with store.pool.connection() as conn:
        links_before = conn.execute("SELECT * FROM links ORDER BY link_id").fetchall()
    fields: dict[str, dict[str, Any]] = {
        "edit": {"entry_type": "invalid-crossref-kind"},
        "link": {"target": f"pa{target}", "mode": "invalid"},
    }
    notice = f"[info] resolved pa{loser} -> pa{survivor} (superseded)"

    body, is_error = runtime_with_store.dispatch_with_status(
        verb,
        {"kind": "paper", "id": f"pa{loser}", "__extras__": fields[verb]},
    )

    assert is_error, body
    assert body.count(notice) == 1, body
    assert [_ref(store, ref_id) for ref_id in (loser, survivor, target)] == refs_before
    assert {
        ref_id: store.events_for(ref_id) for ref_id in (loser, survivor)
    } == events_before
    with store.pool.connection() as conn:
        assert (
            conn.execute("SELECT * FROM links ORDER BY link_id").fetchall()
            == links_before
        )


def test_redirect_receipt_survives_pagination_in_write_footer(
    runtime_with_store: PrecisRuntime, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = runtime_with_store.live_store
    loser, survivor = _merged_pair(store)
    monkeypatch.setenv("PRECIS_MAX_BODY_BYTES", "200")
    runtime_with_store.long_lived = True

    body, is_error = runtime_with_store.dispatch_with_status(
        "edit",
        {
            "kind": "paper",
            "id": f"pa{loser}",
            "dry_run": True,
            "__extras__": {"title": "A long title sentence. " * 30},
        },
    )

    assert not is_error, body
    assert "more(cursor='" in body
    # A 200-byte cap is smaller than the pagination footer reserve. Do not
    # assert a full receipt on page one: reconstruct the response payload.
    # Subsequent frames use a normal cap so draining stays cheap.
    monkeypatch.setenv("PRECIS_MAX_BODY_BYTES", "4096")
    payloads = []
    for _ in range(10):
        payload, _, footer = body.partition("\n\n---\n⚠️")
        payloads.append(payload)
        cursor = re.search(r"more\(cursor='([^']+)'\)", footer)
        if cursor is None:
            break
        body, is_error = runtime_with_store.fetch_more(cursor[1], fold=False)
        assert not is_error, body
    else:
        pytest.fail("write receipt did not finish within ten pages")
    full = "".join(payloads)
    receipts = re.findall(
        r"^\[info\] resolved (pa\d+) -> (pa\d+) \(superseded\)$", full, re.MULTILINE
    )
    assert receipts == [(f"pa{loser}", f"pa{survivor}")]
    assert full.startswith("DRY RUN"), full


@pytest.mark.parametrize("view", ["abstract", "bibtex"])
def test_superseded_reads_keep_merge_note_in_footer(
    runtime_with_store: PrecisRuntime, view: str
) -> None:
    store = runtime_with_store.live_store
    loser, survivor = _merged_pair(store)
    store.stamp_ref_meta(survivor, {"abstract": "The surviving paper abstract."})
    body, is_error = runtime_with_store.dispatch_with_status(
        "get", {"kind": "paper", "id": f"pa{loser}", "view": view}
    )
    assert not is_error, body
    assert "[info] resolved" not in body
    note = f"[info] pa{loser} was merged into pa{survivor} — please use that handle going forward."
    assert body.count(note) == 1
    assert not body.startswith(note)
    if view == "abstract":
        assert body.index("The surviving paper abstract.") < body.index(note)
