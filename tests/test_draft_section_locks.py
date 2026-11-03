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
from types import SimpleNamespace
from typing import Any

import psycopg
import pytest
from psycopg.conninfo import make_conninfo
from psycopg.errors import LockNotAvailable

from precis.errors import NotFound
from precis.store.store import Store
from precis.utils import db_retry

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


# ── gr553312: bounded retry inside the write ────────────────────────────
#
# Prod roles run ``lock_timeout=5s``: a draft write that found its
# section lock held surfaced ``LockNotAvailable`` to the caller although
# the write was safe to replay. The write ops now run under
# ``retry_locked`` — a wait inside the retry budget retires in the
# store; a hold past it reraises the same error dispatch maps to the
# Upstream retry hint. The contending ops run on a second store whose
# sessions carry a compressed ``lock_timeout`` (same idiom as
# ``test_paper_body_ops``'s lock-contention case), so the first attempt
# fails fast and the test doesn't wait out the suite's 30 s default.

CONTENDED_LOCK_TIMEOUT_MS = 300


def _contended_store(store: Store) -> Store:
    dsn = make_conninfo(
        store.dsn or "",
        options="-c statement_timeout=120s "
        f"-c lock_timeout={CONTENDED_LOCK_TIMEOUT_MS}ms",
    )
    return Store.connect(dsn, min_size=1, max_size=2)


def test_sibling_writes_retry_through_held_section_lock(
    store: Store, side_lock, monkeypatch: pytest.MonkeyPatch
) -> None:
    proj = _project(store)
    ref, _title = store.drafts.create_draft(
        name="d4", title="Title", project_ref_id=proj
    )

    # Two sibling puts (same parent → same section) behind a held lock:
    # the gripe's 2-way parallel collision. Deterministic backoff —
    # retry_locked's own ``time`` name is swapped for a no-sleep stub
    # (this test's real ``time.sleep`` is untouched) — so attempts land
    # at exact lock_timeout boundaries: attempt 1 fails at ~0.3 s,
    # attempt 2's wait spans ~[0.3 s, 0.6 s]. The 0.45 s release falls
    # inside attempt 2 with margin both ways (uniform jitter let
    # attempt 3 time out just before a 1.0 s release — flaky, gr553312
    # review).
    monkeypatch.setattr(db_retry, "time", SimpleNamespace(sleep=lambda _s: None))

    lk = side_lock(ref.id, 0)
    store2 = _contended_store(store)
    try:
        t1, r1 = _run_in_thread(
            lambda: store2.drafts.add_chunks(
                ref_id=ref.id,
                chunk_kind="paragraph",
                text="one",
                at={"last": True},
            )
        )
        t2, r2 = _run_in_thread(
            lambda: store2.drafts.add_chunks(
                ref_id=ref.id,
                chunk_kind="paragraph",
                text="two",
                at={"last": True},
            )
        )
        time.sleep(1.5 * CONTENDED_LOCK_TIMEOUT_MS / 1000)
        lk.release()
        t1.join(timeout=10)
        t2.join(timeout=10)
    finally:
        store2.close()

    assert not t1.is_alive() and not t2.is_alive()
    for result in (r1, r2):
        assert "error" not in result, result.get("error")
    assert {c.text for c in r1["value"] + r2["value"]} == {"one", "two"}


def test_section_lock_held_past_retry_budget_reraises(store: Store, side_lock) -> None:
    proj = _project(store)
    ref, _title = store.drafts.create_draft(
        name="d5", title="Title", project_ref_id=proj
    )

    lk = side_lock(ref.id, 0)
    store2 = _contended_store(store)
    try:
        t, result = _run_in_thread(
            lambda: store2.drafts.add_chunks(
                ref_id=ref.id,
                chunk_kind="paragraph",
                text="never lands",
                at={"last": True},
            )
        )
        t.join(timeout=15)
    finally:
        lk.release()
        store2.close()

    assert not t.is_alive()
    assert isinstance(result.get("error"), LockNotAvailable), result.get("error")


def test_non_lock_errors_are_not_retried(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    proj = _project(store)
    ref, _title = store.drafts.create_draft(
        name="d6", title="Title", project_ref_id=proj
    )

    opens = {"n": 0}
    real_tx = store.drafts.tx

    def counting_tx() -> Any:
        opens["n"] += 1
        return real_tx()

    monkeypatch.setattr(store.drafts, "tx", counting_tx)
    with pytest.raises(NotFound):
        store.drafts.add_chunks(
            ref_id=ref.id,
            chunk_kind="paragraph",
            text="x",
            at={"after": "dc999999999"},
        )
    assert opens["n"] == 1
