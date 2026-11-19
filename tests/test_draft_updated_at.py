"""gr687836: authored chunk changes refresh the draft's public clock."""

from datetime import UTC, datetime

import pytest

from precis.store import Store


def _stamp(store: Store, ref_id: int) -> datetime:
    with store.pool.connection() as conn:
        row = conn.execute(
            "SELECT updated_at FROM refs WHERE ref_id = %s", (ref_id,)
        ).fetchone()
    assert row is not None
    return row[0]


@pytest.mark.parametrize("operation", ["add", "text", "meta", "move", "retire"])
def test_chunk_changes_refresh_draft(store: Store, operation: str) -> None:
    ref, title = store.drafts.create_draft(name="clock", title="Clock")
    para = store.drafts.add_chunks(
        ref_id=ref.id, chunk_kind="paragraph", text="Before"
    )[0]
    other, _ = store.drafts.create_draft(name="other-clock", title="Other")
    other_stamp = _stamp(store, other.id)
    stale = datetime(2020, 1, 1, tzinfo=UTC)
    with store.pool.connection() as conn:
        conn.execute(
            "UPDATE refs SET updated_at = %s WHERE ref_id = %s", (stale, ref.id)
        )
    if operation == "add":
        store.drafts.add_chunks(ref_id=ref.id, chunk_kind="paragraph", text="New")
    elif operation == "text":
        store.drafts.edit_text(para.handle, "After")
    elif operation == "meta":
        store.drafts.patch_chunk_meta(para.handle, {"style": "aside"})
    elif operation == "move":
        store.drafts.move_chunk(para.handle, {"before": title.handle})
    else:
        store.drafts.retire_chunk(para.handle)
    with store.pool.connection() as conn:
        row = conn.execute(
            "SELECT max(e.ts) FROM chunk_events e JOIN chunks c USING (chunk_id) "
            "WHERE c.ref_id = %s",
            (ref.id,),
        ).fetchone()
    assert row is not None
    assert _stamp(store, ref.id) == row[0]
    assert _stamp(store, ref.id) > stale
    assert _stamp(store, other.id) == other_stamp


def test_draft_clock_rolls_back_on_aborted_edit(store: Store) -> None:
    ref, title = store.drafts.create_draft(name="rollback-clock", title="Clock")
    before = _stamp(store, ref.id)
    with pytest.raises(RuntimeError, match="abort"):
        with store.drafts.tx() as conn:
            store.drafts.edit_text_in_tx(conn, title.handle, "Rolled back")
            raise RuntimeError("abort")
    assert _stamp(store, ref.id) == before


@pytest.mark.parametrize(
    "operation", ["figure-refresh", "derived-add", "derived-edit", "derived-retire"]
)
def test_draft_clock_ignores_derived_writes(store: Store, operation: str) -> None:
    # gr687836 review: machine writes (lazy figure re-render, draft_refresh,
    # doctor_tick) log chunk_events too; only their source.derived marker
    # keeps them out of the draft's public clock. Each branch exercises a
    # REAL derived path, not a raw UPDATE that emits no event.
    ref, title = store.drafts.create_draft(name="derived-clock", title="Clock")
    para = store.drafts.add_chunks(ref_id=ref.id, chunk_kind="paragraph", text="Body")[
        0
    ]
    before = _stamp(store, ref.id)
    if operation == "figure-refresh":
        # The lazy render path: viewing/exporting a draft re-renders stale
        # pathway figures and stamps the fresh data package.
        fig = store.drafts.add_chunks(
            ref_id=ref.id, chunk_kind="figure", text="A pathway figure"
        )[0]
        assert _stamp(store, ref.id) > before  # the authored add DOES bump
        clock = _stamp(store, ref.id)
        store.drafts.stamp_figure_data_package(fig.chunk_id, {"points": [1, 2]})
        assert _stamp(store, ref.id) == clock
    elif operation == "derived-add":
        store.drafts.add_chunks(
            ref_id=ref.id,
            chunk_kind="paragraph",
            text="Machine prose",
            source={"reason": "draft_refresh", "derived": True},
        )
        assert _stamp(store, ref.id) == before
    elif operation == "derived-edit":
        store.drafts.edit_text(
            title.handle,
            "Machine heading",
            source={"reason": "draft_refresh", "derived": True},
        )
        assert _stamp(store, ref.id) == before
    else:
        store.drafts.retire_chunk(
            para.handle,
            source={"reason": "draft_refresh", "derived": True},
        )
        assert _stamp(store, ref.id) == before


def test_old_events_do_not_regress_clock(store: Store) -> None:
    ref, title = store.drafts.create_draft(name="old-clock", title="Clock")
    before = _stamp(store, ref.id)
    with store.pool.connection() as conn:
        conn.execute(
            "INSERT INTO chunk_events (chunk_id, event_kind, ts) "
            "VALUES (%s, 'edited', %s)",
            (title.chunk_id, datetime(2020, 1, 1, tzinfo=UTC)),
        )
    assert _stamp(store, ref.id) == before


def test_other_chunk_native_kinds_keep_existing_clock_policy(store: Store) -> None:
    ref, title = store.drafts.create_draft(name="plan-clock", title="Plan", kind="plan")
    before = _stamp(store, ref.id)
    store.drafts.edit_text(title.handle, "Changed plan", kind="plan")
    assert _stamp(store, ref.id) == before
