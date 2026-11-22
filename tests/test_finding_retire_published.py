"""A claim hub past ``'candidate'`` retires only through the retirement
procedure (gr760607, ruled by Reto 2026-11-22).

The plain ``delete(kind='finding', ...)`` refuses such a hub and names the
procedure; ``args={'retire_published': True}`` retires it, records
``superseded``/``retracted`` on its publish record with the reason and the
successor, repoints links and draft cites, keeps the id readable as a
tombstone, and files one owner todo when a signed or published artifact
exists outside precis.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest

from precis.dispatch import Hub
from precis.errors import BadInput
from precis.handlers.finding import FindingHandler
from precis.runtime import PrecisRuntime
from precis.taproot.canon import CanonicalClaim
from precis.taproot.hub import attach_evidence, mint_hub
from tests.workers._helpers import seed_ref

PAST_CANDIDATE = [
    "reviewed",
    "signed",
    "anchored",
    "publishing",
    "published",
    "superseded",
    "retracted",
    "rejected",
]


def _handler(store: Any) -> FindingHandler:
    return FindingHandler(hub=Hub(store=store))


def _pair(store: Any) -> tuple[int, int]:
    """(dup, survivor): two live claim hubs."""
    survivor = mint_hub(store, CanonicalClaim(sentence="Survivor hub claim.", scope={}))
    dup = mint_hub(store, CanonicalClaim(sentence="Duplicate hub claim.", scope={}))
    return dup, survivor


def _publish(store: Any, ref_id: int, state: str, **cols: Any) -> int:
    names = ["claim_ref_id", "state", *cols]
    with store.pool.connection() as conn:
        row = conn.execute(
            f"INSERT INTO nanopub_publish ({', '.join(names)}) "
            f"VALUES ({', '.join(['%s'] * len(names))}) RETURNING id",
            (ref_id, state, *cols.values()),
        ).fetchone()
        conn.commit()
    return int(row[0])


def _publish_row(store: Any, row_id: int) -> tuple[Any, ...]:
    with store.pool.connection() as conn:
        return tuple(
            conn.execute(
                "SELECT state, retired_reason, retired_successor_ref_id "
                "FROM nanopub_publish WHERE id = %s",
                (row_id,),
            ).fetchone()
        )


def _is_retired(store: Any, ref_id: int) -> bool:
    with store.pool.connection() as conn:
        row = conn.execute(
            "SELECT retired_at FROM refs WHERE ref_id = %s", (ref_id,)
        ).fetchone()
    return row[0] is not None


def _counts(store: Any) -> tuple[int, ...]:
    """Row counts of every table the door writes, plus publish-row states."""
    with store.pool.connection() as conn:
        counts = tuple(
            conn.execute(f"SELECT count(*) FROM {t}").fetchone()[0]
            for t in ("refs", "links", "ref_events", "chunk_events")
        )
        states = tuple(
            conn.execute(
                "SELECT id, state, retired_reason FROM nanopub_publish ORDER BY id"
            ).fetchall()
        )
    return (*counts, states)


def _owner_todos(store: Any) -> list[tuple[int, str]]:
    with store.pool.connection() as conn:
        rows = conn.execute(
            "SELECT r.ref_id, r.title FROM refs r WHERE r.kind = 'todo' "
            "AND r.retired_at IS NULL ORDER BY r.ref_id"
        ).fetchall()
    return [(int(r[0]), str(r[1])) for r in rows]


def _todo_body(store: Any, ref_id: int) -> str:
    with store.pool.connection() as conn:
        return str(
            conn.execute(
                "SELECT text FROM chunks WHERE ref_id = %s AND ord = 0", (ref_id,)
            ).fetchone()[0]
        )


def _seed_cite(store: Any, hub: int) -> int:
    ref, _title = store.drafts.create_draft(name="retirecite", title="Cite draft")
    chunk = store.drafts.add_chunks(
        ref_id=ref.id,
        chunk_kind="paragraph",
        text=f"Claim [fi{hub}] stands.",
        at={"last": True},
    )[0]
    return int(chunk.chunk_id)


def _chunk_text(store: Any, chunk_id: int) -> str:
    with store.pool.connection() as conn:
        return str(
            conn.execute(
                "SELECT text FROM chunks WHERE chunk_id = %s", (chunk_id,)
            ).fetchone()[0]
        )


class TestPlainDoor:
    @pytest.mark.parametrize("state", PAST_CANDIDATE)
    @pytest.mark.parametrize("dry_run", [True, False])
    @pytest.mark.parametrize("superseded", [True, False])
    def test_refuses_a_hub_past_candidate(
        self, store: Any, state: str, dry_run: bool, superseded: bool
    ) -> None:
        dup, survivor = _pair(store)
        _publish(store, dup, state)
        before = _counts(store)
        sup = f"fi{survivor}" if superseded else None

        with pytest.raises(BadInput, match="past 'candidate'") as ei:
            _handler(store).delete(
                id=dup, superseded_by=sup, reason="same claim", dry_run=dry_run
            )

        assert state in ei.value.cause
        assert "plain delete door refuses" in ei.value.cause
        nxt = str(ei.value.next)
        assert f"id='fi{dup}'" in nxt
        assert "args={'retire_published': True}" in nxt
        assert (f"superseded_by='fi{survivor}'" in nxt) is superseded
        assert _counts(store) == before
        assert not _is_retired(store, dup)

    @pytest.mark.parametrize("dup_row", [None, "candidate"])
    def test_candidate_state_hub_still_retires_by_a_past_candidate_one(
        self, store: Any, dup_row: str | None
    ) -> None:
        dup, survivor = _pair(store)
        row = _publish(store, dup, dup_row) if dup_row else None
        _publish(store, survivor, "published")

        body = (
            _handler(store)
            .delete(id=dup, superseded_by=f"fi{survivor}", reason="same claim")
            .body
        )

        assert f"deleted finding id={dup}" in body
        assert "publish row" not in body
        assert _is_retired(store, dup)
        assert not _is_retired(store, survivor)
        if row is not None:
            assert _publish_row(store, row) == ("candidate", None, None)
        assert _owner_todos(store) == []

    def test_a_non_finding_successor_is_not_suggested(self, store: Any) -> None:
        dup, _survivor = _pair(store)
        _publish(store, dup, "reviewed")
        other = seed_ref(store, title="A paper", kind="paper")

        with pytest.raises(BadInput, match="past 'candidate'") as ei:
            _handler(store).delete(id=dup, superseded_by=f"pa{other}", reason="x")

        assert f"successor must be a finding, not pa{other}" in ei.value.cause
        nxt = str(ei.value.next)
        assert f"pa{other}" not in nxt
        assert "superseded_by='fi<M>'" in nxt
        assert "'Retire a hub past candidate'" in nxt
        assert not _is_retired(store, dup)


class TestProcedure:
    def test_superseded_records_publish_state_repoints_and_tombstones(
        self, store: Any
    ) -> None:
        dup, survivor = _pair(store)
        row = _publish(store, dup, "reviewed")
        _publish(store, survivor, "reviewed")
        paper = seed_ref(store, title="Dup-only paper", kind="paper")
        attach_evidence(store, hub_ref_id=dup, paper_ref_id=paper, role="corroborates")
        chunk = _seed_cite(store, dup)

        body = (
            _handler(store)
            .delete(
                id=dup,
                superseded_by=f"fi{survivor}",
                reason="same claim, minted twice",
                retire_published=True,
            )
            .body
        )

        assert f"publish row {row}: reviewed -> superseded" in body
        assert "draft cites: 1" in body
        assert _is_retired(store, dup)
        assert _publish_row(store, row) == (
            "superseded",
            "same claim, minted twice",
            survivor,
        )
        assert _chunk_text(store, chunk) == f"Claim [fi{survivor}] stands."
        with store.pool.connection() as conn:
            moved = conn.execute(
                "SELECT dst_ref_id FROM links WHERE src_ref_id = %s "
                "AND relation = 'corroborates'",
                (paper,),
            ).fetchall()
        assert [int(r[0]) for r in moved] == [survivor]
        tomb = _handler(store).get(id=dup).body
        assert "[soft-deleted, read-only]" in tomb
        assert f"retired: superseded by fi{survivor} — same claim, minted twice" in tomb
        assert f"get(kind='finding', id={survivor})" in tomb
        # Reviewed carries no signed artifact: no owner todo.
        assert _owner_todos(store) == []

    def test_retracted_published_hub_files_one_owner_todo(self, store: Any) -> None:
        dup, _survivor = _pair(store)
        trusty = "https://w3id.org/np/RAexampleTrustyCode"
        registry = "https://registry.example.org/np/RAexampleTrustyCode"
        row = _publish(
            store,
            dup,
            "published",
            trusty_uri=trusty,
            registry_url=registry,
            aida_uri="http://purl.org/aida/Example",
            published_at=datetime(2026, 11, 1, tzinfo=UTC),
        )

        body = (
            _handler(store)
            .delete(
                id=dup, reason="the measurement was retracted", retire_published=True
            )
            .body
        )

        assert f"publish row {row}: published -> retracted" in body
        assert _publish_row(store, row) == (
            "retracted",
            "the measurement was retracted",
            None,
        )
        todos = _owner_todos(store)
        assert len(todos) == 1, todos
        todo_id, title = todos[0]
        assert f"td{todo_id}" in body
        assert f"fi{dup}" in title and "retraction or erratum" in title
        text = _todo_body(store, todo_id)
        assert trusty in text and registry in text
        assert f"publish row {row}" in text
        assert store.has_tag(todo_id, "OPEN", "ask-user")
        tomb = _handler(store).get(id=dup).body
        assert "retracted: the measurement was retracted" in tomb

    def test_signed_hub_files_the_owner_todo(self, store: Any) -> None:
        dup, survivor = _pair(store)
        _publish(store, dup, "signed", trusty_uri="https://w3id.org/np/RAsigned")

        _handler(store).delete(
            id=dup,
            superseded_by=f"fi{survivor}",
            reason="same claim",
            retire_published=True,
        )

        todos = _owner_todos(store)
        assert len(todos) == 1
        assert "https://w3id.org/np/RAsigned" in _todo_body(store, todos[0][0])

    def test_terminal_only_hub_keeps_its_state(self, store: Any) -> None:
        dup, _survivor = _pair(store)
        row = _publish(store, dup, "rejected")

        body = (
            _handler(store)
            .delete(id=dup, reason="withdrawn", retire_published=True)
            .body
        )

        assert f"publish row {row}: stays rejected" in body
        assert _publish_row(store, row) == ("rejected", "withdrawn", None)
        assert "retired (publish record rejected): withdrawn" in (
            _handler(store).get(id=dup).body
        )

    @pytest.mark.parametrize("reason", [None, "", "   "])
    def test_reason_is_required(self, store: Any, reason: str | None) -> None:
        dup, survivor = _pair(store)
        _publish(store, dup, "reviewed")
        before = _counts(store)

        with pytest.raises(BadInput, match="needs reason="):
            _handler(store).delete(
                id=dup,
                superseded_by=f"fi{survivor}",
                reason=reason,
                retire_published=True,
            )

        assert _counts(store) == before
        assert not _is_retired(store, dup)

    def test_dry_run_writes_nothing_and_shows_the_plan(self, store: Any) -> None:
        dup, survivor = _pair(store)
        row = _publish(store, dup, "anchored", trusty_uri="t", batch_id=None)
        before = _counts(store)

        body = (
            _handler(store)
            .delete(
                id=dup,
                superseded_by=f"fi{survivor}",
                reason="same claim",
                retire_published=True,
                dry_run=True,
            )
            .body
        )

        assert f"on apply: publish row {row}: anchored -> superseded" in body
        assert "on apply: owner todo" in body
        assert _counts(store) == before

    def test_flag_on_a_candidate_hub_points_at_the_plain_door(self, store: Any) -> None:
        dup, survivor = _pair(store)
        before = _counts(store)

        with pytest.raises(BadInput, match="not past 'candidate'") as ei:
            _handler(store).delete(
                id=dup,
                superseded_by=f"fi{survivor}",
                reason="same claim",
                retire_published=True,
            )

        assert "retire_published" not in str(ei.value.next)
        assert _counts(store) == before

    def test_a_publishing_row_is_refused(self, store: Any) -> None:
        dup, _survivor = _pair(store)
        _publish(store, dup, "publishing")
        before = _counts(store)

        with pytest.raises(BadInput, match="publishing"):
            _handler(store).delete(id=dup, reason="x", retire_published=True)

        assert _counts(store) == before

    def test_the_successor_must_be_a_finding(self, store: Any) -> None:
        dup, _survivor = _pair(store)
        _publish(store, dup, "reviewed")
        other = seed_ref(store, title="A paper", kind="paper")

        with pytest.raises(BadInput, match="superseded by another finding"):
            _handler(store).delete(
                id=dup,
                superseded_by=f"pa{other}",
                reason="x",
                retire_published=True,
            )
        assert not _is_retired(store, dup)


class TestRollback:
    def test_a_failed_owner_todo_insert_leaves_the_publish_row(
        self, store: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        dup, survivor = _pair(store)
        row = _publish(store, dup, "signed", trusty_uri="https://w3id.org/np/RAx")
        handler = _handler(store)
        insert = handler.store.insert_ref

        def fail_todo(*args: Any, **kw: Any) -> Any:
            if kw.get("kind") == "todo":
                raise RuntimeError("todo insert fixture")
            return insert(*args, **kw)

        monkeypatch.setattr(handler.store, "insert_ref", fail_todo)
        before = _counts(store)

        with pytest.raises(RuntimeError, match="todo insert fixture"):
            handler.delete(
                id=dup,
                superseded_by=f"fi{survivor}",
                reason="same claim",
                retire_published=True,
            )

        assert _publish_row(store, row) == ("signed", None, None)
        assert not _is_retired(store, dup)
        assert _counts(store) == before

    def test_a_cited_hub_without_successor_rolls_the_publish_row_back(
        self, store: Any
    ) -> None:
        dup, _survivor = _pair(store)
        row = _publish(store, dup, "published", trusty_uri="https://w3id.org/np/RAy")
        chunk = _seed_cite(store, dup)
        before = _counts(store)

        with pytest.raises(BadInput) as ei:
            _handler(store).delete(id=dup, reason="withdrawn", retire_published=True)

        assert f"fi{dup} is still cited" in ei.value.cause
        assert _publish_row(store, row) == ("published", None, None)
        assert not _is_retired(store, dup)
        assert _owner_todos(store) == []
        assert _chunk_text(store, chunk) == f"Claim [fi{dup}] stands."
        assert _counts(store) == before


def test_the_procedure_through_dispatch(runtime_with_store: PrecisRuntime) -> None:
    """``args={'retire_published': True}`` reaches the door, and a retracted
    hub's ``fi`` handle reads as the tombstone."""
    runtime = runtime_with_store
    store = runtime.live_store
    dup, _survivor = _pair(store)
    row = _publish(store, dup, "reviewed")

    out, failed = runtime.dispatch_with_status(
        "delete",
        {
            "kind": "finding",
            "id": f"fi{dup}",
            "reason": "no longer holds",
            "__extras__": {"retire_published": True},
        },
    )

    assert not failed, out
    assert f"publish row {row}: reviewed -> retracted" in out
    read, failed = runtime.dispatch_with_status(
        "get", {"kind": "finding", "id": f"fi{dup}"}
    )
    assert "retracted: no longer holds" in read, read
