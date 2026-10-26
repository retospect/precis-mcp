"""gr520909: a gripe left unpicked for weeks shows a stale mark.

Unpicked is derived per read — no ``meta.fix``, STATUS never past
``triaged``, age ≥ ``gf.UNPICKED_AFTER``, and no live job/todo edge —
so the mark appears on the card, in list rows and in the ``/open`` +
digest header, and any pickup clears it on the next read.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from precis.dispatch import Hub
from precis.handlers import _gripe_fix as gf
from precis.handlers.gripe import GripeHandler
from precis.store import Tag
from tests._refs import insert_gripe


@pytest.fixture
def handler(hub: Hub) -> GripeHandler:
    return GripeHandler(hub=hub)


def _gripe(
    handler: GripeHandler,
    title: str = "Area: something is wrong",
    *,
    status: str = "open",
    days: int = 30,
) -> int:
    ref = insert_gripe(handler.store, title, status=status)
    if days:
        _backdate(handler, int(ref.id), days)
    return int(ref.id)


def _backdate(handler: GripeHandler, gid: int, days: int) -> None:
    with handler.store.pool.connection() as conn:
        conn.execute(
            "UPDATE refs SET created_at = %s WHERE ref_id = %s",
            (datetime.now(UTC) - timedelta(days=days), gid),
        )
        conn.commit()


def _link_todo(handler: GripeHandler, gid: int, title: str = "fix it") -> int:
    tid = int(handler.store.insert_ref(kind="todo", slug=None, title=title).id)
    handler.store.add_link(src_ref_id=gid, dst_ref_id=tid, relation="related-to")
    return tid


# ── the mark itself ──────────────────────────────────────────────────


def test_old_unpicked_shows_card_line_and_row_mark(
    handler: GripeHandler,
) -> None:
    gid = _gripe(handler)
    card = handler.get(id=gid).body
    assert "stale: unpicked 30d" in card
    assert "STATUS:open" in card
    assert "gr520909" in card
    listing = handler.get(id="/open").body
    assert "unpicked 30d" in listing


def test_fresh_gripe_has_no_mark(handler: GripeHandler) -> None:
    gid = _gripe(handler, days=0)
    assert "stale:" not in handler.get(id=gid).body
    body = handler.get(id="/open").body
    assert "unpicked" not in body


def test_under_threshold_has_no_mark(handler: GripeHandler) -> None:
    gid = _gripe(handler, days=gf.UNPICKED_AFTER.days - 1)
    assert "stale:" not in handler.get(id=gid).body


# ── what clears it ───────────────────────────────────────────────────


def test_meta_fix_clears_the_mark(handler: GripeHandler) -> None:
    gid = _gripe(handler)
    assert "stale:" in handler.get(id=gid).body
    handler.edit(id=gid, mode="fix", batch="wave-1", brief="do it")
    card = handler.get(id=gid).body
    assert "stale:" not in card
    assert "fix:" in card


def test_status_past_triaged_clears_the_mark(handler: GripeHandler) -> None:
    gid = _gripe(handler, status="ready_for_fix")
    assert "stale:" not in handler.get(id=gid).body


def test_triaged_stays_stale(handler: GripeHandler) -> None:
    gid = _gripe(handler, status="triaged")
    assert "stale: unpicked 30d" in handler.get(id=gid).body


def test_live_linked_todo_clears_terminal_reflags(
    handler: GripeHandler,
) -> None:
    gid = _gripe(handler)
    assert "stale:" in handler.get(id=gid).body
    tid = _link_todo(handler, gid)
    assert "stale:" not in handler.get(id=gid).body
    # a finished todo is a closed attempt, not a pickup
    handler.store.add_tag(
        tid, Tag.closed("STATUS", "done"), set_by="agent", replace_prefix=True
    )
    assert "stale: unpicked 30d" in handler.get(id=gid).body


# ── the header count (/open + session digest share age_summary) ──────


def test_open_header_counts_unpicked(handler: GripeHandler) -> None:
    stale_gid = _gripe(handler)
    _gripe(handler, title="fresh one", days=0)
    body = handler.get(id="/open").body
    assert "1 unpicked ≥14d" in body
    # picking it up clears the derived count on next read
    handler.edit(id=stale_gid, mode="fix", batch="wave-1")
    assert "unpicked" not in handler.get(id="/open").body
