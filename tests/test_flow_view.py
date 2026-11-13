"""``search(kind=K, view='flow')`` — opened vs closed per bucket plus
open-to-close percentiles (gr633906).

The burn-down read the coordinator used to write as ad-hoc
``ref_tags``/``tags`` SQL: opens bucket on ``refs.created_at``, closes
on the set-time of the ref's *current* terminal ``STATUS:`` tag, and
``since=``/``until=`` bound the event clock on both sides.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta

import pytest

from precis.runtime import PrecisRuntime
from precis.store import Store, Tag

pytestmark = pytest.mark.db


def _gripe(store: Store, *open_tags: str) -> int:
    """The SECURITY DEFINER gripe path (ref + body + STATUS:open in one
    transaction) — insert_ref + separate tag writes trip the
    gripe_status_check trigger."""
    with store.pool.connection() as conn:
        row = conn.execute("SELECT public.file_gripe_readonly(%s)", ("t",)).fetchone()
        assert row is not None
        conn.commit()
        ref_id = int(row[0])
    for t in open_tags:
        store.add_tag(ref_id, Tag.open(t), set_by="agent")
    return ref_id


def _job(
    store: Store, *, job_type: str | None = None, status: str | None = None
) -> int:
    meta = {"job_type": job_type} if job_type is not None else {}
    ref = store.insert_ref(kind="job", slug=None, title="t", meta=meta)
    if status is not None:
        store.add_tag(ref.id, Tag.closed("STATUS", status), set_by="system")
    return ref.id


def _close_gripe(store: Store, ref_id: int, status: str = "done") -> None:
    """Flip STATUS:open → a terminal value in one row (replace_prefix
    keeps the deferred gripe_status_check invariant: exactly one
    STATUS)."""
    store.add_tag(
        ref_id, Tag.closed("STATUS", status), set_by="agent", replace_prefix=True
    )


def _backdate_ref(store: Store, ref_id: int, when: datetime) -> None:
    with store.pool.connection() as conn:
        conn.execute(
            "UPDATE refs SET created_at = %s WHERE ref_id = %s", (when, ref_id)
        )
        conn.commit()


def _backdate_status(store: Store, ref_id: int, when: datetime) -> None:
    """Move the ref's STATUS tag row's set-time — the clock the flow
    view buckets closes on."""
    with store.pool.connection() as conn:
        conn.execute(
            "UPDATE ref_tags rt SET created_at = %s FROM tags t "
            "WHERE rt.tag_id = t.tag_id AND rt.ref_id = %s "
            "AND t.namespace = 'STATUS'",
            (when, ref_id),
        )
        conn.commit()


def _day(days_ago: float) -> datetime:
    return datetime.now(UTC) - timedelta(days=days_ago)


def _bucket_line(body: str, day: datetime) -> tuple[int, int, int] | None:
    """Parse ``<date> opened closed net`` out of a flow render."""
    m = re.search(
        rf"^\s*{day.strftime('%Y-%m-%d')}\s+(\d+)\s+(\d+)\s+([+-]?\d+)\s*$",
        body,
        re.MULTILINE,
    )
    if m is None:
        return None
    return int(m.group(1)), int(m.group(2)), int(m.group(3))


class TestFlowView:
    def test_opened_closed_buckets(
        self, runtime_with_store: PrecisRuntime, store: Store
    ) -> None:
        today = datetime.now(UTC)
        old, recent = _gripe(store), _gripe(store)
        _backdate_ref(store, old, _day(3))
        _close_gripe(store, old)
        _backdate_status(store, old, _day(2))
        body = runtime_with_store.dispatch(
            "search", {"kind": "gripe", "view": "flow", "since": "2020-01-01"}
        )
        assert "# gripe flow — by day" in body
        assert "(closed = done, wontfix)" in body
        assert "opened 2 · closed 1 · net +1" in body
        assert "open now 1" in body
        assert _bucket_line(body, _day(3)) == (1, 0, 1)
        assert _bucket_line(body, _day(2)) == (0, 1, -1)
        assert _bucket_line(body, today) == (1, 0, 1)

    def test_open_now_counts_unclosed(
        self, runtime_with_store: PrecisRuntime, store: Store
    ) -> None:
        _gripe(store)
        closed = _gripe(store)
        _close_gripe(store, closed)
        body = runtime_with_store.dispatch("search", {"kind": "gripe", "view": "flow"})
        assert "open now 1" in body

    def test_time_to_close_percentiles(
        self, runtime_with_store: PrecisRuntime, store: Store
    ) -> None:
        g1, g2 = _gripe(store), _gripe(store)
        _close_gripe(store, g1)
        _close_gripe(store, g2)
        _backdate_ref(store, g1, _day(2))
        _backdate_status(store, g1, _day(1))
        _backdate_ref(store, g2, _day(5))
        _backdate_status(store, g2, _day(0.5))
        body = runtime_with_store.dispatch("search", {"kind": "gripe", "view": "flow"})
        # Durations 1d and 4.5d: continuous median = 2.75d, p90 above it.
        m = re.search(
            r"time to close over 2 windowed closes: median (\S+) · p90 (\S+) · max (\S+)",
            body,
        )
        assert m, body
        assert m.group(1) == "2.8d"
        assert m.group(3) == "4.5d"

    def test_close_before_window_still_counts_when_tag_set_in_window(
        self, runtime_with_store: PrecisRuntime, store: Store
    ) -> None:
        """A ref minted before the window but closed inside it lands on
        the closed side only — the burn-down semantic."""
        g = _gripe(store)
        _close_gripe(store, g)
        _backdate_ref(store, g, _day(30))
        _backdate_status(store, g, _day(1))
        body = runtime_with_store.dispatch("search", {"kind": "gripe", "view": "flow"})
        assert "opened 0 · closed 1 · net -1" in body
        assert _bucket_line(body, _day(30)) is None
        assert _bucket_line(body, _day(1)) == (0, 1, -1)

    def test_close_outside_window_dropped(
        self, runtime_with_store: PrecisRuntime, store: Store
    ) -> None:
        g = _gripe(store)
        _close_gripe(store, g)
        _backdate_ref(store, g, _day(30))
        _backdate_status(store, g, _day(20))
        body = runtime_with_store.dispatch("search", {"kind": "gripe", "view": "flow"})
        assert "opened 0 · closed 0" in body
        assert "time to close" not in body

    def test_closed_override(
        self, runtime_with_store: PrecisRuntime, store: Store
    ) -> None:
        done, wontfix = _gripe(store), _gripe(store)
        _close_gripe(store, done, "done")
        _close_gripe(store, wontfix, "wontfix")
        body = runtime_with_store.dispatch(
            "search",
            {
                "kind": "gripe",
                "view": "flow",
                "__extras__": {"closed": ["wontfix"]},
            },
        )
        assert "(closed = wontfix)" in body
        assert "closed 1" in body
        assert "open now 1" in body  # done isn't closed under the override

    def test_closed_comma_string(
        self, runtime_with_store: PrecisRuntime, store: Store
    ) -> None:
        done, wontfix = _gripe(store), _gripe(store)
        _close_gripe(store, done, "done")
        _close_gripe(store, wontfix, "wontfix")
        body = runtime_with_store.dispatch(
            "search",
            {
                "kind": "gripe",
                "view": "flow",
                "__extras__": {"closed": "STATUS:done,wontfix"},
            },
        )
        assert "(closed = done, wontfix)" in body
        assert "closed 2" in body

    def test_tags_filter(self, runtime_with_store: PrecisRuntime, store: Store) -> None:
        _gripe(store, "area:x")
        tagged = _gripe(store, "area:x")
        _gripe(store, "area:y")
        _close_gripe(store, tagged)
        body = runtime_with_store.dispatch(
            "search",
            {"kind": "gripe", "view": "flow", "tags": ["area:x"]},
        )
        assert "filtered: tags=['area:x']" in body
        assert "opened 2 · closed 1" in body

    def test_status_filter(
        self, runtime_with_store: PrecisRuntime, store: Store
    ) -> None:
        open_g, done_g = _gripe(store), _gripe(store)
        _close_gripe(store, done_g)
        body = runtime_with_store.dispatch(
            "search",
            {"kind": "gripe", "view": "flow", "status": "done"},
        )
        assert "opened 1 · closed 1" in body
        assert "open now 0" in body

    def test_job_flow_and_job_type(
        self, runtime_with_store: PrecisRuntime, store: Store
    ) -> None:
        _job(store, job_type="run_slice", status="succeeded")
        _job(store, job_type="run_slice", status="failed")
        _job(store, job_type="sweep", status="running")
        body = runtime_with_store.dispatch("search", {"kind": "job", "view": "flow"})
        assert "(closed = succeeded, failed, cancelled, timed-out)" in body
        assert "closed 2" in body
        assert "open now 1" in body
        scoped = runtime_with_store.dispatch(
            "search",
            {
                "kind": "job",
                "view": "flow",
                "__extras__": {"job_type": "run_slice"},
            },
        )
        assert "job_type='run_slice'" in scoped
        assert "closed 2" in scoped
        assert "open now 0" in scoped

    def test_bucket_sizes(
        self, runtime_with_store: PrecisRuntime, store: Store
    ) -> None:
        _gripe(store)
        for bucket in ("week", "month", "hour"):
            body = runtime_with_store.dispatch(
                "search",
                {
                    "kind": "gripe",
                    "view": "flow",
                    "__extras__": {"bucket": bucket},
                },
            )
            assert f"— by {bucket}," in body
            assert "opened 1" in body

    def test_age_spelling_window(
        self, runtime_with_store: PrecisRuntime, store: Store
    ) -> None:
        old = _gripe(store)
        _backdate_ref(store, old, _day(30))
        _gripe(store)
        body = runtime_with_store.dispatch(
            "search", {"kind": "gripe", "view": "flow", "since": "7d"}
        )
        assert "opened 1" in body

    def test_get_verb_serves_flow(
        self, runtime_with_store: PrecisRuntime, store: Store
    ) -> None:
        _gripe(store)
        via_get = runtime_with_store.dispatch("get", {"kind": "gripe", "view": "flow"})
        assert "# gripe flow — by day" in via_get

    def test_default_window_named(
        self, runtime_with_store: PrecisRuntime, store: Store
    ) -> None:
        _gripe(store)
        body = runtime_with_store.dispatch("search", {"kind": "gripe", "view": "flow"})
        assert "(default)" in body


class TestFlowErrors:
    def test_requires_single_kind(self, runtime_with_store: PrecisRuntime) -> None:
        body = runtime_with_store.dispatch(
            "search", {"kind": "gripe,todo", "view": "flow"}
        )
        assert "one kind" in body

    def test_unknown_kind(self, runtime_with_store: PrecisRuntime) -> None:
        body = runtime_with_store.dispatch(
            "search", {"kind": "nonsense", "view": "flow"}
        )
        assert "unknown kind" in body

    def test_bad_bucket(self, runtime_with_store: PrecisRuntime) -> None:
        body = runtime_with_store.dispatch(
            "search",
            {"kind": "gripe", "view": "flow", "__extras__": {"bucket": "year"}},
        )
        assert "args.bucket" in body

    def test_bad_closed_value(
        self, runtime_with_store: PrecisRuntime, store: Store
    ) -> None:
        _gripe(store)
        body = runtime_with_store.dispatch(
            "search",
            {
                "kind": "gripe",
                "view": "flow",
                "__extras__": {"closed": ["bogus"]},
            },
        )
        assert "STATUS vocabulary" in body

    def test_refused_args(self, runtime_with_store: PrecisRuntime) -> None:
        body = runtime_with_store.dispatch(
            "search", {"kind": "gripe", "view": "flow", "q": "x"}
        )
        assert "q= is not supported with view='flow'" in body

    def test_wrapper_page_default_tolerated_but_real_paging_refused(
        self, runtime_with_store: PrecisRuntime, store: Store
    ) -> None:
        """td658272/gr657066: ``tools.core.search``'s payload always
        carries ``page=1`` — the wrapper default, not caller intent —
        so it must not refuse ``view='flow'``; a real ``page=`` still
        does (the convention ``/queue`` listings already use)."""
        _gripe(store)
        body = runtime_with_store.dispatch(
            "search", {"kind": "gripe", "view": "flow", "page": 1}
        )
        assert "# gripe flow" in body
        body = runtime_with_store.dispatch(
            "search", {"kind": "gripe", "view": "flow", "page": 2}
        )
        assert "page= is not supported with view='flow'" in body

    def test_unknown_extras(self, runtime_with_store: PrecisRuntime) -> None:
        body = runtime_with_store.dispatch(
            "search",
            {"kind": "gripe", "view": "flow", "__extras__": {"group_by": "state"}},
        )
        assert "not supported with view='flow'" in body

    def test_inverted_window(self, runtime_with_store: PrecisRuntime) -> None:
        body = runtime_with_store.dispatch(
            "search",
            {
                "kind": "gripe",
                "view": "flow",
                "since": "2026-11-10",
                "until": "2026-11-01",
            },
        )
        assert "not before" in body

    def test_job_type_on_non_job_kind(
        self, runtime_with_store: PrecisRuntime, store: Store
    ) -> None:
        _gripe(store)
        body = runtime_with_store.dispatch(
            "search",
            {
                "kind": "gripe",
                "view": "flow",
                "__extras__": {"job_type": "run_slice"},
            },
        )
        assert "kind='job' only" in body
