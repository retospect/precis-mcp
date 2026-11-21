"""gr551840 — ``get(kind='paper', id='<unheld DOI>')`` queues a
``paper_ingest`` job instead of raising NotFound (Reto ruling
2026-11-02: "a DOI someone requests should be pursued, even if it is
not already ingested").

Covered here: the queue ack + idempotent reuse (the 30×-a-day probe
loop that filed the gripe), the recent-failure requeue gate, the
untouched non-DOI/malformed/held paths, and the job's own dispatch
(mint-or-collapse the stub, pin it for ``fetch_oa``) — the get itself
does no network work. The review pass adds the spend/refusal bounds:
the fleet rolling-24h mint cap, the 5-per-batch cap, the read-only and
retired doors, and the real-dispatch ``finished_at`` stamp the requeue
window reads. gr715493 adds the view door: a get naming a view is a
read, so an unheld DOI answers the NotFound (no mint) — plain and
batch, including aliases for the default renderer.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from psycopg.types.json import Jsonb

from precis.dispatch import Hub
from precis.embedder import MockEmbedder
from precis.errors import BadInput, NotFound
from precis.handlers.paper import PaperHandler
from precis.runtime import PrecisRuntime
from precis.store import ChunkInsert, Store
from precis.store.types import Tag
from precis.workers._mint import mint_background_job
from precis.workers.executors.job_inproc import run_job_inproc_pass
from precis.workers.job_types import get_job_type, paper_ingest
from tests.extract_helpers import fake_ctx

DOI = "10.1021/ja01019a011"


def _seed_paper(
    store: Store, *, slug: str = "held2020paper", doi: str = "10.1111/jnc.13915"
) -> int:
    e = MockEmbedder(dim=1024)
    ref = store.insert_ref(
        kind="paper",
        slug=slug,
        title="A held paper",
        provider="manual",
        authors=[{"name": "Held, P."}],
        year=2020,
        meta={"doi": doi, "journal": "JNC", "abstract": "An existing abstract."},
    )
    store.insert_ref_identifiers(ref.id, [("doi", doi, "manual")])
    store.chunks.insert_chunks(
        ref.id,
        [ChunkInsert(ord=0, text="Introduction.", embedding=e.embed_one("Intro."))],
    )
    return ref.id


@pytest.fixture
def handler(hub: Hub) -> PaperHandler:
    return PaperHandler(hub=hub)


def _jobs(store: Store) -> list[dict[str, Any]]:
    with store.pool.connection() as conn:
        rows = conn.execute(
            "SELECT ref_id, title, meta FROM refs WHERE kind = 'job' ORDER BY ref_id"
        ).fetchall()
    return [{"id": int(r[0]), "title": r[1], "meta": dict(r[2] or {})} for r in rows]


def _stub_for_doi(store: Store, doi: str) -> dict[str, Any] | None:
    with store.pool.connection() as conn:
        row = conn.execute(
            "SELECT r.ref_id, r.title, r.meta, r.prio FROM refs r "
            "JOIN ref_identifiers i ON i.ref_id = r.ref_id "
            "WHERE i.id_kind = 'doi' AND i.id_value = %s "
            "AND r.kind = 'paper' AND r.retired_at IS NULL",
            (doi,),
        ).fetchone()
    if row is None:
        return None
    return {
        "id": int(row[0]),
        "title": row[1],
        "meta": dict(row[2] or {}),
        "prio": row[3],
    }


def _mint_terminal_job(
    store: Store,
    doi: str,
    *,
    status: str = "failed",
    finished: datetime | None = None,
    error: str | None = None,
) -> int:
    """A terminal ``paper_ingest`` job under the DOI idem key, as a real
    dispatch's end state leaves it (``add_tag`` stamps ``finished_at``;
    the test then rewinds/forwards the stamp to land the window case)."""
    with store.tx() as conn:
        ref = mint_background_job(
            store,
            conn=conn,
            job_type=paper_ingest.JOB_TYPE,
            executor="job_inproc",
            title=f"ingest DOI {doi}",
            params={"doi": doi},
            idem_key=paper_ingest.idem_key(doi),
            status="running",
        )
        store.add_tag(
            ref.id,
            Tag.closed("STATUS", status),
            set_by="system",
            replace_prefix=True,
            conn=conn,
        )
        patch: dict[str, Any] = {}
        if finished is not None:
            patch["finished_at"] = finished.isoformat()
        if error is not None:
            patch["error"] = error
        if patch:
            conn.execute(
                "UPDATE refs SET meta = meta || %s::jsonb WHERE ref_id = %s",
                (Jsonb(patch), ref.id),
            )
        return int(ref.id)


# ---------------------------------------------------------------------------
# The get door
# ---------------------------------------------------------------------------


def test_doi_status_reports_availability_without_queuing(
    store: Store, handler: PaperHandler
) -> None:
    held_doi = "10.1111/jnc.13915"
    ref_id = _seed_paper(store, doi=held_doi)

    held = handler.get(id=f"https://doi.org/{held_doi}", view="status")
    missing = handler.get(id=DOI, view="status")

    assert f"https://doi.org/{held_doi} -> pa{ref_id}" in held.body
    assert "full-text" in held.body
    assert f"{DOI}  unresolved" in missing.body
    assert not _jobs(store)
    assert _stub_for_doi(store, DOI) is None


class TestUnheldDoiGetQueues:
    def test_held_abstract_still_reads(
        self, store: Store, handler: PaperHandler
    ) -> None:
        _seed_paper(store, doi=DOI)
        assert "An existing abstract." in handler.get(id=DOI, view="abstract").body
        assert _jobs(store) == []

    def test_abstract_miss_does_not_queue(
        self, store: Store, handler: PaperHandler
    ) -> None:
        """gr717456: inspecting an abstract must not launch acquisition."""
        with pytest.raises(NotFound):
            handler.get(id=DOI, view="abstract")
        assert _jobs(store) == []
        assert _stub_for_doi(store, DOI) is None

    @pytest.mark.parametrize("confirm_bulk", [False, True])
    def test_batch_abstract_misses_do_not_queue(
        self, store: Store, handler: PaperHandler, confirm_bulk: bool
    ) -> None:
        ids: list[str | int] = [f"10.1234/abstract-miss-{i}" for i in range(7)]
        resp = handler.get(id=ids, view="abstract", args={"confirm_bulk": confirm_bulk})
        assert resp.body.count("not found") == len(ids)
        assert "confirm_bulk" not in resp.body
        assert _jobs(store) == []

    def test_queues_job_and_names_it(self, store: Store, handler: PaperHandler) -> None:
        resp = handler.get(id=DOI)
        jobs = _jobs(store)
        assert len(jobs) == 1
        meta = jobs[0]["meta"]
        assert meta["job_type"] == "paper_ingest"
        assert meta["executor"] == "job_inproc"
        assert meta["params"] == {"doi": DOI}
        assert meta["idem_key"] == paper_ingest.idem_key(DOI)
        assert meta["origin"] == "background"
        assert f"not ingested yet; queued ingest job jo{jobs[0]['id']}" in resp.body
        assert f"for {DOI}" in resp.body
        assert "retry get in a few minutes" in resp.body
        assert f"get(kind='job', id={jobs[0]['id']})" in resp.body

    def test_repeat_get_reuses_the_job(
        self, store: Store, handler: PaperHandler
    ) -> None:
        first = handler.get(id=DOI)
        second = handler.get(id=DOI)
        jobs = _jobs(store)
        assert len(jobs) == 1
        assert "already queued/running" in second.body
        assert f"jo{jobs[0]['id']}" in second.body

    def test_doi_spellings_dedupe_to_one_job(
        self, store: Store, handler: PaperHandler
    ) -> None:
        for raw in (
            DOI,
            f"doi:{DOI}",
            f"DOI:{DOI.upper()}",
            f"https://doi.org/{DOI}",
            f"doi.org/{DOI.upper()}",
        ):
            resp = handler.get(id=raw)
            assert "queued ingest job" in resp.body or "already queued" in resp.body
        assert len(_jobs(store)) == 1

    def test_find_in_ref_on_unheld_doi_queues(
        self, store: Store, handler: PaperHandler
    ) -> None:
        resp = handler.get(id=DOI, q="band gap")
        assert len(_jobs(store)) == 1
        assert "queued ingest job" in resp.body

    def test_batch_element_queues(self, store: Store, handler: PaperHandler) -> None:
        resp = handler.get(id=[DOI, "no-such-slug"])
        assert len(_jobs(store)) == 1
        assert "queued ingest job" in resp.body
        assert "no-such-slug  not found" in resp.body

    def test_get_does_no_network(
        self, store: Store, handler: PaperHandler, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The DOI resolvers are the job dispatch's affair — the get path
        must never call them inline (the ack only mints the row)."""
        monkeypatch.setattr(
            paper_ingest,
            "_s2_lookup",
            lambda doi: pytest.fail("get ran the S2 resolver inline"),
        )
        monkeypatch.setattr(
            paper_ingest,
            "_registry_lookup",
            lambda doi: pytest.fail("get ran the DOI registries inline"),
        )
        resp = handler.get(id=DOI)
        assert "queued ingest job" in resp.body


class TestUnheldDoiGate:
    def test_recently_failed_job_blocks_requeue(
        self, store: Store, handler: PaperHandler
    ) -> None:
        job_id = _mint_terminal_job(
            store,
            DOI,
            finished=datetime.now(UTC) - timedelta(hours=1),
            error="did not resolve via Semantic Scholar or the DOI registries",
        )
        resp = handler.get(id=DOI)
        assert len(_jobs(store)) == 1
        assert f"jo{job_id}" in resp.body
        assert "not requeueing" in resp.body
        assert "did not resolve" in resp.body
        assert f"get(kind='job', id={job_id})" in resp.body

    def test_stale_failure_requeues(self, store: Store, handler: PaperHandler) -> None:
        _mint_terminal_job(
            store,
            DOI,
            finished=datetime.now(UTC)
            - timedelta(hours=paper_ingest.REQUEUE_WINDOW_HOURS + 1),
        )
        resp = handler.get(id=DOI)
        jobs = _jobs(store)
        assert len(jobs) == 2
        assert "queued ingest job" in resp.body
        assert "retry get in a few minutes" in resp.body

    def test_cancelled_job_inside_window_blocks(
        self, store: Store, handler: PaperHandler
    ) -> None:
        _mint_terminal_job(store, DOI, status="cancelled", finished=datetime.now(UTC))
        resp = handler.get(id=DOI)
        assert len(_jobs(store)) == 1
        assert "STATUS:cancelled" in resp.body
        assert "not requeueing" in resp.body


class TestViewGetDoesNotQueue:
    """gr715493: a get that names a view is a read of stored data — for
    an unheld DOI there is nothing to render, so the miss answers the
    NotFound with the acquire hint ("does not queue") and no job row
    lands. The gr551840 queue door opens only for a view-less get."""

    def test_bibtex_view_miss_does_not_queue(
        self, store: Store, handler: PaperHandler
    ) -> None:
        with pytest.raises(NotFound, match="not ingested") as excinfo:
            handler.get(id=DOI, view="bibtex")
        nxt = excinfo.value.next
        assert isinstance(nxt, str)
        assert "does not queue" in nxt
        assert f"put(kind='paper', doi='{DOI}')" in nxt
        assert _jobs(store) == []

    def test_view_path_alias_does_not_queue(
        self, store: Store, handler: PaperHandler
    ) -> None:
        with pytest.raises(NotFound, match="not ingested"):
            handler.get(id=DOI, view="cite/bib")
        assert _jobs(store) == []

    @pytest.mark.parametrize("view", ["meta", "text", "body", "full"])
    def test_default_renderer_alias_does_not_queue(
        self, store: Store, handler: PaperHandler, view: str
    ) -> None:
        with pytest.raises(NotFound, match="not ingested") as excinfo:
            handler.get(id=DOI, view=view)
        assert "does not queue" in (excinfo.value.next or "")
        assert _jobs(store) == []

    def test_find_in_ref_with_view_does_not_queue(
        self, store: Store, handler: PaperHandler
    ) -> None:
        """``q=`` plus a view is refused by the find-in-ref preflight
        before any DOI resolution, so no job can be minted."""
        with pytest.raises(BadInput, match="doesn't combine with view"):
            handler.get(id=DOI, view="bibtex", q="x")
        assert _jobs(store) == []

    def test_view_get_on_retired_doi_shows_tombstone(
        self, store: Store, handler: PaperHandler
    ) -> None:
        """The tombstone answer is a read too — a view get on a retired
        DOI still gets it, without queueing anything."""
        ref_id = _seed_paper(store, doi=DOI)
        store.retire_ref(ref_id)
        resp = handler.get(id=DOI, view="bibtex")
        assert "deleted paper" in resp.body
        assert f"pa{ref_id}" in resp.body
        assert _jobs(store) == []

    @pytest.mark.parametrize("view", ["bibtex", "meta", "text", "body", "full"])
    def test_batch_with_view_does_not_queue_or_ask_confirm(
        self, store: Store, handler: PaperHandler, view: str
    ) -> None:
        """A view batch is a read: no confirm_bulk pre-scan demand and
        no per-element mints — every unheld DOI reads as not-found."""
        dois: list[str | int] = [f"10.1021/viewcap{i}" for i in range(7)]
        resp = handler.get(id=dois, view=view)
        assert "confirm_bulk" not in resp.body
        assert resp.body.count("not found") == 7
        assert _jobs(store) == []

    def test_batch_without_view_still_queues(
        self, store: Store, handler: PaperHandler
    ) -> None:
        resp = handler.get(id=[DOI])
        assert "queued ingest job" in resp.body
        assert len(_jobs(store)) == 1


class TestUnaffectedGetPaths:
    def test_held_doi_renders_unchanged(
        self, store: Store, handler: PaperHandler
    ) -> None:
        _seed_paper(store)
        resp = handler.get(id="10.1111/jnc.13915")
        assert "A held paper" in resp.body
        assert _jobs(store) == []

    def test_malformed_doi_still_errors(
        self, store: Store, handler: PaperHandler
    ) -> None:
        for bad in ("doi:not-a-doi", "10.1/", "DOI:"):
            with pytest.raises((NotFound, BadInput)):
                handler.get(id=bad)
        assert _jobs(store) == []

    def test_unheld_arxiv_still_errors(
        self, store: Store, handler: PaperHandler
    ) -> None:
        with pytest.raises(NotFound):
            handler.get(id="2401.99999")
        assert _jobs(store) == []

    def test_plain_slug_miss_still_errors(
        self, store: Store, handler: PaperHandler
    ) -> None:
        with pytest.raises(NotFound):
            handler.get(id="no-such-paper-slug")
        assert _jobs(store) == []


# ---------------------------------------------------------------------------
# The job itself
# ---------------------------------------------------------------------------


class TestRegistration:
    def test_registry_resolves_spec(self) -> None:
        spec = get_job_type("paper_ingest")
        assert spec is paper_ingest.SPEC
        assert spec.dispatch is paper_ingest._dispatch
        assert spec.compatible_executors == frozenset({"job_inproc"})

    def test_validate_rejects_a_non_doi(self, store: Store) -> None:
        with pytest.raises(BadInput):
            paper_ingest.validate(store, {"doi": "not-a-doi"})
        paper_ingest.validate(store, {"doi": DOI})


def _dispatch(ctx: Any) -> Any:
    dispatch = paper_ingest.SPEC.dispatch
    assert dispatch is not None
    return dispatch(ctx, paper_ingest.SPEC)


class TestDispatch:
    def test_s2_hit_mints_pinned_stub(
        self, store: Store, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            paper_ingest,
            "_s2_lookup",
            lambda doi: {
                "title": "The Requested Paper",
                "year": 2014,
                "doi": DOI,
                "authors": [{"name": "Author, A."}],
                "s2_id": "abc123",
            },
        )
        out: list = []
        ctx = fake_ctx(store, {"job_type": "paper_ingest", "params": {"doi": DOI}}, out)
        _dispatch(ctx)

        stub = _stub_for_doi(store, DOI)
        assert stub is not None
        assert stub["title"] == "The Requested Paper"
        assert stub["prio"] == 1
        assert stub["meta"].get("prio_by") == "acquire"
        assert stub["meta"].get("oa_requeued")
        assert any(
            ev[0] == "meta" and ev[1].get("paper_ref_id") == stub["id"] for ev in out
        )
        assert not any(ev[0] == "fail" for ev in out)

    def test_registry_fallback_verifies(
        self, store: Store, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(paper_ingest, "_s2_lookup", lambda doi: None)
        monkeypatch.setattr(
            paper_ingest,
            "_registry_lookup",
            lambda doi: {
                "title": "Crossref-Verified Paper",
                "year": 2019,
                "doi": DOI,
                "source": "crossref",
                "journal": "JACS",
                "authors": [{"family": "Reg", "given": "I."}],
            },
        )
        out: list = []
        ctx = fake_ctx(store, {"job_type": "paper_ingest", "params": {"doi": DOI}}, out)
        _dispatch(ctx)

        stub = _stub_for_doi(store, DOI)
        assert stub is not None
        assert stub["title"] == "Crossref-Verified Paper"
        assert stub["meta"].get("acquire_registry") == "crossref"
        assert stub["meta"].get("journal") == "JACS"
        assert stub["prio"] == 1
        assert not any(ev[0] == "fail" for ev in out)

    def test_genuine_miss_fails_the_job(
        self, store: Store, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A DOI that resolves nowhere fails the job — the terminal
        record is what the get-side gate reports; no stub litters the
        backlog."""
        monkeypatch.setattr(paper_ingest, "_s2_lookup", lambda doi: None)
        monkeypatch.setattr(paper_ingest, "_registry_lookup", lambda doi: None)
        out: list = []
        ctx = fake_ctx(store, {"job_type": "paper_ingest", "params": {"doi": DOI}}, out)
        _dispatch(ctx)

        assert _stub_for_doi(store, DOI) is None
        fails = [ev for ev in out if ev[0] == "fail"]
        assert fails and "did not resolve" in fails[0][1]["reason"]

    def test_resolver_fault_mints_unverified(
        self, store: Store, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A resolver outage is not evidence the DOI is wrong — mint the
        stub marked unverified (mirrors acquire's gr483930 rule)."""
        monkeypatch.setattr(
            paper_ingest,
            "_s2_lookup",
            lambda doi: (_ for _ in ()).throw(RuntimeError("s2 down")),
        )
        monkeypatch.setattr(
            paper_ingest,
            "_registry_lookup",
            lambda doi: (_ for _ in ()).throw(RuntimeError("crossref down")),
        )
        out: list = []
        ctx = fake_ctx(store, {"job_type": "paper_ingest", "params": {"doi": DOI}}, out)
        _dispatch(ctx)

        stub = _stub_for_doi(store, DOI)
        assert stub is not None
        assert stub["meta"].get("acquire_unverified") is True
        assert stub["prio"] == 1
        assert not any(ev[0] == "fail" for ev in out)

    def test_s2_echo_of_poisoned_cluster_uses_registry_record(
        self, store: Store, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """gr710828, mirrored from acquire: S2 echoes the DOI back onto a
        mis-merged cluster — the stub must not adopt the cluster's arXiv /
        S2 aliases, and the registry record that established the mismatch
        is reused (the registries are asked exactly once, so a second-ask
        miss cannot fail a job the first ask confirmed)."""
        foreign = "10.9728/dcs.2024.25.7.1847"
        calls: list[str] = []
        monkeypatch.setattr(
            paper_ingest,
            "_s2_lookup",
            lambda doi: {
                "title": "A Unified Approach to Interpreting Model Predictions",
                "year": 2017,
                "doi": doi,
                "arxiv_id": "1705.07874",
                "s2_id": "442e10a3c6640ded9408622005e3c2a8906ce4c2",
                "authors": [{"name": "Scott M. Lundberg"}],
            },
        )

        def _registry(doi: str) -> dict[str, Any]:
            calls.append(doi)
            return {
                "title": "Exploration of Digital Divide Factors Affecting "
                "Life Satisfaction in Older People: Using Tree-Based "
                "Ensemble Machine Learning and SHAP",
                "authors": [{"given": "Jeong-Seon", "family": "Lee"}],
                "year": 2024,
                "source": "crossref",
            }

        monkeypatch.setattr(paper_ingest, "_registry_lookup", _registry)
        out: list = []
        ctx = fake_ctx(
            store, {"job_type": "paper_ingest", "params": {"doi": foreign}}, out
        )
        _dispatch(ctx)

        stub = _stub_for_doi(store, foreign)
        assert stub is not None
        assert stub["meta"].get("acquire_registry") == "crossref"
        assert stub["title"] == (
            "Exploration of Digital Divide Factors Affecting Life "
            "Satisfaction in Older People: Using Tree-Based Ensemble "
            "Machine Learning and SHAP"
        )
        assert calls == [foreign]
        with store.pool.connection() as conn:
            rows = conn.execute(
                "SELECT id_kind FROM ref_identifiers WHERE ref_id = %s",
                (stub["id"],),
            ).fetchall()
        kinds = [r[0] for r in rows]
        assert "doi" in kinds
        assert not any(k in ("arxiv", "s2") for k in kinds)
        assert not any(ev[0] == "fail" for ev in out)

    def test_empty_doi_fails(
        self, store: Store, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        out: list = []
        ctx = fake_ctx(
            store, {"job_type": "paper_ingest", "params": {"doi": "  "}}, out
        )
        _dispatch(ctx)
        assert any(ev[0] == "fail" for ev in out)


class TestEndToEnd:
    def test_stub_in_place_means_next_get_resolves(
        self, store: Store, handler: PaperHandler
    ) -> None:
        """The loop the gripe wanted: get → queue → (dispatch ran) → get
        finds the held paper through ``ref_identifiers``."""
        handler.get(id=DOI)
        ref_id = _seed_paper(store, slug="ingested2026", doi=DOI)
        resp = handler.get(id=DOI)
        assert f"pa{ref_id}" in resp.body
        assert "queued ingest job" not in resp.body


# ---------------------------------------------------------------------------
# The bounds on the mint (gr551840 review)
# ---------------------------------------------------------------------------


class TestGetQueueBounds:
    """The review's spend/refusal bounds: the idem key only dedupes ONE
    DOI's retries, so distinct-DOI mints needed a fleet daily cap and a
    per-batch cap; the read-only and retired doors refuse before the
    mint; and the requeue window reads the stamp a REAL dispatch leaves
    (``meta.finished_at``, falling back to the row's own timestamps)."""

    def test_daily_cap_blocks_past_limit(
        self, store: Store, handler: PaperHandler, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``PRECIS_GET_DOI_QUEUE_DAILY`` gates get-minted jobs (the
        ``discovered_via='get'`` marker) — past it the get answers the
        plain not-ingested text with the explicit-put hint, and no
        fourth job row exists."""
        monkeypatch.setenv(paper_ingest.GET_DOI_QUEUE_DAILY_ENV, "3")
        dois = [f"10.1021/capbound{i}" for i in range(4)]
        for doi in dois[:3]:
            assert "queued ingest job" in handler.get(id=doi).body
        resp = handler.get(id=dois[3])
        assert "daily get-queue cap reached" in resp.body
        assert f"put(kind='paper', doi='{dois[3]}')" in resp.body
        jobs = _jobs(store)
        assert len(jobs) == 3
        assert all(j["meta"].get("discovered_via") == "get" for j in jobs)

    def test_capped_answer_names_the_explicit_resubmit(
        self, store: Store, handler: PaperHandler, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """gr740575: the capped ack must leave a working queue path —
        ``put`` by DOI was itself broken in prod (gr740076), so the
        answer names the same-call resubmit too."""
        monkeypatch.setenv(paper_ingest.GET_DOI_QUEUE_DAILY_ENV, "1")
        handler.get(id="10.1021/capmsg0")
        resp = handler.get(id="10.1021/capmsg1")
        assert "daily get-queue cap reached" in resp.body
        assert "args={'queue': 'explicit'}" in resp.body
        assert "put(kind='paper', doi='10.1021/capmsg1')" in resp.body

    def test_explicit_resubmit_queues_past_the_daily_cap(
        self, store: Store, handler: PaperHandler, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """gr740575: ``args={'queue': 'explicit'}`` is the deliberate
        lane the review left unbounded — it mints past the spent cap,
        stamped ``discovered_via='get-explicit'`` so it never counts
        toward the implicit lane's bound."""
        monkeypatch.setenv(paper_ingest.GET_DOI_QUEUE_DAILY_ENV, "4")
        dois = [f"10.1021/expcap{i}" for i in range(5)]
        for doi in dois[:3]:
            assert "queued ingest job" in handler.get(id=doi).body
        resp = handler.get(id=dois[3], args={"queue": "explicit"})
        assert "queued ingest job" in resp.body
        jobs = _jobs(store)
        assert len(jobs) == 4
        assert jobs[-1]["meta"].get("discovered_via") == "get-explicit"
        # The explicit mint did not count toward the 'get' cap — the
        # fourth implicit mint still fits under cap=4 (only then does
        # the fifth read as capped).
        assert "queued ingest job" in handler.get(id=dois[4]).body
        resp = handler.get(id="10.1021/expcap5")
        assert "daily get-queue cap reached" in resp.body

    def test_explicit_batch_resubmit_queues_past_daily_cap(
        self, store: Store, handler: PaperHandler, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(paper_ingest.GET_DOI_QUEUE_DAILY_ENV, "1")
        handler.get(id="10.1021/expbatch0")
        dois: list[str | int] = [f"10.1021/expbatch{i}" for i in range(1, 3)]
        resp = handler.get(id=dois, args={"queue": "explicit"})
        assert resp.body.count("queued ingest job") == 2
        assert len(_jobs(store)) == 3

    def test_explicit_resubmit_via_find_in_ref(
        self, store: Store, handler: PaperHandler, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(paper_ingest.GET_DOI_QUEUE_DAILY_ENV, "1")
        handler.get(id="10.1021/expfind0")
        resp = handler.get(id=DOI, q="band gap", args={"queue": "explicit"})
        assert "queued ingest job" in resp.body
        assert len(_jobs(store)) == 2

    def test_explicit_does_not_override_a_recent_failure(
        self, store: Store, handler: PaperHandler
    ) -> None:
        """Explicit lifts the spend bound only — a DOI whose last
        attempt failed inside the window still reports that attempt."""
        _mint_terminal_job(store, DOI, finished=datetime.now(UTC))
        resp = handler.get(id=DOI, args={"queue": "explicit"})
        assert len(_jobs(store)) == 1
        assert "not requeueing" in resp.body

    def test_explicit_is_not_a_read_door(
        self, store: Store, handler: PaperHandler
    ) -> None:
        """A view get stays a read even with the flag — ``queue`` names
        intent, it does not turn a read into a write."""
        with pytest.raises(NotFound, match="not ingested"):
            handler.get(id=DOI, view="bibtex", args={"queue": "explicit"})
        assert _jobs(store) == []

    def test_batch_over_cap_asks_for_confirmation(
        self, store: Store, handler: PaperHandler
    ) -> None:
        """gr575784/gr575843: a batch of unheld DOIs over
        ``_BATCH_DOI_QUEUE_CAP`` (5) asks for ``confirm_bulk`` up front
        instead of silently minting a partial batch and capping the
        rest one "cap reached" message at a time — nothing queues yet."""
        dois: list[str | int] = [f"10.1021/batchcap{i}" for i in range(7)]
        resp = handler.get(id=dois)
        assert "7 of 7 ids are unheld DOIs" in resp.body
        assert "args={'confirm_bulk': True}" in resp.body
        assert _jobs(store) == []

    def test_batch_over_cap_confirm_bulk_queues_all(
        self, store: Store, handler: PaperHandler
    ) -> None:
        """Resubmitting with ``confirm_bulk`` queues every element in
        the one call, past the per-call cap."""
        dois: list[str | int] = [f"10.1021/batchcap{i}" for i in range(7)]
        resp = handler.get(id=dois, args={"confirm_bulk": True})
        assert resp.body.count("queued ingest job") == 7
        assert "cap reached" not in resp.body
        assert len(_jobs(store)) == 7

    def test_batch_at_cap_queues_without_confirmation(
        self, store: Store, handler: PaperHandler
    ) -> None:
        """At or under the cap, the batch still queues in one call —
        no confirmation needed."""
        dois: list[str | int] = [f"10.1021/batchcap{i}" for i in range(5)]
        resp = handler.get(id=dois)
        assert resp.body.count("queued ingest job") == 5
        assert len(_jobs(store)) == 5

    def test_read_only_scope_gives_old_notfound_no_insert(
        self, store: Store, handler: PaperHandler, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``get`` sits in ``READ_ONLY_VERBS`` — under a read scope
        (read-only serve / read-only DB role) the unheld-DOI door
        must not mint: the call keeps the pre-queue NotFound with the
        acquire hint ("does not queue"), and no job row lands."""
        monkeypatch.setattr("precis.runtime.scope.current_scope", lambda: "read")
        with pytest.raises(NotFound, match="not ingested") as excinfo:
            handler.get(id=DOI)
        nxt = excinfo.value.next
        assert isinstance(nxt, str)
        assert "does not queue" in nxt
        assert "put(kind='paper'" in nxt
        assert _jobs(store) == []

    def test_retired_doi_does_not_queue(
        self, store: Store, handler: PaperHandler
    ) -> None:
        """A DOI whose only carrier is a soft-deleted paper answers the
        tombstone ack — ``retire_ref`` leaves the ``ref_identifiers``
        row, the live-only resolve misses, and re-ingesting a deleted
        paper is not the get's call."""
        ref_id = _seed_paper(store, doi=DOI)
        store.retire_ref(ref_id)
        resp = handler.get(id=DOI)
        assert "deleted paper" in resp.body
        assert "not queueing" in resp.body
        assert f"pa{ref_id}" in resp.body
        assert _jobs(store) == []

    def test_real_dispatch_failure_stamps_finished_at_blocks_requeue(
        self, store: Store, handler: PaperHandler, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A genuine miss everywhere fails the real job run —
        ``record_failure`` stamps ``meta.finished_at``/``error`` via the
        STATUS write — and a get inside the window reports that attempt
        instead of minting a second job."""
        monkeypatch.setattr(paper_ingest, "_s2_lookup", lambda doi: None)
        monkeypatch.setattr(paper_ingest, "_registry_lookup", lambda doi: None)
        handler.get(id=DOI)
        assert run_job_inproc_pass(store, limit=1)["claimed"] == 1
        jobs = _jobs(store)
        assert len(jobs) == 1
        meta = jobs[0]["meta"]
        assert meta.get("finished_at")
        assert "did not resolve" in (meta.get("error") or "")
        resp = handler.get(id=DOI)
        assert len(_jobs(store)) == 1
        assert "STATUS:failed" in resp.body
        assert "not requeueing" in resp.body
        assert f"jo{jobs[0]['id']}" in resp.body


class TestExplicitResubmitThroughDispatch:
    """gr740575: the capped ack's advertised resubmit must survive the
    dispatch layer too — the direct-handler tests above bypass both
    gates the flag had to pass: the ``get`` ``ARGS_KEYS`` extras gate
    (``queue`` was undeclared) and the find-in-ref route's own extras
    whitelist (``context`` only)."""

    def test_plain_get_dispatch_accepts_queue_explicit(
        self,
        runtime_with_store: PrecisRuntime,
        store: Store,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv(paper_ingest.GET_DOI_QUEUE_DAILY_ENV, "1")
        runtime_with_store.dispatch("get", {"kind": "paper", "id": "10.1021/disp0"})
        out = runtime_with_store.dispatch(
            "get",
            {
                "kind": "paper",
                "id": "10.1021/disp1",
                "__extras__": {"queue": "explicit"},
            },
        )
        assert "[error" not in out, out
        assert "queued ingest job" in out, out
        jobs = _jobs(store)
        assert len(jobs) == 2
        assert jobs[-1]["meta"].get("discovered_via") == "get-explicit"

    def test_find_in_ref_dispatch_accepts_queue_explicit(
        self,
        runtime_with_store: PrecisRuntime,
        store: Store,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """``get(kind='paper', id=<doi>, q=…, args={'queue': …})`` is
        routed to ``find_in_ref`` before ``get`` runs — its extras
        whitelist must let the flag through to the one kind whose
        find declares it."""
        monkeypatch.setenv(paper_ingest.GET_DOI_QUEUE_DAILY_ENV, "1")
        runtime_with_store.dispatch("get", {"kind": "paper", "id": "10.1021/dfind0"})
        out = runtime_with_store.dispatch(
            "get",
            {
                "kind": "paper",
                "id": "10.1021/dfind1",
                "q": "band gap",
                "__extras__": {"queue": "explicit"},
            },
        )
        assert "[error" not in out, out
        assert "queued ingest job" in out, out
        assert len(_jobs(store)) == 2

    def test_find_in_ref_batch_cap_guides_retry(
        self,
        runtime_with_store: PrecisRuntime,
        store: Store,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv(paper_ingest.GET_DOI_QUEUE_DAILY_ENV, "1")
        dois = [f"10.1021/dfindbatch{i}" for i in range(7)]
        request = {
            "kind": "paper",
            "id": dois,
            "q": "band gap",
            "__extras__": {"queue": "explicit"},
        }
        out = runtime_with_store.dispatch("get", request)
        assert out.count("queued ingest job") == 5
        assert out.count("retry capped IDs in batches of at most 5") == 2
        assert "put(kind='paper'" not in out
        assert len(_jobs(store)) == 5

        out = runtime_with_store.dispatch("get", {**request, "id": dois[5:]})
        assert out.count("queued ingest job") == 2
        assert len(_jobs(store)) == 7

    def test_find_in_ref_queue_refused_on_a_kind_without_the_door(
        self, runtime_with_store: PrecisRuntime
    ) -> None:
        """The extras key is per-kind: a ``find_in_ref`` that never
        declared ``queue`` refuses it instead of silently dropping it."""
        out = runtime_with_store.dispatch(
            "get",
            {
                "kind": "skill",
                "id": "precis-paper-help",
                "q": "ingest",
                "__extras__": {"queue": "explicit"},
            },
        )
        assert "[error:BadInput]" in out, out
        assert "not accepted by skill find-in-ref" in out, out
