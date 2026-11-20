"""Real-PG progress counts, including the review regressions for gr699009."""

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from precis.dispatch import Hub
from precis.errors import BadInput, Unsupported
from precis.handlers.gripe import GripeHandler
from precis.store import Store, Tag
from precis.utils.gripe_stats import render_stats, rolling_stats
from tests._refs import insert_gripe

NOW = datetime(2026, 11, 17, 12, tzinfo=UTC)


def _seed(
    store: Store,
    hours: float,
    *,
    status: str = "done",
    comment: str | None = None,
    comment_hours: float | None = None,
) -> int:
    ref = insert_gripe(store, "progress fixture", status=status)
    with store.tx() as conn:
        conn.execute(
            "UPDATE ref_tags SET created_at = %s WHERE ref_id = %s",
            (NOW - timedelta(hours=hours), ref.id),
        )
        if comment:
            conn.execute(
                "INSERT INTO chunks (ref_id, ord, chunk_kind, text, created_at) "
                "VALUES (%s, 1, 'gripe_comment', %s, %s)",
                (
                    ref.id,
                    comment,
                    NOW
                    - timedelta(
                        hours=hours if comment_hours is None else comment_hours
                    ),
                ),
            )
    return ref.id


def test_cumulative_windows_and_closure_evidence(store: Store) -> None:
    """Old passes, failed checks, reopened and future closures cannot count."""
    _seed(store, 1, comment="resolved: abc1234 — auto-verified after deploy: sha")
    _seed(store, 6, comment="resolved: abc1234 — user-verified after deploy")
    _seed(store, 12, comment="resolved: abc1234 — prod check passed: OK")
    _seed(store, 24, status="auto_verify")
    _seed(store, 24.001)
    _seed(store, -1)
    _seed(
        store,
        0.5,
        status="triaged",
        comment="resolved: abc1234 — prod check passed: OK",
    )
    _seed(store, 0.5, comment="resolved: abc1234 — prod check FAILED: no")
    _seed(
        store,
        0.5,
        comment="resolved: abc1234 — prod check passed: old",
        comment_hours=2,
    )
    assert rolling_stats(store, now=NOW) == [
        {"hours": 1, "fixed": 3, "verified": 1, "duplicates": 0},
        {"hours": 6, "fixed": 4, "verified": 2, "duplicates": 0},
        {"hours": 12, "fixed": 5, "verified": 3, "duplicates": 0},
        {"hours": 24, "fixed": 6, "verified": 3, "duplicates": 0},
    ]


def test_duplicates_require_gripe_successor(store: Store) -> None:
    """Spam and filed-in-error retirements must not inflate duplicate progress."""
    keeper = insert_gripe(store, "keeper")
    for meta in (
        {},
        {"superseded_by": "sweep"},
        {"superseded_by": f"gr{keeper.id}"},
        {"superseded_by": keeper.id},
    ):
        ref = insert_gripe(store, "retired", meta=meta)
        with store.tx() as conn:
            conn.execute(
                "UPDATE refs SET retired_at = %s WHERE ref_id = %s",
                (NOW - timedelta(minutes=30), ref.id),
            )
    linked = insert_gripe(store, "linked duplicate")
    store.add_link(src_ref_id=keeper.id, dst_ref_id=linked.id, relation="supersedes")
    # Multiple evidence paths must still count one gripe.
    store.update_ref(linked.id, meta_patch={"superseded_by": keeper.id})
    with store.tx() as conn:
        conn.execute(
            "UPDATE refs SET retired_at = %s WHERE ref_id = %s",
            (NOW - timedelta(hours=6), linked.id),
        )
    assert [r["duplicates"] for r in rolling_stats(store, now=NOW)] == [2, 3, 3, 3]


def test_empty_stats_and_mcp(store: Store) -> None:
    rows = rolling_stats(store, now=NOW)
    assert [r["fixed"] for r in rows] == [0, 0, 0, 0]
    assert "| 24h | 0 | 0 | 0 |" in render_stats(rows)
    handler = GripeHandler(hub=Hub(store=store))
    assert "| 6h | 0 | 0 | 0 |" in handler.get(id="/stats").body


def test_verification_writer_and_reclose(store: Store) -> None:
    """The real writer shares the comment/tag clock; an old pass cannot recur."""
    from precis.store import Tag

    ref = insert_gripe(store, "writer fixture", status="auto_verify")
    handler = GripeHandler(hub=Hub(store=store))
    handler.put(
        id=ref.id,
        text="resolved: abc1234 — auto-verified after deploy: sha",
        tags=["STATUS:done"],
    )
    assert rolling_stats(store)[0]["verified"] == 1
    store.add_tag(
        ref.id, Tag.closed("STATUS", "triaged"), set_by="agent", replace_prefix=True
    )
    assert rolling_stats(store)[0]["verified"] == 0
    handler.put(
        id=ref.id,
        text="resolved: abc1234 — closed without a new pass",
        tags=["STATUS:done"],
    )
    assert rolling_stats(store)[0]["verified"] == 0


def test_stats_refuses_view_and_query(store: Store) -> None:
    handler = GripeHandler(hub=Hub(store=store))
    with pytest.raises(Unsupported, match="takes no view="):
        handler.get(id="/stats", view="comments")
    with pytest.raises(BadInput, match="takes no q="):
        handler.get(id="/stats", q="needle")


def test_stats_refuses_list_args(store: Store) -> None:
    """Every list-shaping kwarg the dispatcher admits is refused, not dropped
    (mcp-authoring rule 6: an accepted kwarg is read or refused)."""
    handler = GripeHandler(hub=Hub(store=store))
    refused: dict[str, dict[str, Any]] = {
        "page": {"page": 2},
        "page_size": {"page_size": 5},
        "sort": {"sort": "drain"},
        "closed_since": {"closed_since": "2026-11-17T00:00:00Z"},
        "closed_until": {"closed_until": "2026-11-17T00:00:00Z"},
        "queue": {"queue": "in-build"},
        "host": {"host": "maple"},
        "batch": {"batch": "s1"},
        "trace": {"trace": "ValueError"},
        "comment": {"comment": 1},
        "comments": {"comments": 1},
        "tag_prefixes": {"tag_prefixes": "thread,fix"},
    }
    for name, kwargs in refused.items():
        with pytest.raises(BadInput, match=f"takes no .*{name}"):
            handler.get(id="/stats", **kwargs)
    # page=1 is a tolerated no-op, the same reading the '/queue'
    # and '/batch' intercept gives it.
    assert "| 1h | 0 | 0 | 0 |" in handler.get(id="/stats", page=1).body


def _backdate_state(store: Store, ref_id: int, *, hours: float) -> None:
    with store.tx() as conn:
        conn.execute(
            "UPDATE ref_tags SET created_at = %s WHERE ref_id = %s",
            (NOW - timedelta(hours=hours), ref_id),
        )


def test_duplicates_count_live_wontfix_superseded(store: Store) -> None:
    """The documented dup close leaves the dup live — put(id=dup,
    tags=['STATUS:wontfix'], link='gripe:<keeper>', rel='superseded-by') —
    so its window reads the STATUS attachment clock, not retired_at."""
    keeper = insert_gripe(store, "keeper")
    handler = GripeHandler(hub=Hub(store=store))
    dup = insert_gripe(store, "dup")
    handler.put(
        id=dup.id,
        text="resolved: duplicate of the keeper",
        tags=["STATUS:wontfix"],
        link=f"gripe:{keeper.id}",
        rel="superseded-by",
    )
    _backdate_state(store, dup.id, hours=0.5)
    fresh = insert_gripe(store, "fresh dup")
    handler.put(
        id=fresh.id,
        text="resolved: duplicate of the keeper",
        tags=["STATUS:wontfix"],
        link=f"gripe:{keeper.id}",
        rel="superseded-by",
    )
    _backdate_state(store, fresh.id, hours=0.1)
    # A wontfix close with no gripe successor is a decision, not a dup.
    plain = insert_gripe(store, "plain wontfix")
    handler.put(
        id=plain.id,
        text="resolved: wontfix — working as intended",
        tags=["STATUS:wontfix"],
    )
    _backdate_state(store, plain.id, hours=0.1)
    # A superseded ref not (yet) wontfix is not a dup either.
    waiting = insert_gripe(store, "superseded still open")
    handler.put(
        id=waiting.id,
        text="see the keeper instead",
        link=f"gripe:{keeper.id}",
        rel="superseded-by",
    )
    _backdate_state(store, waiting.id, hours=0.1)
    assert [r["duplicates"] for r in rolling_stats(store, now=NOW)] == [2, 2, 2, 2]
    # Reopening the dup (a new STATUS displaces wontfix) drops it again.
    store.add_tag(
        dup.id, Tag.closed("STATUS", "open"), set_by="agent", replace_prefix=True
    )
    assert [r["duplicates"] for r in rolling_stats(store, now=NOW)] == [1, 1, 1, 1]
