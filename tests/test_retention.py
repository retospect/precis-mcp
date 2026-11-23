"""Tests for the ``retention`` pass — gr690338's first policy slice.

DB-backed (real news refs, links, chunks and read_receipts), no LLM.
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime, timedelta

import pytest

from precis.handlers._cache_base import FetchResult
from precis.handlers.news import NewsHandler
from precis.principal import bind
from precis.runtime import PrecisRuntime
from precis.store import Store
from precis.store.types import ChunkInsert
from precis.utils.db_log_handler import BufferedDBLogHandler
from precis.utils.slug import slug_from_text
from precis.workers import retention, scheduler
from precis.workers.retention import (
    FIRST_REPORT_KEY,
    META_RETENTION,
    NEWS_RETIRE_REASON,
    NEWS_UNUSED_DAYS,
    SERVICE_NAME,
    run_retention_pass,
)
from tests.conftest import _active_dsn
from tests.workers._helpers import seed_chunk, seed_ref


def _light(store: Store, prio: int = 5) -> None:
    """Write the ``service_config`` row that arms the pass."""
    with store.pool.connection() as conn:
        conn.execute(
            "INSERT INTO service_config (host, service, prio) "
            "VALUES ('*', %s, %s) "
            "ON CONFLICT (host, service) DO UPDATE SET prio = EXCLUDED.prio",
            (SERVICE_NAME, prio),
        )
        conn.commit()


def _news(store: Store, title: str, *, days: int = 120) -> int:
    """A news ref backdated past the 90-day horizon by default."""
    ref_id = seed_ref(store, title=title, kind="news")
    _backdate(store, ref_id, days)
    return ref_id


def _backdate(store: Store, ref_id: int, days: int = 120) -> None:
    old = datetime.now(UTC) - timedelta(days=days)
    with store.pool.connection() as conn:
        conn.execute(
            "UPDATE refs SET created_at=%s, updated_at=%s WHERE ref_id=%s",
            (old, old, ref_id),
        )


def _arm_report_done(store: Store) -> None:
    """Pretend the first report-only fire already ran."""
    doc = {"at": datetime.now(UTC).isoformat(), "counts": {"news": 0}}
    with store.pool.connection() as conn:
        conn.execute(
            "INSERT INTO app_settings (key, value, updated_at) VALUES (%s, %s, now())",
            (FIRST_REPORT_KEY, json.dumps(doc)),
        )
        conn.commit()


def _is_retired(store: Store, ref_id: int) -> bool:
    with store.pool.connection() as conn:
        row = conn.execute(
            "SELECT retired_at IS NOT NULL FROM refs WHERE ref_id=%s", (ref_id,)
        ).fetchone()
    assert row is not None
    return bool(row[0])


def _receipt(store: Store, identity: str, *, live: bool = True) -> None:
    expires = (
        datetime.now(UTC) + timedelta(hours=1)
        if live
        else datetime.now(UTC) - timedelta(hours=1)
    )
    with store.pool.connection() as conn:
        conn.execute(
            "INSERT INTO read_receipts (owner_key, identity, expires_at) "
            "VALUES ('install', %s, %s)",
            (identity, expires),
        )
        conn.commit()


def test_dark_pass_is_a_no_op(store: Store) -> None:
    """No service_config row → the pass does nothing and writes no marker."""
    unused = _news(store, "old unused item")

    result = run_retention_pass(store)

    assert (result.claimed, result.ok, result.failed) == (0, 0, 0)
    assert not _is_retired(store, unused)
    with store.pool.connection() as conn:
        marker = conn.execute(
            "SELECT 1 FROM app_settings WHERE key=%s", (FIRST_REPORT_KEY,)
        ).fetchone()
    assert marker is None


def test_first_lit_run_reports_counts_and_deletes_nothing(store: Store) -> None:
    """The ruling's report-only first run: counts land in the marker and
    the BatchResult summary; no ref is touched."""
    _light(store)
    a = _news(store, "unused one")
    b = _news(store, "unused two")

    result = run_retention_pass(store)

    assert result.report_only is True
    assert result.claimed == 2
    assert result.ok == 0 and result.failed == 0
    assert result.extra["counts"] == {"news": 2}
    assert not _is_retired(store, a)
    assert not _is_retired(store, b)

    with store.pool.connection() as conn:
        row = conn.execute(
            "SELECT value FROM app_settings WHERE key=%s", (FIRST_REPORT_KEY,)
        ).fetchone()
    assert row is not None
    doc = json.loads(row[0])
    assert doc["counts"] == {"news": 2}


def test_second_run_retires_unused_news_with_the_reason(store: Store) -> None:
    """An armed pass soft-deletes the unused news and stamps the ruling's
    literal reason on refs.meta — reversibly (the row stays)."""
    _light(store)
    _arm_report_done(store)
    unused = _news(store, "old unused item")

    result = run_retention_pass(store)

    assert result.report_only is False
    assert (result.claimed, result.ok, result.failed) == (1, 1, 0)
    assert _is_retired(store, unused)
    with store.pool.connection() as conn:
        row = conn.execute(
            "SELECT meta FROM refs WHERE ref_id=%s", (unused,)
        ).fetchone()
    assert row is not None
    meta = row[0]
    assert isinstance(meta, dict)
    breadcrumb = meta[META_RETENTION]
    assert breadcrumb["reason"] == NEWS_RETIRE_REASON
    assert "at" in breadcrumb


def test_recent_news_survives_an_armed_pass(store: Store) -> None:
    _light(store)
    _arm_report_done(store)
    fresh = _news(store, "fresh item", days=NEWS_UNUSED_DAYS - 10)

    result = run_retention_pass(store)

    assert result.claimed == 0
    assert not _is_retired(store, fresh)


def test_viewed_news_is_never_selected(store: Store) -> None:
    _light(store)
    _arm_report_done(store)
    viewed = _news(store, "someone read this")
    with store.pool.connection() as conn:
        conn.execute("UPDATE refs SET last_viewed_at=now() WHERE ref_id=%s", (viewed,))

    result = run_retention_pass(store)

    assert result.claimed == 0
    assert not _is_retired(store, viewed)


def test_recalled_news_is_never_selected(store: Store) -> None:
    _light(store)
    _arm_report_done(store)
    recalled = _news(store, "an agent read this")
    with store.pool.connection() as conn:
        conn.execute(
            "UPDATE refs SET last_recalled_at=now() WHERE ref_id=%s", (recalled,)
        )

    result = run_retention_pass(store)

    assert result.claimed == 0
    assert not _is_retired(store, recalled)


def test_news_with_a_read_receipt_is_never_selected(store: Store) -> None:
    """Both receipt shapes protect — the ref-scoped identity and a
    chunk-scoped one."""
    _light(store)
    _arm_report_done(store)
    a = _news(store, "receipt ref scope")
    b = _news(store, "receipt chunk scope")
    _receipt(store, f"news:{a}:-")
    _receipt(store, f"news:{b}:42")

    result = run_retention_pass(store)

    assert result.claimed == 0
    assert not _is_retired(store, a)
    assert not _is_retired(store, b)


def test_expired_receipt_still_protects_until_pruned(store: Store) -> None:
    _light(store)
    _arm_report_done(store)
    old = _news(store, "expired receipt")
    _receipt(store, f"news:{old}:-", live=False)

    assert run_retention_pass(store).claimed == 0
    assert not _is_retired(store, old)


def test_news_with_a_link_is_never_selected(store: Store) -> None:
    """Either direction protects — the news points at something, or
    something points at the news."""
    _light(store)
    _arm_report_done(store)
    a = _news(store, "linked source")
    b = _news(store, "linked target")
    other = seed_ref(store, title="connecting paper", kind="paper")
    store.add_link(src_ref_id=a, dst_ref_id=other, set_by="agent")
    store.add_link(src_ref_id=other, dst_ref_id=b, set_by="agent")

    result = run_retention_pass(store)

    assert result.claimed == 0
    assert not _is_retired(store, a)
    assert not _is_retired(store, b)


def test_news_cited_by_a_live_chunk_is_never_selected(store: Store) -> None:
    _light(store)
    _arm_report_done(store)
    cited = _news(store, "grounding source")
    doc = seed_ref(store, title="draft citing the news", kind="draft")
    seed_chunk(store, ref_id=doc, text=f"The announcement [nw{cited}] says so.")

    result = run_retention_pass(store)

    assert result.claimed == 0
    assert not _is_retired(store, cited)


def test_cited_page_does_not_hide_later_unused_news(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(retention, "MAX_CANDIDATES_PER_FIRE", 2)
    _light(store)
    _arm_report_done(store)
    first = _news(store, "first cited item")
    second = _news(store, "second cited item")
    unused = _news(store, "unused item on the next page")
    doc = seed_ref(store, title="draft citing a list", kind="draft")
    seed_chunk(
        store,
        ref_id=doc,
        text=f"Both [nw{first}, nw{second}] report it.",
    )

    assert run_retention_pass(store).ok == 1
    assert not _is_retired(store, first)
    assert not _is_retired(store, second)
    assert _is_retired(store, unused)


def test_bare_handle_token_without_brackets_is_not_a_citation(store: Store) -> None:
    """A path or code span mentioning nw<id> must not protect — only the
    bracket citation shape does."""
    _light(store)
    _arm_report_done(store)
    unused = _news(store, "mentioned in passing")
    doc = seed_ref(store, title="doc mentioning a handle", kind="markdown")
    seed_chunk(store, ref_id=doc, text=f"see handle nw{unused} in the registry")

    result = run_retention_pass(store)

    assert (result.claimed, result.ok) == (1, 1)
    assert _is_retired(store, unused)


def test_armed_pass_is_idempotent(store: Store) -> None:
    _light(store)
    _arm_report_done(store)
    _news(store, "old unused item")

    first = run_retention_pass(store)
    second = run_retention_pass(store)

    assert first.ok == 1
    assert (second.claimed, second.ok, second.failed) == (0, 0, 0)


def test_acting_fire_does_not_rewrite_the_first_report(store: Store) -> None:
    """The marker is the state that arms acting fires; an acting fire must
    not touch it — its updated_at stays where the report-only fire left
    it."""
    _light(store)
    old = (datetime.now(UTC) - timedelta(days=2)).isoformat()
    doc = json.dumps({"at": old, "counts": {"news": 3}})
    with store.pool.connection() as conn:
        conn.execute(
            "INSERT INTO app_settings (key, value, updated_at) VALUES (%s, %s, %s)",
            (FIRST_REPORT_KEY, doc, old),
        )
        conn.commit()
    _news(store, "old unused item")

    result = run_retention_pass(store)

    assert result.ok == 1
    with store.pool.connection() as conn:
        row = conn.execute(
            "SELECT value, updated_at FROM app_settings WHERE key=%s",
            (FIRST_REPORT_KEY,),
        ).fetchone()
    assert row is not None
    assert json.loads(row[0])["counts"] == {"news": 3}
    assert row[1].replace(tzinfo=UTC) < datetime.now(UTC) - timedelta(days=1)


def _cached_news(store: Store, url: str) -> int:
    ref, _ = store.put_cache_entry(
        kind="news",
        slug=slug_from_text(url, max_len=72),
        title="Original article",
        body_blocks=[ChunkInsert(ord=0, text="Original article body")],
        provider="news",
        request_hash=NewsHandler._hash(url),
        ttl_seconds=None,
        cache_meta={"url": url},
    )
    _backdate(store, ref.id)
    return ref.id


@pytest.mark.parametrize("address", ["handle", "url"])
def test_focused_news_read_survives_receipt_expiry(
    store: Store, runtime_with_store: PrecisRuntime, address: str
) -> None:
    _light(store)
    _arm_report_done(store)
    url = "https://example.com/news/article"
    ref_id = _cached_news(store, url)
    ident = f"nw{ref_id}" if address == "handle" else url
    with bind("install"):
        response = runtime_with_store.dispatch("get", {"kind": "news", "id": ident})
    assert "Original article body" in response
    if address == "handle":
        assert store.has_receipt(owner_key="install", identity=f"news:{ref_id}:-")
    with store.pool.connection() as conn:
        conn.execute("UPDATE read_receipts SET expires_at = now()")
    store.prune_read_receipts()
    with store.pool.connection() as conn:
        row = conn.execute(
            "SELECT last_recalled_at FROM refs WHERE ref_id = %s", (ref_id,)
        ).fetchone()
    assert row is not None and row[0] is not None
    assert run_retention_pass(store).claimed == 0
    assert not _is_retired(store, ref_id)


@pytest.mark.parametrize("late_use", ["read", "link", "citation"])
def test_rechecks_use_arriving_after_selection(
    store: Store,
    runtime_with_store: PrecisRuntime,
    monkeypatch: pytest.MonkeyPatch,
    late_use: str,
) -> None:
    _light(store)
    _arm_report_done(store)
    ref_id = _cached_news(store, "https://example.com/news/late")
    source = seed_ref(store, title="Later use", kind="memory")
    select = retention._news_retention_candidates

    def select_then_use(current_store: Store) -> list[int]:
        targets = select(current_store)
        assert ref_id in targets
        if late_use == "read":
            assert "Original article body" in runtime_with_store.dispatch(
                "get", {"id": f"nw{ref_id}"}
            )
        elif late_use == "link":
            store.add_link(src_ref_id=source, dst_ref_id=ref_id, set_by="agent")
        else:
            seed_chunk(store, ref_id=source, text=f"See [nw{ref_id}].")
        return targets

    monkeypatch.setattr(retention, "_news_retention_candidates", select_then_use)
    result = run_retention_pass(store)
    assert (result.claimed, result.ok, result.failed) == (1, 0, 0)
    assert not _is_retired(store, ref_id)


def test_retired_news_url_can_refill_and_old_handle_can_be_restored(
    store: Store, runtime_with_store: PrecisRuntime, monkeypatch: pytest.MonkeyPatch
) -> None:
    _light(store)
    _arm_report_done(store)
    url = "https://example.com/news/refill"
    old = _cached_news(store, url)

    def fetch(self: NewsHandler, key: str) -> FetchResult:
        assert key == url
        return FetchResult(
            "Refilled article", [ChunkInsert(ord=0, text="New article body")]
        )

    monkeypatch.setattr(NewsHandler, "_fetch", fetch)
    assert run_retention_pass(store).ok == 1
    assert _is_retired(store, old)
    response = runtime_with_store.dispatch("get", {"kind": "news", "id": url})
    assert "New article body" in response
    assert _is_retired(store, old)
    assert store.restore_ref(old)
    assert "Original article body" in runtime_with_store.dispatch(
        "get", {"id": f"nw{old}"}
    )
    assert "New article body" in runtime_with_store.dispatch(
        "get", {"kind": "news", "id": url}
    )


def test_first_fire_persists_counts_through_scheduler(
    store: Store, caplog: pytest.LogCaptureFixture
) -> None:
    _light(store)
    unused = _news(store, "eligible for the first report")
    cadence = next(cad for cad in scheduler.CADENCES if cad.name == SERVICE_NAME)
    handler = BufferedDBLogHandler(_active_dsn(), host_name="retention-test")
    scheduler.log.addHandler(handler)
    try:
        with caplog.at_level(logging.INFO, logger=scheduler.log.name):
            result = scheduler.run_scheduler_pass(
                store, host="retention-test", cadences=(cadence,)
            )
    finally:
        scheduler.log.removeHandler(handler)
        handler.close()
    assert (result.claimed, result.ok, result.failed) == (1, 1, 0)
    with store.pool.connection() as conn:
        row = conn.execute(
            "SELECT payload FROM worker_logs WHERE host = %s AND payload->>'handler' = %s",
            ("retention-test", SERVICE_NAME),
        ).fetchone()
    assert row is not None
    assert row[0]["counts"] == {"news": 1}
    assert row[0]["first_run"] is True
    assert row[0]["report_only"] is True
    assert not _is_retired(store, unused)
