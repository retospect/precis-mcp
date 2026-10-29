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

import ast
import re
from datetime import UTC, datetime, timedelta
from typing import Any
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


def _run_search_hint(runtime: PrecisRuntime, call_text: str) -> str:
    """Run a ``search(kind='job', …)`` ``next=`` hint through dispatch —
    the ``next=``-is-a-working-call rule verified by executing it, not
    by matching its text. ``args={…}`` re-nests into ``__extras__``."""
    call = ast.parse(call_text, mode="eval").body
    assert isinstance(call, ast.Call)
    payload: dict[str, Any] = {}
    for kwarg in call.keywords:
        assert kwarg.arg is not None  # ``**kw`` unpacks carry no name
        value = ast.literal_eval(kwarg.value)
        if kwarg.arg == "args":
            payload["__extras__"] = value
        else:
            payload[kwarg.arg] = value
    return runtime.dispatch("search", payload)


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

    def test_refuses_unapplied_kw_with_working_next(
        self, jobs: JobHandler, runtime_with_store: PrecisRuntime
    ) -> None:
        """The job_type branch drops no kw silently either — a key it
        can't apply refuses, and ``next=`` is the caller's own call
        minus the stray key (mcp-authoring rule 2), verified by running
        it."""
        with pytest.raises(BadInput) as exc_info:
            jobs.search(job_type="sweep", status="*", chunk_scope="body")
        err = exc_info.value
        assert "job_type-filtered" in str(err), err
        hint = err.next
        assert isinstance(hint, str)
        assert "chunk_scope" not in hint
        assert "'job_type': 'sweep'" in hint
        out = _run_search_hint(runtime_with_store, hint)
        assert "[error" not in out, out


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

    def test_count_since_id_min(
        self, runtime_with_store: PrecisRuntime, store: Store
    ) -> None:
        """``view='count'`` + top-level ``since=`` stays on the shared
        count path — so a top-level ``id_min=`` narrows it instead of
        being silently dropped by the dated-listing branch."""
        _job(store, job_type="sweep", status="succeeded")
        keep = _job(store, job_type="sweep", status="succeeded")
        body = runtime_with_store.dispatch(
            "search",
            {
                "kind": "job",
                "view": "count",
                "status": "*",
                "since": (datetime.now(UTC) - timedelta(days=1)).isoformat(),
                "id_min": keep,
            },
        )
        assert "[error" not in body, body
        assert body.startswith("# 1 job entr"), body

    def test_since_listing_applies_id_min(self, jobs: JobHandler, store: Store) -> None:
        """The dated plain listing (job_type=None) honours id bounds too."""
        _job(store, job_type="sweep", status="queued")
        keep = _job(store, job_type="sweep", status="queued")
        resp = jobs.search(
            since=(datetime.now(UTC) - timedelta(days=1)).isoformat(),
            status="*",
            id_min=keep,
        )
        assert resp.body.startswith("# 1 job entr"), resp.body
        assert str(keep) in resp.body

    def test_since_listing_refuses_unapplied_kw(self, jobs: JobHandler) -> None:
        """A kw the dated listing can't apply is refused, not swallowed."""
        with pytest.raises(BadInput, match="bogus_arg"):
            jobs.search(
                since=(datetime.now(UTC) - timedelta(days=1)).isoformat(),
                bogus_arg=1,
            )

    def test_since_listing_refusal_next_is_a_working_call(
        self, jobs: JobHandler, runtime_with_store: PrecisRuntime
    ) -> None:
        """``next=`` is the caller's own call minus the unapplied key —
        a concrete ``search(…)`` that works (mcp-authoring rule 2), not
        prose and not a detour through ``view='count'`` (whose ``**_kw``
        would swallow the same key). The accepted-keys list drops
        sort/mode — the queryless listing refuses both."""
        with pytest.raises(BadInput) as exc_info:
            jobs.search(
                since=(datetime.now(UTC) - timedelta(days=1)).isoformat(),
                status="*",
                chunk_scope="body",
            )
        err = exc_info.value
        assert "view='count'" not in str(err), err
        assert "sort" not in str(err), err
        hint = err.next
        assert isinstance(hint, str)
        assert "chunk_scope" not in hint
        out = _run_search_hint(runtime_with_store, hint)
        assert "[error" not in out, out

    def test_since_listing_refusals_do_not_name_job_type(
        self, jobs: JobHandler
    ) -> None:
        """sort=/mode=/view= refused with no job_type must not read
        "job_type-filtered" — the caller never named one — and the
        next= must be a call that works."""
        cases: list[tuple[dict[str, Any], str, str]] = [
            ({"sort": "recency"}, "sort=", "q="),
            ({"mode": "semantic"}, "mode=", "q="),
            ({"view": "tree"}, "unknown view", "view='count'"),
        ]
        for kwargs, marker, want_next in cases:
            with pytest.raises(BadInput, match=marker) as exc_info:
                jobs.search(
                    since=(datetime.now(UTC) - timedelta(days=1)).isoformat(),
                    **kwargs,
                )
            assert "job_type-filtered" not in str(exc_info.value), exc_info.value
            assert want_next in str(exc_info.value.next or ""), exc_info.value

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
