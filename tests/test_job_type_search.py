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
        m = re.match(r"^  (.+?): (\d+)", line)
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

    def test_mode_lexical_names_the_same_leg(
        self, jobs: JobHandler, store: Store
    ) -> None:
        """gr608736: ``mode='lexical'`` asks for what the job_type-filtered
        path already is — the title-lexical ``q=`` leg — so it applies
        instead of refusing (callers kept repeating the refused call)."""
        ref = store.insert_ref(
            kind="job",
            slug=None,
            title="graph_garden nightly sweep",
            meta={"job_type": "sweep"},
        )
        store.insert_ref(kind="job", slug=None, title="t", meta={"job_type": "sweep"})
        resp = jobs.search(job_type="sweep", q="nightly", mode="lexical", status="*")
        assert resp.result_count == 1
        assert str(ref.id) in resp.body

    def test_ranked_mode_spellings_still_refuse(self, jobs: JobHandler) -> None:
        """The modes that ask for a leg this path doesn't run —
        ``semantic``/``hybrid`` (vector) and ``verbatim``/``literal``
        (exact-match) — keep refusing, with the same label."""
        for m in ("semantic", "hybrid", "verbatim", "literal"):
            with pytest.raises(BadInput, match="mode=") as exc_info:
                jobs.search(job_type="sweep", q="nightly", mode=m)
            assert "job_type-filtered" in str(exc_info.value), exc_info.value

    def test_mode_lexical_on_the_dated_listing(
        self, jobs: JobHandler, store: Store
    ) -> None:
        """The job_type=None sibling (the dated listing) takes the same
        lexical spelling — ``q=`` is title-lexical there too."""
        _job(store, job_type="sweep", status="queued")
        resp = jobs.search(
            status="*",
            since=(datetime.now(UTC) - timedelta(days=1)).isoformat(),
            mode="lexical",
        )
        assert resp.body.startswith("# 1 job entry"), resp.body

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

    def test_dispatch_q_mode_lexical_job_type(
        self, runtime_with_store: PrecisRuntime, store: Store
    ) -> None:
        """The gripe's call shape (gr608736): ``search(kind='job',
        q='…', mode='lexical', args={'job_type': …})`` — refused as
        "mode= has no ranked-search meaning" even though the lexical
        spelling names the leg that runs."""
        store.insert_ref(
            kind="job",
            slug=None,
            title="run_slice nightly attempt",
            meta={"job_type": "run_slice"},
        )
        body = runtime_with_store.dispatch(
            "search",
            {
                "kind": "job",
                "q": "nightly",
                "mode": "lexical",
                "status": "*",
                "__extras__": {"job_type": "run_slice"},
            },
        )
        assert "[error" not in body, body
        assert "with job_type 'run_slice'" in body

    def test_empty_job_type_rejected(self, jobs: JobHandler) -> None:
        with pytest.raises(BadInput, match="non-empty"):
            jobs.search(job_type="  ")

    def test_unknown_view_rejected(self, jobs: JobHandler) -> None:
        with pytest.raises(BadInput, match="unknown view"):
            jobs.search(job_type="sweep", view="tree")

    def test_sort_rejected(self, jobs: JobHandler) -> None:
        with pytest.raises(BadInput, match="sort="):
            jobs.search(job_type="sweep", sort="relevance")

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
            ({"sort": "relevance"}, "sort=", "q="),
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

    def test_bare_job_type_facet_alias(self, jobs: JobHandler, store: Store) -> None:
        """``group_by='job_type'`` — the spelling agents reach for —
        reads as ``meta.job_type`` (gr536316)."""
        _job(store, job_type="sweep", status="succeeded")
        _job(store, job_type="sweep", status="failed")
        _job(store, job_type="fix_gripe", status="failed")

        resp = jobs.search(view="count", group_by="job_type,state", status="*")
        assert _facet_counts(resp.body) == {
            "sweep | STATUS:succeeded": 1,
            "sweep | STATUS:failed": 1,
            "fix_gripe | STATUS:failed": 1,
        }

    def test_group_rows_carry_newest_oldest(
        self, jobs: JobHandler, store: Store
    ) -> None:
        """Each facet row names its newest/oldest member + created
        bounds — the "last run / how far back" half of the count."""
        old = _job(store, job_type="sweep", status="failed")
        _backdate(store, old, datetime.now(UTC) - timedelta(days=10))
        new = _job(store, job_type="sweep", status="failed")

        resp = jobs.search(view="count", group_by="job_type", status="*")
        line = next(l for l in resp.body.splitlines() if "sweep" in l and "newest" in l)
        assert f"newest jo{new}" in line
        assert f"oldest jo{old}" in line

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


class TestJobWindowListing:
    """``args={'since': …}`` with no ``job_type=`` — the same filtered
    listing over all types (gr536316); bounds take ISO or an age
    (``'24'``/``'6h'``/``'7d'``)."""

    def test_since_hours_lists_all_types(self, jobs: JobHandler, store: Store) -> None:
        old = _job(store, job_type="sweep", status="succeeded")
        _backdate(store, old, datetime.now(UTC) - timedelta(days=10))
        _job(store, job_type="fix_gripe", status="failed")

        resp = jobs.search(status="*", since="24")
        assert resp.body.startswith("# 1 job entry"), resp.body
        assert " created [" in resp.body

    def test_age_spellings(self, jobs: JobHandler, store: Store) -> None:
        old = _job(store, job_type="sweep", status="succeeded")
        _backdate(store, old, datetime.now(UTC) - timedelta(days=10))
        _job(store, job_type="sweep", status="succeeded")

        day = jobs.search(job_type="sweep", status="*", since="24h")
        assert day.body.startswith("# 1 job entry"), day.body
        fortnight = jobs.search(job_type="sweep", status="*", since="14d")
        assert fortnight.body.startswith("# 2 job entries"), fortnight.body

    def test_bad_since_rejected(self, jobs: JobHandler) -> None:
        with pytest.raises(BadInput, match="ISO date"):
            jobs.search(job_type="sweep", since="yesterday-ish")

    def test_bare_year_rejected(self, jobs: JobHandler) -> None:
        """gr536316 fix round 1: ``since='2026'`` is a mistyped ISO
        year, not "2026 hours ago" — the bare-number reading silently
        scoped to ~84 days back. Refused, with ``next=`` showing both
        spellings."""
        with pytest.raises(BadInput, match="ambiguous") as exc:
            jobs.search(job_type="sweep", since="2026")
        assert "2026h" in str(exc.value.next)
        assert "2026-01-01" in str(exc.value.next)

    def test_bare_over_year_rejected(self, jobs: JobHandler) -> None:
        with pytest.raises(BadInput, match="ambiguous"):
            jobs.search(job_type="sweep", since="9000")

    def test_suffixed_and_iso_spellings_still_parse(self, jobs: JobHandler) -> None:
        # An explicit h/d unit says "I meant an age" — unbounded; an
        # ISO year arrives as a full date.
        assert "job entr" in jobs.search(job_type="sweep", since="2026h").body
        assert "job entr" in jobs.search(job_type="sweep", since="2026-01-01").body
        assert "job entr" in jobs.search(job_type="sweep", since="8760").body

    def test_since_with_other_view_rejected(self, jobs: JobHandler) -> None:
        with pytest.raises(BadInput, match="since"):
            jobs.search(view="ids", since="24")

    def test_count_takes_age_spelling(self, jobs: JobHandler, store: Store) -> None:
        old = _job(store, job_type="sweep", status="failed")
        _backdate(store, old, datetime.now(UTC) - timedelta(days=10))
        _job(store, job_type="sweep", status="failed")

        resp = jobs.search(view="count", status="*", since="24")
        assert resp.body.startswith("# 1 job entry"), resp.body


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


# ── relative since/until, sort='recent', view='parked' (gr735229, gr735226) ──


def _titled(store: Store, title: str, job_type: str, status: str = "queued") -> int:
    ref = store.insert_ref(
        kind="job", slug=None, title=title, meta={"job_type": job_type}
    )
    store.add_tag(ref.id, Tag.closed("STATUS", status), set_by="system")
    return ref.id


def _touch(store: Store, ref_id: int, when: datetime) -> None:
    with store.pool.connection() as conn:
        conn.execute(
            "UPDATE refs SET updated_at = %s WHERE ref_id = %s", (when, ref_id)
        )
        conn.commit()


def _listed_ids(body: str) -> list[int]:
    """Job ids in the order a listing renders them (``jo<N>`` handles)."""
    return [int(m) for m in re.findall(r"\bjo(\d+)\b", body)]


class TestRelativeWindowAndRecentSort:
    def test_span_since_until_window_the_listing(
        self, runtime_with_store: PrecisRuntime, store: Store
    ) -> None:
        now = datetime.now(UTC)
        old = _titled(store, "sweep alpha old", "sweep")
        new = _titled(store, "sweep alpha new", "sweep")
        _backdate(store, old, now - timedelta(hours=3))
        _backdate(store, new, now - timedelta(minutes=10))
        base = {"kind": "job", "q": "alpha", "sort": "recent", "status": "*"}

        def ids(**window: str) -> list[int]:
            body = runtime_with_store.dispatch("search", {**base, **window})
            assert "[error" not in body, body
            return _listed_ids(body)

        assert ids(since="1h") == [new]
        assert ids(since="30m") == [new]
        assert ids(since="1d") == [new, old]
        assert ids(until="1h") == [old]
        assert ids(since="4h", until="1h") == [old]
        assert ids(since="1w") == [new, old]

    def test_span_since_reaches_the_ranked_source_search(
        self, runtime_with_store: PrecisRuntime
    ) -> None:
        """A q-bearing since= with no sort= takes the source-search
        primitive, which used to refuse '2h' (gr735229)."""
        body = runtime_with_store.dispatch(
            "search", {"kind": "job", "q": "zz_no_such_token_zz", "since": "2h"}
        )
        assert "[error" not in body, body

    def test_count_view_takes_span_bound(
        self, runtime_with_store: PrecisRuntime, store: Store
    ) -> None:
        """gr735599: the reported call — q= + view='count' + group_by +
        ``since='30m'`` — routes to the job handler's count path, whose
        pre-span bound parser took only ``[hd]`` ages and refused '30m'
        with "an age in hours/days"."""
        _titled(store, "release_cut run new", "release_cut", "succeeded")
        old = _titled(store, "release_cut run old", "release_cut", "failed")
        _backdate(store, old, datetime.now(UTC) - timedelta(hours=2))
        body = runtime_with_store.dispatch(
            "search",
            {
                "kind": "job",
                "q": "release_cut",
                "since": "30m",
                "view": "count",
                "page": 1,
                "__extras__": {"group_by": "state"},
            },
        )
        assert "[error" not in body, body
        assert "# 1 job entr" in body, body

    @pytest.mark.parametrize(
        "call",
        [
            {"q": "x", "since": "soon"},  # source-search primitive
            {"q": "x", "sort": "recent", "since": "soon"},  # job handler
            {"since": "soon"},  # dated listing
            {"until": "2h ago"},
            # a span too long to represent is a refusal, not an overflow
            {"q": "x", "since": "9999999d"},
            {"since": "99999999999w"},
        ],
    )
    def test_bad_bound_names_both_forms(
        self, runtime_with_store: PrecisRuntime, call: dict[str, str]
    ) -> None:
        body = runtime_with_store.dispatch("search", {"kind": "job", **call})
        assert "[error:BadInput]" in body, body
        assert "an ISO date/timestamp or a span like 30m, 2h, 3d" in body, body

    def test_sort_recent_is_newest_created_first(
        self, runtime_with_store: PrecisRuntime, store: Store
    ) -> None:
        now = datetime.now(UTC)
        ids = [
            _titled(store, f"release_poll run {n}", "release_poll") for n in range(3)
        ]
        other = _titled(store, "release_deploy run", "release_deploy")
        # Created oldest..newest = ids[0], ids[2], ids[1] — deliberately not
        # ref_id order, so neither the plain listing's order (equal rank,
        # then ref_id descending) nor a last-touched order (ids[0] was
        # touched last) can pass for newest-created-first.
        for age_h, ref_id in zip((30, 10, 20), ids):
            _backdate(store, ref_id, now - timedelta(hours=age_h))
        _touch(store, ids[0], now)
        query = {"kind": "job", "q": "release_poll", "status": "*"}
        unsorted = runtime_with_store.dispatch("search", query)
        assert _listed_ids(unsorted) == [ids[2], ids[1], ids[0]], unsorted
        body = runtime_with_store.dispatch("search", {**query, "sort": "recent"})
        assert _listed_ids(body) == [ids[1], ids[2], ids[0]], body
        assert other not in _listed_ids(body), body
        newest = runtime_with_store.dispatch(
            "search", {**query, "sort": "recent", "page_size": 1}
        )
        assert _listed_ids(newest) == [ids[1]], newest

    def test_sort_recent_composes_with_job_type_and_status(
        self, runtime_with_store: PrecisRuntime, store: Store
    ) -> None:
        now = datetime.now(UTC)
        done = _titled(store, "sweep a", "sweep", "succeeded")
        failed = _titled(store, "sweep b", "sweep", "failed")
        _backdate(store, done, now - timedelta(hours=2))
        _backdate(store, failed, now - timedelta(hours=1))
        body = runtime_with_store.dispatch(
            "search",
            {
                "kind": "job",
                "sort": "recent",
                "status": "succeeded",
                "__extras__": {"job_type": "sweep"},
            },
        )
        assert _listed_ids(body) == [done], body


def _park(
    store: Store,
    title: str,
    job_type: str,
    *,
    wake_in: timedelta,
    why: str | None = None,
    parent_id: int | None = None,
    params: dict[str, Any] | None = None,
    lane_wait: list[str] | None = None,
) -> int:
    payload: dict[str, Any] = {"ts": int((datetime.now(UTC) + wake_in).timestamp())}
    if why is not None:
        payload["why"] = why
    meta: dict[str, Any] = {
        "job_type": job_type,
        "wake_when": {"kind": "at_time", "payload": payload},
    }
    if params is not None:
        meta["params"] = params
    if lane_wait is not None:
        meta["lane_wait"] = lane_wait
    ref = store.insert_ref(
        kind="job", slug=None, title=title, meta=meta, parent_id=parent_id
    )
    store.add_tag(ref.id, Tag.closed("STATUS", "waiting_time"), set_by="system")
    return ref.id


class TestParkedView:
    def test_lists_parks_furthest_wake_first_and_flags_far_ones(
        self, runtime_with_store: PrecisRuntime, store: Store
    ) -> None:
        todo = store.insert_ref(kind="todo", slug=None, title="the todo", meta={})
        near = _park(
            store,
            "run_slice near",
            "run_slice",
            wake_in=timedelta(minutes=40),
            why="lane-wait: run_slice: lane 'devin' is backed off",
            parent_id=todo.id,
            params={"lane": "devin", "target_node": "node-a"},
            lane_wait=["devin", "codex"],
        )
        far = _park(store, "run_slice far", "run_slice", wake_in=timedelta(days=3))
        far2 = _park(store, "quest_tick far", "quest_tick", wake_in=timedelta(days=9))
        running = _titled(store, "run_slice running", "run_slice", "running")
        queued = _titled(store, "run_slice queued", "run_slice", "queued")

        body = runtime_with_store.dispatch(
            "search", {"kind": "job", "view": "parked", "status": "*"}
        )
        assert "[error" not in body, body
        assert _listed_ids(body) == [far2, far, near], body
        assert running not in _listed_ids(body) and queued not in _listed_ids(body)
        assert body.splitlines()[0] == "# 3 parked jobs", body
        assert "2 parked for more than a day" in body, body
        rows = {
            int(m.group(1)): m.group(0) for m in re.finditer(r".*\bjo(\d+)\b.*", body)
        }
        assert rows[far].lstrip().startswith("!"), rows[far]
        assert rows[far2].lstrip().startswith("!"), rows[far2]
        assert not rows[near].lstrip().startswith("!"), rows[near]
        # The near park shows its parent, lanes, node, wake (UTC) and stored
        # reason; a park with no stored reason prints '-', never a guess.
        assert f"td{todo.id}" in rows[near], rows[near]
        assert "lane devin,codex node node-a" in rows[near], rows[near]
        assert "lane-wait: run_slice: lane 'devin' is backed off" in rows[near]
        wake = datetime.now(UTC) + timedelta(minutes=40)
        assert wake.strftime("%Y-%m-%dT%H:") in rows[near], rows[near]
        assert rows[far].rstrip().endswith("-"), rows[far]

    def test_q_job_type_and_page_size_narrow_the_list(
        self, runtime_with_store: PrecisRuntime, store: Store
    ) -> None:
        a = _park(store, "run_slice alpha", "run_slice", wake_in=timedelta(hours=2))
        b = _park(store, "run_slice beta", "run_slice", wake_in=timedelta(hours=1))
        c = _park(store, "quest_tick gamma", "quest_tick", wake_in=timedelta(hours=3))
        by_q = runtime_with_store.dispatch(
            "search", {"kind": "job", "view": "parked", "q": "run_slice"}
        )
        assert _listed_ids(by_q) == [a, b], by_q
        by_type = runtime_with_store.dispatch(
            "search",
            {"kind": "job", "view": "parked", "__extras__": {"job_type": "quest_tick"}},
        )
        assert _listed_ids(by_type) == [c], by_type
        paged = runtime_with_store.dispatch(
            "search", {"kind": "job", "view": "parked", "page_size": 1}
        )
        assert _listed_ids(paged)[:1] == [c], paged
        assert "page=2" in paged, paged

    def test_empty_says_no_parked_jobs(
        self, runtime_with_store: PrecisRuntime, store: Store
    ) -> None:
        _titled(store, "run_slice running", "run_slice", "running")
        body = runtime_with_store.dispatch("search", {"kind": "job", "view": "parked"})
        assert body.startswith("no parked jobs"), body

    def test_unknown_view_lists_parked(self, runtime_with_store: PrecisRuntime) -> None:
        body = runtime_with_store.dispatch(
            "search", {"kind": "job", "q": "run_slice", "view": "bogus"}
        )
        assert "[error:BadInput]" in body, body
        assert "parked" in body, body

    def test_conflicting_status_or_sort_refused(
        self, runtime_with_store: PrecisRuntime
    ) -> None:
        for extra in ({"status": "running"}, {"sort": "recent"}):
            body = runtime_with_store.dispatch(
                "search", {"kind": "job", "view": "parked", **extra}
            )
            assert "[error:BadInput]" in body, body
