"""Section-scoped advisory locks on structural draft ops (gr176088 part 2a).

``DraftStore._lock_sections`` takes a ``pg_advisory_xact_lock`` per
distinct (ref_id, section) touched by a structural op — narrow enough
that two writers on *different* sections of the same draft still
parallelize, but two writers racing the *same* section serialize. These
tests hold the lock on a side connection (never committed, so the
``pg_advisory_xact_lock`` stays held) and prove: same-section ops block,
different-section ops don't, and ``move_chunk`` blocks on its
destination section.
"""

from __future__ import annotations

import threading
import time

import psycopg
import pytest

from precis.store.store import Store

BLOCK_WAIT = 0.5


def _project(store: Store) -> int:
    ref = store.insert_ref(kind="todo", slug=None, title="Section-lock project")
    return ref.id


def _lock_key_sql() -> str:
    return (
        "SELECT pg_advisory_xact_lock("
        "hashtextextended('draft-section:' || %s || ':' || %s, 0))"
    )


class _SideLock:
    """Holds a section's advisory lock on a dedicated, never-committed
    connection until released. Not pool-backed on purpose — a bare
    ``psycopg.connect`` keeps the leak-check's pool-backend accounting
    untouched and gives us full control over when the tx (and thus the
    lock) ends."""

    def __init__(self, dsn: str, ref_id: int, parent_or_0: int) -> None:
        self._conn = psycopg.connect(dsn)
        self._conn.execute(_lock_key_sql(), (ref_id, parent_or_0))
        self._released = False

    def release(self) -> None:
        """Idempotent — a test may release early to unblock its own
        thread; the fixture's teardown sweep then no-ops on it."""
        if self._released:
            return
        self._released = True
        try:
            self._conn.rollback()
        finally:
            self._conn.close()


@pytest.fixture
def side_lock(store: Store):
    held: list[_SideLock] = []
    dsn = store.dsn
    assert dsn is not None

    def _hold(ref_id: int, parent_or_0: int) -> _SideLock:
        lk = _SideLock(dsn, ref_id, parent_or_0)
        held.append(lk)
        return lk

    try:
        yield _hold
    finally:
        for lk in held:
            lk.release()


def _run_in_thread(fn):
    result: dict[str, object] = {}

    def _target() -> None:
        try:
            result["value"] = fn()
        except Exception as exc:  # pragma: no cover - surfaced via assert
            result["error"] = exc

    t = threading.Thread(target=_target)
    t.start()
    return t, result


def test_add_chunks_blocks_on_held_section_lock(store: Store, side_lock) -> None:
    proj = _project(store)
    ref, title = store.drafts.create_draft(
        name="d1", title="Title", project_ref_id=proj
    )
    intro = store.drafts.add_chunks(
        ref_id=ref.id,
        chunk_kind="heading",
        text="Intro",
        at={"after": title.handle},
    )[0]

    # A concurrent writer has this section (children of ``intro``) locked.
    lk = side_lock(ref.id, intro.chunk_id)

    t, result = _run_in_thread(
        lambda: store.drafts.add_chunks(
            ref_id=ref.id,
            chunk_kind="paragraph",
            text="body",
            at={"into": intro.handle, "last": True},
        )
    )
    time.sleep(BLOCK_WAIT)
    assert t.is_alive(), "add_chunks should block while the section lock is held"

    lk.release()
    t.join(timeout=5)
    assert not t.is_alive()
    assert "error" not in result, result.get("error")
    assert result["value"][0].text == "body"


def test_add_chunks_does_not_block_on_different_section(
    store: Store, side_lock
) -> None:
    proj = _project(store)
    ref, title = store.drafts.create_draft(
        name="d2", title="Title", project_ref_id=proj
    )
    intro = store.drafts.add_chunks(
        ref_id=ref.id,
        chunk_kind="heading",
        text="Intro",
        at={"after": title.handle},
    )[0]

    # Hold the lock on ``intro``'s section; the op below targets the
    # top level (a different section) and must not serialize behind it.
    side_lock(ref.id, intro.chunk_id)

    t, result = _run_in_thread(
        lambda: store.drafts.add_chunks(
            ref_id=ref.id,
            chunk_kind="heading",
            text="Methods",
            at={"after": intro.handle},
        )
    )
    t.join(timeout=BLOCK_WAIT)
    assert not t.is_alive(), "add_chunks on an unrelated section must not block"
    assert "error" not in result, result.get("error")
    assert result["value"][0].text == "Methods"


def test_move_chunk_blocks_on_held_destination_section(store: Store, side_lock) -> None:
    proj = _project(store)
    ref, title = store.drafts.create_draft(
        name="d3", title="Title", project_ref_id=proj
    )
    intro = store.drafts.add_chunks(
        ref_id=ref.id,
        chunk_kind="heading",
        text="Intro",
        at={"after": title.handle},
    )[0]
    methods = store.drafts.add_chunks(
        ref_id=ref.id,
        chunk_kind="heading",
        text="Methods",
        at={"after": intro.handle},
    )[0]
    para = store.drafts.add_chunks(
        ref_id=ref.id,
        chunk_kind="paragraph",
        text="body",
        at={"into": intro.handle, "last": True},
    )[0]

    # A concurrent writer holds the destination section (children of
    # ``methods``) locked; moving ``para`` into it must block.
    lk = side_lock(ref.id, methods.chunk_id)

    t, result = _run_in_thread(
        lambda: store.drafts.move_chunk(
            para.handle, {"into": methods.handle, "last": True}
        )
    )
    time.sleep(BLOCK_WAIT)
    assert t.is_alive(), "move_chunk should block on the held destination section"

    lk.release()
    t.join(timeout=5)
    assert not t.is_alive()
    assert "error" not in result, result.get("error")

    moved = store.drafts.get_draft_chunk(para.handle)
    assert moved is not None
    assert moved.parent_chunk_id == methods.chunk_id
