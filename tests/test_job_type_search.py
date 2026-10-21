"""``search(kind='job', args={'job_type': ...})`` + ``id='/cadence'``
(gr502653).

The watch question — "how many <type> jobs were minted/finished/failed
since <t>", "when does cadence <name> fire next" — used to fall back to
``scripts/prod-psql`` over ``refs.meta->>'job_type'`` and
``scheduler_leases``. These tests pin the MCP affordances against a real
Postgres: the listing's "N of K" header, the count's status facets (the
``meta_eq`` channel through ``filtered_ref_clauses``), the ``under=``
scope honouring the same filter, and the cadence lease read.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from precis.dispatch import Hub
from precis.errors import BadInput, NotFound
from precis.handlers.job import JobHandler
from precis.runtime import PrecisRuntime
from precis.store import Store, Tag

pytestmark = pytest.mark.db


@pytest.fixture
def jobs(hub: Hub) -> JobHandler:
    return JobHandler(hub=hub)


def _job(store: Store, *, job_type: str | None, status: str | None) -> int:
    meta = {"job_type": job_type} if job_type is not None else {}
    ref = store.insert_ref(kind="job", slug=None, title="t", meta=meta)
    if status is not None:
        store.add_tag(ref.id, Tag.closed("STATUS", status), set_by="system")
    return ref.id


def _backdate(store: Store, ref_id: int, when: datetime) -> None:
    with store.pool.connection() as conn:
        conn.execute(
            "UPDATE refs SET created_at = %s WHERE ref_id = %s", (when, ref_id)
        )
        conn.commit()


def _facet_counts(body: str) -> dict[str, int]:
    out: dict[str, int] = {}
    for line in body.splitlines():
        m = re.match(r"^  (.+): (\d+)$", line)
        if m:
            out[m.group(1)] = int(m.group(2))
    return out


def _cad() -> str:
    return f"cad-{uuid4().hex}"


class TestJobTypeListing:
    def test_lists_only_matching_type(self, jobs: JobHandler, store: Store) -> None:
        _job(store, job_type="sweep", status="queued")
        _job(store, job_type="sweep", status="succeeded")
        _job(store, job_type="fix_gripe", status="queued")
        _job(store, job_type=None, status="queued")

        resp = jobs.search(job_type="sweep", status="*")
        assert resp.body.startswith("# 2 job entries with job_type 'sweep'"), resp.body
        assert resp.result_count == 2

    def test_status_filter_narrows(self, jobs: JobHandler, store: Store) -> None:
        _job(store, job_type="sweep", status="queued")
        _job(store, job_type="sweep", status="failed")

        resp = jobs.search(job_type="sweep", status="failed")
        assert resp.body.startswith("# 1 job entry"), resp.body

    def test_since_bounds_created(self, jobs: JobHandler, store: Store) -> None:
        old = _job(store, job_type="sweep", status="succeeded")
        _backdate(store, old, datetime.now(UTC) - timedelta(days=10))
        _job(store, job_type="sweep", status="succeeded")

        resp = jobs.search(
            job_type="sweep",
            status="*",
            since=(datetime.now(UTC) - timedelta(days=1)).isoformat(),
        )
        assert resp.body.startswith("# 1 job entry"), resp.body

    def test_q_is_title_lexical(self, jobs: JobHandler, store: Store) -> None:
        ref = store.insert_ref(
            kind="job",
            slug=None,
            title="graph_garden nightly sweep",
            meta={"job_type": "sweep"},
        )
        store.insert_ref(kind="job", slug=None, title="t", meta={"job_type": "sweep"})
        resp = jobs.search(job_type="sweep", q="nightly", status="*")
        assert resp.result_count == 1
        assert str(ref.id) in resp.body

    def test_no_matches_offers_next(self, jobs: JobHandler, store: Store) -> None:
        _job(store, job_type="sweep", status="queued")
        resp = jobs.search(job_type="nonexistent_type", status="*")
        assert "no job entries with job_type 'nonexistent_type'" in resp.body
        assert "meta.job_type,state" in resp.body

    def test_pagination(self, jobs: JobHandler, store: Store) -> None:
        for _ in range(3):
            _job(store, job_type="sweep", status="queued")
        resp = jobs.search(job_type="sweep", status="*", page_size=2, page=2)
        assert resp.body.startswith("# rows 3-3 of 3"), resp.body

    def test_empty_job_type_rejected(self, jobs: JobHandler) -> None:
        with pytest.raises(BadInput, match="non-empty"):
            jobs.search(job_type="  ")

    def test_unknown_view_rejected(self, jobs: JobHandler) -> None:
        with pytest.raises(BadInput, match="unknown view"):
            jobs.search(job_type="sweep", view="tree")

    def test_sort_rejected(self, jobs: JobHandler) -> None:
        with pytest.raises(BadInput, match="sort="):
            jobs.search(job_type="sweep", sort="recency")


class TestJobTypeCount:
    """``view='count'`` + ``job_type=`` — the gripe's exact call shape:
    the filtered set faceted by status."""

    def test_status_facets(self, jobs: JobHandler, store: Store) -> None:
        _job(store, job_type="sweep", status="queued")
        _job(store, job_type="sweep", status="succeeded")
        _job(store, job_type="sweep", status="failed")
        _job(store, job_type="fix_gripe", status="failed")

        resp = jobs.search(view="count", job_type="sweep", group_by="state", status="*")
        assert resp.body.startswith("# 3 job entries"), resp.body
        assert "meta.job_type='sweep'" in resp.body
        assert _facet_counts(resp.body) == {
            "STATUS:queued": 1,
            "STATUS:succeeded": 1,
            "STATUS:failed": 1,
        }

    def test_count_since_window(self, jobs: JobHandler, store: Store) -> None:
        old = _job(store, job_type="sweep", status="succeeded")
        _backdate(store, old, datetime.now(UTC) - timedelta(days=10))
        _job(store, job_type="sweep", status="succeeded")

        resp = jobs.search(
            view="count",
            job_type="sweep",
            status="*",
            since=(datetime.now(UTC) - timedelta(days=1)).isoformat(),
        )
        assert resp.body.startswith("# 1 job entry"), resp.body

    def test_dispatch_end_to_end(
        self, runtime_with_store: PrecisRuntime, store: Store
    ) -> None:
        _job(store, job_type="sweep", status="succeeded")
        _job(store, job_type="sweep", status="failed")
        body = runtime_with_store.dispatch(
            "search",
            {
                "kind": "job",
                "view": "count",
                "status": "*",
                "__extras__": {"job_type": "sweep", "group_by": "state"},
            },
        )
        assert body.startswith("# 2 job entries"), body
        assert "STATUS:succeeded: 1" in body
        assert "STATUS:failed: 1" in body

    def test_dispatch_listing_via_args(
        self, runtime_with_store: PrecisRuntime, store: Store
    ) -> None:
        """The gripe's call shape: ``args={'job_type', 'since'}`` rides
        ``__extras__`` and reaches the handler's listing path."""
        _job(store, job_type="sweep", status="succeeded")
        body = runtime_with_store.dispatch(
            "search",
            {
                "kind": "job",
                "status": "*",
                "__extras__": {
                    "job_type": "sweep",
                    "since": (datetime.now(UTC) - timedelta(days=1)).isoformat(),
                },
            },
        )
        assert "with job_type 'sweep'" in body, body


class TestJobTypeUnder:
    def test_under_scopes_by_type(self, jobs: JobHandler, store: Store) -> None:
        parent = store.insert_ref(kind="todo", slug=None, title="parent", meta={})
        wanted = store.insert_ref(
            kind="job",
            slug=None,
            title="t",
            meta={"job_type": "sweep"},
            parent_id=parent.id,
        )
        store.insert_ref(
            kind="job",
            slug=None,
            title="t",
            meta={"job_type": "fix_gripe"},
            parent_id=parent.id,
        )
        resp = jobs.search(under=parent.id, job_type="sweep")
        assert resp.body.startswith("# 1 job(s)"), resp.body
        assert str(wanted.id) in resp.body

    def test_under_count_by_type(self, jobs: JobHandler, store: Store) -> None:
        parent = store.insert_ref(kind="todo", slug=None, title="parent", meta={})
        store.insert_ref(
            kind="job",
            slug=None,
            title="t",
            meta={"job_type": "sweep"},
            parent_id=parent.id,
        )
        resp = jobs.search(under=parent.id, view="count", job_type="sweep", status="*")
        assert "# 1 job entr" in resp.body, resp.body


class TestCadenceView:
    def test_one_cadence(self, jobs: JobHandler, store: Store) -> None:
        name = _cad()
        assert store.claim_scheduler_lease(name, 3600, "h-test") is True
        body = jobs.get(id=f"/cadence/{name}").body
        assert f"# cadence {name}" in body
        assert "interval: 1h" in body
        assert "next fire:" in body
        assert "last host: h-test" in body

    def test_all_cadences(self, jobs: JobHandler, store: Store) -> None:
        name = _cad()
        store.seed_scheduler_lease(name, 300)
        body = jobs.get(id="/cadence").body
        assert "# cadences" in body
        assert name in body

    def test_unknown_cadence(self, jobs: JobHandler, store: Store) -> None:
        with pytest.raises(NotFound, match="no cadence"):
            jobs.get(id="/cadence/definitely-not-a-cadence")
