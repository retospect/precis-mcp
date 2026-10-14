"""``search(kind=K, view='count')`` — filtered totals + facet counts
(gr472990 case a).

The view answers "how many" / "by what" over the same filter set the
hit-page paths use (``tags=``/``status=``/``link=``/``q=``/ref-id
includes), so its numbers must match the "N of K" headers those paths
print. These tests pin that against a real Postgres: a
``meta.job_type`` × ``state`` facet on jobs is checked against manual
``GROUP BY`` SQL, and a ``tag:`` axis facet on gripes against the tag
listing's own page header.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta

import pytest

from precis.dispatch import Hub
from precis.errors import BadInput
from precis.handlers.gripe import GripeHandler
from precis.handlers.job import JobHandler
from precis.runtime import PrecisRuntime
from precis.store import Store, Tag
from precis.store.types import ChunkInsert

pytestmark = pytest.mark.db


@pytest.fixture
def jobs(hub: Hub) -> JobHandler:
    return JobHandler(hub=hub)


@pytest.fixture
def gripes(hub: Hub) -> GripeHandler:
    return GripeHandler(hub=hub)


def _job(store: Store, *, job_type: str | None, status: str | None) -> int:
    meta = {"job_type": job_type} if job_type is not None else {}
    ref = store.insert_ref(kind="job", slug=None, title="t", meta=meta)
    if status is not None:
        store.add_tag(ref.id, Tag.closed("STATUS", status), set_by="system")
    return ref.id


def _failed_job(store: Store, *, job_type: str | None, event: str | None) -> int:
    ref_id = _job(store, job_type=job_type, status="failed")
    if event is not None:
        store.chunks.insert_chunks(
            ref_id,
            [ChunkInsert(ord=1, text=event, meta={"chunk_kind": "job_event"})],
        )
    return ref_id


def _gripe(store: Store, text: str, *, tags: tuple[str, ...] = ()) -> int:
    """File a gripe via the SECURITY DEFINER path (ref + body chunk +
    STATUS:open), then attach any extra open tags; returns its id."""
    with store.pool.connection() as conn:
        row = conn.execute("SELECT public.file_gripe_readonly(%s)", (text,)).fetchone()
        assert row is not None
        conn.commit()
        ref_id = int(row[0])
    for t in tags:
        store.add_tag(ref_id, Tag.open(t), set_by="agent")
    return ref_id


def _backdate(store: Store, ref_id: int, when: datetime) -> None:
    with store.pool.connection() as conn:
        conn.execute(
            "UPDATE refs SET created_at = %s WHERE ref_id = %s", (when, ref_id)
        )
        conn.commit()


def _sql_job_cell(store: Store, job_type: str | None, status: str | None) -> int:
    """Manual SQL count for one (job_type, STATUS) group — the number
    ``view='count'`` must reproduce."""
    clauses = ["r.retired_at IS NULL", "r.kind = 'job'"]
    params: list[object] = []
    if job_type is None:
        clauses.append("r.meta->>'job_type' IS NULL")
    else:
        clauses.append("r.meta->>'job_type' = %s")
        params.append(job_type)
    if status is None:
        clauses.append(
            "NOT EXISTS (SELECT 1 FROM ref_tags rt JOIN tags t "
            "ON t.tag_id = rt.tag_id WHERE rt.ref_id = r.ref_id "
            "AND t.namespace = 'STATUS')"
        )
    else:
        clauses.append(
            "r.ref_id IN (SELECT rt.ref_id FROM ref_tags rt JOIN tags t "
            "ON t.tag_id = rt.tag_id WHERE t.namespace = 'STATUS' "
            "AND t.value = %s AND (rt.expires_at IS NULL "
            "OR rt.expires_at > now()))"
        )
        params.append(status)
    with store.pool.connection() as conn:
        row = conn.execute(
            "SELECT count(*) FROM refs r WHERE " + " AND ".join(clauses),
            params,
        ).fetchone()
    assert row is not None
    return int(row[0])


def _facet_counts(body: str) -> dict[str, int]:
    """Parse ``  <v1> | <v2>: N`` group lines out of a count response."""
    out: dict[str, int] = {}
    for line in body.splitlines():
        m = re.match(r"^  (.+): (\d+)$", line)
        if m:
            out[m.group(1)] = int(m.group(2))
    return out


class TestJobTypeStateFacet:
    def test_counts_match_manual_sql(self, jobs: JobHandler, store: Store) -> None:
        _job(store, job_type="fix_gripe", status="succeeded")
        _job(store, job_type="fix_gripe", status="succeeded")
        _job(store, job_type="fix_gripe", status="failed")
        _job(store, job_type="sweep", status="succeeded")
        _job(store, job_type="sweep", status=None)
        _job(store, job_type=None, status="queued")

        resp = jobs.search(view="count", group_by="meta.job_type,state", status="*")

        assert resp.body.startswith("# 6 job entries")
        assert "meta.job_type × state:" in resp.body
        got = _facet_counts(resp.body)
        expected = {
            (jt, st): _sql_job_cell(store, jt, st)
            for jt in ("fix_gripe", "sweep", None)
            for st in ("succeeded", "failed", "queued", None)
        }
        for (jt, st), n in expected.items():
            label = f"{jt or '(none)'} | {f'STATUS:{st}' if st else '(none)'}"
            if n:
                assert got[label] == n, (label, got)
        # The seeded shape, spelled out — guards against a swapped axis.
        assert got["fix_gripe | STATUS:succeeded"] == 2
        assert got["fix_gripe | STATUS:failed"] == 1
        assert got["sweep | STATUS:succeeded"] == 1
        assert got["sweep | (none)"] == 1
        assert got["(none) | STATUS:queued"] == 1
        assert len(got) == 5

    def test_state_facet_only(self, jobs: JobHandler, store: Store) -> None:
        _job(store, job_type="sweep", status="queued")
        _job(store, job_type="sweep", status="queued")
        _job(store, job_type="sweep", status="succeeded")
        resp = jobs.search(view="count", group_by="state", status="*")
        got = _facet_counts(resp.body)
        assert got == {"STATUS:queued": 2, "STATUS:succeeded": 1}
        assert "state:" in resp.body

    def test_status_filter_narrows_count(self, jobs: JobHandler, store: Store) -> None:
        _job(store, job_type="sweep", status="queued")
        _job(store, job_type="sweep", status="succeeded")
        resp = jobs.search(view="count", status="queued")
        assert resp.body.startswith("# 1 job entry")


class TestGripeTagAxisFacet:
    def test_matches_search_page_headers(
        self, gripes: GripeHandler, store: Store
    ) -> None:
        _gripe(store, "search blows up on percent", tags=("area:token-waste",))
        _gripe(store, "another token-waste gripe", tags=("area:token-waste",))
        _gripe(store, "capability gap gripe", tags=("area:capability-gap",))
        _gripe(store, "unaxised gripe")

        resp = gripes.search(view="count", group_by="tag:area")

        assert resp.body.startswith("# 4 gripe entries")
        got = _facet_counts(resp.body)
        # Each group's count equals what the tag-filtered listing's own
        # "# N gripe entries tagged [...]" header reports for that tag.
        for axis_value in ("area:token-waste", "area:capability-gap"):
            page = gripes.search(tags=[axis_value])
            m = re.match(r"^# (\d+) gripe entr", page.body)
            assert m is not None, page.body
            assert got[axis_value] == int(m.group(1))
        assert got["area:token-waste"] == 2
        assert got["area:capability-gap"] == 1
        assert got["(none)"] == 1

    def test_default_status_filter_and_terminal_note(
        self, gripes: GripeHandler, store: Store
    ) -> None:
        open_id = _gripe(store, "live gripe")
        done_id = _gripe(store, "closed gripe")
        # Grips start STATUS:open; displace one to a terminal value.
        gripes.tag(id=done_id, add=["STATUS:done"])
        del open_id

        resp = gripes.search(view="count", group_by="state")
        assert resp.body.startswith("# 1 gripe entry")
        assert "(default: live statuses)" in resp.body
        assert "1 terminal entries hidden; status='*' to include" in resp.body
        assert _facet_counts(resp.body) == {"STATUS:open": 1}

        resp_all = gripes.search(view="count", group_by="state", status="*")
        assert resp_all.body.startswith("# 2 gripe entries")
        assert _facet_counts(resp_all.body) == {
            "STATUS:open": 1,
            "STATUS:done": 1,
        }

    def test_total_only_shows_group_by_hint(
        self, gripes: GripeHandler, store: Store
    ) -> None:
        _gripe(store, "one")
        _gripe(store, "two")
        resp = gripes.search(view="count")
        assert resp.body.startswith("# 2 gripe entries")
        assert "group_by" in resp.body


class TestCountTimeWindow:
    """``since=``/``until=`` bound ``refs.created_at`` on ``view='count'``
    (mcp-introspection gap closures 2026-10-14)."""

    def test_since_until_bound_the_count(self, jobs: JobHandler, store: Store) -> None:
        old = _job(store, job_type="sweep", status="succeeded")
        new = _job(store, job_type="sweep", status="succeeded")
        now = datetime.now(UTC)
        _backdate(store, old, now - timedelta(days=10))
        _backdate(store, new, now - timedelta(hours=1))

        resp = jobs.search(
            view="count", status="*", since=(now - timedelta(days=1)).isoformat()
        )
        assert resp.body.startswith("# 1 job entry"), resp.body

        resp = jobs.search(
            view="count", status="*", until=(now - timedelta(days=1)).isoformat()
        )
        assert resp.body.startswith("# 1 job entry"), resp.body

        resp = jobs.search(
            view="count",
            status="*",
            since=(now - timedelta(days=30)).isoformat(),
            until=(now - timedelta(minutes=30)).isoformat(),
        )
        assert resp.body.startswith("# 2 job entries"), resp.body
        assert "created [" in resp.body

    def test_since_renders_in_filters(self, jobs: JobHandler, store: Store) -> None:
        _job(store, job_type="sweep", status="succeeded")
        resp = jobs.search(view="count", status="*", since="2020-01-01")
        assert "created [2020-01-01" in resp.body
        assert resp.body.startswith("# 1 job entry")

    def test_bad_since_raises(self, jobs: JobHandler) -> None:
        with pytest.raises(BadInput, match="ISO date"):
            jobs.search(view="count", since="yesterday-ish")

    def test_since_off_count_view_raises(self, jobs: JobHandler) -> None:
        # Reached only on a direct handler call (dispatch routes dated
        # non-count searches to the source-search primitive).
        with pytest.raises(BadInput, match="view='count'"):
            jobs.search(since="2024-01-01", status="*")

    def test_since_via_dispatch(
        self, runtime_with_store: PrecisRuntime, store: Store
    ) -> None:
        ref_id = _job(store, job_type="sweep", status="succeeded")
        _backdate(store, ref_id, datetime.now(UTC) - timedelta(days=10))
        body = runtime_with_store.dispatch(
            "search",
            {
                "kind": "job",
                "view": "count",
                "status": "*",
                "since": (datetime.now(UTC) - timedelta(days=1)).isoformat(),
            },
        )
        assert body.startswith("# 0 job entries"), body


class TestJobErrorHeads:
    """The count view's failed-job trailer (top error heads per failing
    job_type) — the "which failure family" drill that used to need raw
    refs+chunks SQL."""

    def test_heads_grouped_per_job_type(self, jobs: JobHandler, store: Store) -> None:
        _failed_job(store, job_type="fix_gripe", event="gripe 12345 has no parent")
        _failed_job(store, job_type="fix_gripe", event="gripe 67890 has no parent")
        _failed_job(store, job_type="sweep", event="disk full")
        _failed_job(store, job_type="sweep", event=None)
        _job(store, job_type="sweep", status="succeeded")

        resp = jobs.search(view="count", group_by="state", status="*")

        assert "## error heads — 4 failed job(s) in this set" in resp.body
        assert "fix_gripe (2):" in resp.body
        # digits fold: two different gripe ids are one family
        assert "2x gripe # has no parent" in resp.body
        assert "sweep (2):" in resp.body
        assert "1x disk full" in resp.body
        assert "1x (no job_event recorded)" in resp.body

    def test_heads_scoped_by_since(self, jobs: JobHandler, store: Store) -> None:
        old = _failed_job(store, job_type="sweep", event="old failure")
        _backdate(store, old, datetime.now(UTC) - timedelta(days=10))
        _failed_job(store, job_type="sweep", event="new failure")

        resp = jobs.search(
            view="count",
            status="*",
            since=(datetime.now(UTC) - timedelta(days=1)).isoformat(),
        )
        assert "1x new failure" in resp.body
        assert "old failure" not in resp.body

    def test_no_failed_jobs_no_trailer(self, jobs: JobHandler, store: Store) -> None:
        _job(store, job_type="sweep", status="succeeded")
        resp = jobs.search(view="count", status="*")
        assert "error heads" not in resp.body


class TestPassthroughKinds:
    """todo and memory own a ``view=`` dispatch ahead of the base
    ``search()`` — pin that ``'count'`` still reaches the shared path."""

    def test_todo_count(self, hub: Hub, store: Store) -> None:
        from precis.handlers.todo import TodoHandler

        for status in ("open", "open", "done"):
            ref = store.insert_ref(kind="todo", slug=None, title="t", meta={})
            store.add_tag(ref.id, Tag.closed("STATUS", status), set_by="system")
        resp = TodoHandler(hub=hub).search(view="count", group_by="state", status="*")
        assert _facet_counts(resp.body) == {"STATUS:open": 2, "STATUS:done": 1}

    def test_todo_count_rejects_under(self, hub: Hub, store: Store) -> None:
        from precis.handlers.todo import TodoHandler

        with pytest.raises(BadInput, match="under="):
            TodoHandler(hub=hub).search(view="count", under=1)

    def test_memory_count(self, hub: Hub, store: Store) -> None:
        from precis.handlers.memory import MemoryHandler

        for section in ("threads", "threads", "gotchas"):
            ref = store.insert_ref(kind="memory", slug=None, title="t", meta={})
            store.add_tag(ref.id, Tag.open(f"section:{section}"), set_by="agent")
        resp = MemoryHandler(hub=hub).search(view="count", group_by="tag:section")
        assert _facet_counts(resp.body) == {
            "section:threads": 2,
            "section:gotchas": 1,
        }


class TestCountValidation:
    def test_unknown_group_by_term(self, gripes: GripeHandler) -> None:
        with pytest.raises(BadInput, match="unknown group_by term"):
            gripes.search(view="count", group_by="nonsense")

    def test_unknown_view(self, gripes: GripeHandler) -> None:
        with pytest.raises(BadInput, match="unknown view"):
            gripes.search(view="histogram")

    def test_dispatch_requires_single_kind(
        self, runtime_with_store: PrecisRuntime
    ) -> None:
        body = runtime_with_store.dispatch("search", {"view": "count"})
        assert "kind=" in body
        body = runtime_with_store.dispatch(
            "search", {"view": "count", "kind": "gripe,todo"}
        )
        assert "one kind" in body

    def test_dispatch_end_to_end(
        self, runtime_with_store: PrecisRuntime, store: Store
    ) -> None:
        _gripe(store, "through the wire", tags=("area:token-waste",))
        body = runtime_with_store.dispatch(
            "search",
            {
                "kind": "gripe",
                "view": "count",
                "__extras__": {"group_by": "tag:area"},
            },
        )
        assert body.startswith("# 1 gripe entry")
        assert "area:token-waste: 1" in body

    def test_rejected_arg_combos(self, runtime_with_store: PrecisRuntime) -> None:
        # since=/until= are ACCEPTED on view='count' (mcp-introspection
        # gap closures 2026-10-14) — they bound refs.created_at. What
        # still has no meaning on a count is rejected.
        for extra in (
            {"sort": "recency"},
            {"angle": 1.0},
        ):
            body = runtime_with_store.dispatch(
                "search", {"kind": "gripe", "view": "count", **extra}
            )
            assert "not supported with view='count'" in body, (extra, body)
