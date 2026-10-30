"""``edit(args={'reextract': True})`` + the ``paper_reextract`` job
(gr517423 slice 4): the door validates and enqueues — the job probes,
identity-gates, extracts and swaps the body in one transaction. The
extraction is stubbed through the ``extractor=`` seam — Marker never
runs in tests.
"""

from __future__ import annotations

from typing import Any

import pytest

from precis.dispatch import Hub
from precis.errors import BadInput
from precis.handlers.paper import PaperHandler
from precis.store import ChunkInsert, Store


def _seed(store: Store, slug: str, **kw: Any) -> int:
    return store.insert_ref(
        kind="paper",
        slug=slug,
        title=kw.pop("title", f"Title of {slug}"),
        provider="manual",
        meta=kw.pop("meta", {}),
        **kw,
    ).id


def _body(store: Store, ref_id: int, n: int = 2) -> None:
    vec = [0.01] * store.embedding_dim()
    store.chunks.insert_chunks(
        ref_id,
        [
            ChunkInsert(ord=i, text=f"body block {i} of {ref_id}", embedding=vec)
            for i in range(n)
        ],
    )


def _pdf(
    store: Store,
    ref_id: int,
    sha: str,
    storage_path: str = "x.pdf",
    *,
    held: bool = True,
) -> None:
    """A corpus ``pdfs`` row + pointer + identifier row; ``held`` also
    records a ``pdf_locations`` verdict so ``pdf_held_anywhere`` is true."""
    with store.pool.connection() as conn:
        conn.execute(
            "INSERT INTO pdfs (pdf_sha256, content_hash, page_count, "
            "size_bytes, storage_path) VALUES (%s, %s, 3, 10, %s) "
            "ON CONFLICT DO NOTHING",
            (sha, sha, storage_path),
        )
        conn.execute("UPDATE refs SET pdf_sha256 = %s WHERE ref_id = %s", (sha, ref_id))
        conn.execute(
            "INSERT INTO ref_identifiers (id_kind, id_value, ref_id, source) "
            "VALUES ('pdf_sha256', %s, %s, 'test') ON CONFLICT DO NOTHING",
            (sha, ref_id),
        )
        conn.commit()
    if held:
        store.record_pdf_location(sha, "testnode", storage_path)


def _ords(store: Store, ref_id: int) -> list[int]:
    with store.pool.connection() as conn:
        rows = conn.execute(
            "SELECT ord FROM chunks WHERE ref_id = %s AND ord >= 0 "
            "AND retired_at IS NULL ORDER BY ord",
            (ref_id,),
        ).fetchall()
    return [int(r[0]) for r in rows]


def _body_texts(store: Store, ref_id: int) -> list[str]:
    with store.pool.connection() as conn:
        rows = conn.execute(
            "SELECT text FROM chunks WHERE ref_id = %s AND ord >= 0 "
            "AND retired_at IS NULL ORDER BY ord",
            (ref_id,),
        ).fetchall()
    return [str(r[0]) for r in rows]


def _emb_count(store: Store, ref_id: int) -> int:
    with store.pool.connection() as conn:
        row = conn.execute(
            "SELECT count(*) FROM chunk_embeddings e JOIN chunks c "
            "ON c.chunk_id = e.chunk_id WHERE c.ref_id = %s AND c.ord >= 0",
            (ref_id,),
        ).fetchone()
    return int((row or (0,))[0])


def _meta(store: Store, ref_id: int) -> dict[str, Any]:
    ref = store.fetch_refs_by_ids([ref_id], include_deleted=True)[ref_id]
    return dict(ref.meta or {})


def _events(store: Store, ref_id: int, event: str) -> list[Any]:
    return [e for e in store.events_for(ref_id) if e.event == event]


def _write_pdf(path: Any, *, text: str | None) -> None:
    import fitz

    doc = fitz.open()
    page = doc.new_page()
    if text:
        page.insert_text((72, 72), text)
    doc.save(str(path))
    doc.close()


def _fake_body(n_chunks: int, *, meta: dict[str, Any] | None = None) -> Any:
    from precis.ingest.db_writer import ChunkToWrite
    from precis.ingest.pipeline import BodyExtraction

    chunks = [
        ChunkToWrite(ord=i, chunk_kind="paragraph", text=f"Re-extracted {i}.")
        for i in range(n_chunks)
    ]
    return BodyExtraction(
        chunks=chunks,
        blocks=[],
        meta=dict(meta or {}),
        page_first=1 if chunks else None,
        page_last=1 if chunks else None,
        page_count=1 if chunks else 0,
    )


class TestHealBodilessRef:
    def test_force_reextracts_a_settled_sha(self, store: Store, tmp_path: Any) -> None:
        """A sha already judged once is excluded from the unattended
        candidate scan; ``heal_bodiless_ref`` force re-runs it."""
        from precis.ingest.paper_hygiene import (
            _bodiless_candidates,
            heal_bodiless_ref,
        )

        pdf = tmp_path / "ok.pdf"
        _write_pdf(pdf, text="Title of rex-force — a real body of text.")
        rid = _seed(store, "rex-force")
        sha = f"{rid:064d}"
        _pdf(store, rid, sha, storage_path=str(pdf))

        def fake(path: Any, cand: Any) -> Any:
            return _fake_body(3)

        out = heal_bodiless_ref(store, rid, corpus_dirs=(tmp_path,), extractor=fake)
        assert out is not None and out.outcome == "extracted"
        assert out.chunks_written == 3
        assert _ords(store, rid) == [0, 1, 2]
        # Judged: the unattended pass's scan never sees the sha again.
        assert rid not in {c.ref_id for c in _bodiless_candidates(store, limit=None)}

        # Drop the body back to bodiless (keep the pdf), then the
        # non-forced scan still skips the sha — the verdict event for it
        # exists — but the forced per-ref call re-extracts.
        with store.pool.connection() as conn:
            conn.execute("DELETE FROM chunks WHERE ref_id = %s AND ord >= 0", (rid,))
            conn.commit()
        assert _bodiless_candidates(store, limit=None) == []
        again = heal_bodiless_ref(store, rid, corpus_dirs=(tmp_path,), extractor=fake)
        assert again is not None and again.outcome == "extracted"
        assert _ords(store, rid) == [0, 1, 2]

    def test_missing_file_judged_and_event(self, store: Store, tmp_path: Any) -> None:
        from precis.ingest.paper_hygiene import heal_bodiless_ref

        rid = _seed(store, "rex-miss")
        sha = f"{rid:064d}"
        _pdf(
            store,
            rid,
            sha,
            storage_path=str(tmp_path / "gone.pdf"),
            held=False,
        )
        out = heal_bodiless_ref(store, rid, corpus_dirs=(tmp_path,))
        assert out is not None and out.outcome == "missing_file"
        assert _ords(store, rid) == []
        evs = [e for e in store.events_for(rid) if e.source == "heal:bodiless"]
        assert [e.event for e in evs] == ["missing_file"]

    def test_identity_mismatch_judged_before_extract(
        self, store: Store, tmp_path: Any
    ) -> None:
        """The Opus-ruling gate: a file whose leading pages are another
        paper's text never reaches the extractor (gr517423 finding 1)."""
        from precis.ingest.paper_hygiene import heal_bodiless_ref

        pdf = tmp_path / "wrong.pdf"
        _write_pdf(pdf, text="Completely Unrelated Article About Chemistry")
        rid = _seed(store, "rex-idmiss")
        sha = f"{rid:064d}"
        _pdf(store, rid, sha, storage_path=str(pdf))

        def never(path: Any, cand: Any) -> Any:
            raise AssertionError("extractor must not run on a mismatch")

        out = heal_bodiless_ref(store, rid, corpus_dirs=(tmp_path,), extractor=never)
        assert out is not None and out.outcome == "identity_mismatch"
        assert _ords(store, rid) == []

    def test_not_a_candidate_returns_none(self, store: Store) -> None:
        from precis.ingest.paper_hygiene import heal_bodiless_ref

        rid = _seed(store, "rex-bodied")
        _body(store, rid, 1)
        assert heal_bodiless_ref(store, rid) is None


class TestReextractJob:
    """The job owns the swap: extract + identity-check first, then
    DELETE+INSERT inside one locked transaction — the old body only
    leaves with the new one."""

    def test_swap_replaces_body_keeps_pointer(
        self, store: Store, tmp_path: Any
    ) -> None:
        from precis.workers.job_types import paper_reextract

        pdf = tmp_path / "job.pdf"
        _write_pdf(pdf, text="Title of rex-job — body text.")
        rid = _seed(store, "rex-job")
        _body(store, rid, 2)
        sha = f"{rid:064d}"
        _pdf(store, rid, sha, storage_path=str(pdf))
        rec = paper_reextract.reextract(
            store,
            rid,
            corpus_dirs=(tmp_path,),
            extractor=lambda p, c: _fake_body(3),
        )
        assert rec["status"] == "extracted"
        assert rec["pdf_sha256"] == sha and rec["chunks"] == 3
        # Old body gone, new body in — one DELETE+INSERT swap.
        assert _ords(store, rid) == [0, 1, 2]
        assert _body_texts(store, rid) == [f"Re-extracted {i}." for i in range(3)]
        # The worker's embed pass fills vectors — ingest writes none.
        assert _emb_count(store, rid) == 0
        assert _meta(store, rid)["reextract"]["status"] == "extracted"
        ev = _events(store, rid, "body_replaced")
        assert len(ev) == 1
        assert ev[0].payload["old_chunks"] == 2
        assert ev[0].payload["new_chunks"] == 3
        assert ev[0].payload["pdf_repointed"] is False

    def test_named_sha_repoints_pointer(self, store: Store, tmp_path: Any) -> None:
        """``params.pdf_sha256`` names the file to extract — the pointer
        moves to it inside the swap transaction."""
        from precis.workers.job_types import paper_reextract

        pdf_a = tmp_path / "a.pdf"
        pdf_b = tmp_path / "b.pdf"
        _write_pdf(pdf_a, text="Totally Other Paper's Text Here")
        _write_pdf(pdf_b, text="Title of rex-named — the right file.")
        rid = _seed(store, "rex-named")
        _body(store, rid, 1)
        sha_a = f"{rid:064d}"
        sha_b = "b" * 64
        _pdf(store, rid, sha_a, storage_path=str(pdf_a))
        _pdf(store, rid, sha_b, storage_path=str(pdf_b))
        # _pdf re-points on each call; put the pointer back on A.
        with store.pool.connection() as conn:
            conn.execute(
                "UPDATE refs SET pdf_sha256 = %s WHERE ref_id = %s", (sha_a, rid)
            )
            conn.commit()
        rec = paper_reextract.reextract(
            store,
            rid,
            pdf_sha256=sha_b,
            corpus_dirs=(tmp_path,),
            extractor=lambda p, c: _fake_body(2),
        )
        assert rec["status"] == "extracted"
        assert rec["pdf_repointed"] is True
        assert _pdf_sha(store, rid) == sha_b
        assert _body_texts(store, rid) == ["Re-extracted 0.", "Re-extracted 1."]

    def test_identity_mismatch_body_intact(self, store: Store, tmp_path: Any) -> None:
        """Naming another paper's attached sha fails the identity gate —
        the existing body is untouched (gr517423 finding 1)."""
        from precis.workers.job_types import paper_reextract

        pdf = tmp_path / "other.pdf"
        _write_pdf(pdf, text="An Entirely Different Paper About Nothing")
        rid = _seed(store, "rex-mismatch")
        _body(store, rid, 2)
        sha = "c" * 64
        _pdf(store, rid, sha, storage_path=str(pdf))
        rec = paper_reextract.reextract(
            store,
            rid,
            pdf_sha256=sha,
            corpus_dirs=(tmp_path,),
            extractor=lambda p, c: _fake_body(5),
        )
        assert rec["status"] == "failed"
        assert "identity_mismatch" in rec["reason"]
        assert _ords(store, rid) == [0, 1]
        assert _body_texts(store, rid) == [f"body block {i} of {rid}" for i in (0, 1)]
        assert _meta(store, rid)["reextract"]["status"] == "failed"
        assert len(_events(store, rid, "reextract_failed")) == 1

    def test_missing_file_fails_body_intact(self, store: Store, tmp_path: Any) -> None:
        """A sha the ledger has *swept and found absent everywhere*
        fails missing_file — a never-swept sha defers instead."""
        from precis.workers.job_types import paper_reextract

        rid = _seed(store, "rex-jobmiss")
        _body(store, rid, 1)
        sha = f"{rid:064d}"
        _pdf(
            store,
            rid,
            sha,
            storage_path=str(tmp_path / "gone.pdf"),
            held=False,
        )
        # Swept verdict, absent there: pdf_missing -> terminal failure.
        store.record_pdf_location(sha, "testnode", "")
        rec = paper_reextract.reextract(store, rid, corpus_dirs=(tmp_path,))
        assert rec["status"] == "failed"
        assert "missing_file" in rec["reason"]
        assert _ords(store, rid) == [0]
        assert _events(store, rid, "reextract_failed")[0].payload["pdf_sha256"] == sha

    def test_unswept_sha_is_deferred_not_failed(
        self, store: Store, tmp_path: Any
    ) -> None:
        """A sha with no ledger verdict at all is unknown, not missing —
        deferred until the sweep rules (gr517423 r3 finding D)."""
        from precis.workers.job_types import paper_reextract

        rid = _seed(store, "rex-unswept")
        _body(store, rid, 1)
        sha = f"{rid:064d}"
        _pdf(
            store,
            rid,
            sha,
            storage_path=str(tmp_path / "gone.pdf"),
            held=False,
        )
        rec = paper_reextract.reextract(store, rid, corpus_dirs=(tmp_path,))
        assert rec["status"] == "deferred"
        assert "no pdf_locations verdict" in rec["reason"]
        assert rec["holders"] == []
        assert _ords(store, rid) == [0]
        assert _events(store, rid, "reextract_failed") == []

    def test_held_elsewhere_is_deferred_not_failed(
        self, store: Store, tmp_path: Any
    ) -> None:
        """No local copy but a node holds it: deferred = retry, and the
        body is untouched (gr517423 finding 2)."""
        from precis.workers.job_types import paper_reextract

        rid = _seed(store, "rex-elsewhere")
        _body(store, rid, 1)
        sha = f"{rid:064d}"
        _pdf(store, rid, sha, storage_path="/nonexistent-mount/gone.pdf")
        rec = paper_reextract.reextract(store, rid, corpus_dirs=(tmp_path,))
        assert rec["status"] == "deferred"
        assert rec["holders"] == ["testnode"]
        assert "testnode" in rec["reason"]
        assert _ords(store, rid) == [0]
        assert _events(store, rid, "reextract_failed") == []

    def test_extractor_raise_fails_body_intact(
        self, store: Store, tmp_path: Any
    ) -> None:
        from precis.workers.job_types import paper_reextract

        pdf = tmp_path / "boom.pdf"
        _write_pdf(pdf, text="Title of rex-raise — body text.")
        rid = _seed(store, "rex-raise")
        _body(store, rid, 2)
        sha = f"{rid:064d}"
        _pdf(store, rid, sha, storage_path=str(pdf))

        def boom(path: Any, cand: Any) -> Any:
            raise RuntimeError("marker died")

        rec = paper_reextract.reextract(
            store, rid, corpus_dirs=(tmp_path,), extractor=boom
        )
        assert rec["status"] == "failed"
        assert "extraction raised" in rec["reason"]
        assert _ords(store, rid) == [0, 1]

    def test_detached_sha_is_skipped(self, store: Store, tmp_path: Any) -> None:
        """``params.pdf_sha256`` honors the queued sha — a file detached
        since enqueue is a skip, never an extraction of whatever the
        pointer now names (gr517423 finding 3)."""
        from precis.workers.job_types import paper_reextract

        pdf = tmp_path / "x.pdf"
        _write_pdf(pdf, text="Title of rex-detach — body text.")
        rid = _seed(store, "rex-detach")
        _body(store, rid, 1)
        sha = f"{rid:064d}"
        _pdf(store, rid, sha, storage_path=str(pdf))
        rec = paper_reextract.reextract(
            store,
            rid,
            pdf_sha256="d" * 64,
            corpus_dirs=(tmp_path,),
            extractor=lambda p, c: _fake_body(2),
        )
        assert rec["status"] == "skipped"
        assert "no longer attached" in rec["reason"]
        assert _ords(store, rid) == [0]
        assert _pdf_sha(store, rid) == sha

    def test_bodiless_ref_gets_a_body(self, store: Store, tmp_path: Any) -> None:
        from precis.workers.job_types import paper_reextract

        pdf = tmp_path / "b.pdf"
        _write_pdf(pdf, text="Title of rex-empty — body text.")
        rid = _seed(store, "rex-empty")
        sha = f"{rid:064d}"
        _pdf(store, rid, sha, storage_path=str(pdf))
        rec = paper_reextract.reextract(
            store,
            rid,
            corpus_dirs=(tmp_path,),
            extractor=lambda p, c: _fake_body(2),
        )
        assert rec["status"] == "extracted"
        assert _ords(store, rid) == [0, 1]

    def test_extracted_body_is_a_draft_promotion_opportunity(
        self, store: Store, tmp_path: Any
    ) -> None:
        """A swapped-in body can satisfy staged findings waiting on this
        paper's quote — the swap runs the same promotion ingest does."""
        from unittest.mock import patch

        from precis.workers.job_types import paper_reextract

        pdf = tmp_path / "promote.pdf"
        _write_pdf(pdf, text="Title of rex-promote — body text.")
        rid = _seed(store, "rex-promote")
        sha = f"{rid:064d}"
        _pdf(store, rid, sha, storage_path=str(pdf))
        with patch(
            "precis.finding_drafts.promote_drafts_for_paper", return_value=0
        ) as promote:
            rec = paper_reextract.reextract(
                store,
                rid,
                corpus_dirs=(tmp_path,),
                extractor=lambda p, c: _fake_body(2),
            )
        assert rec["status"] == "extracted"
        promote.assert_called_once_with(store, rid)

    def test_chunk_links_removed_counted(self, store: Store, tmp_path: Any) -> None:
        """Links anchored on the outgoing chunks die with the DELETE —
        the count lands on the record and the body_replaced event
        (gr517423 r3 finding C)."""
        from precis.workers.job_types import paper_reextract

        pdf = tmp_path / "links.pdf"
        _write_pdf(pdf, text="Title of rex-links — body text.")
        rid = _seed(store, "rex-links")
        other = _seed(store, "rex-links-other")
        _body(store, rid, 2)
        store.add_link(
            src_ref_id=rid, dst_ref_id=other, relation="related-to", src_pos=0
        )
        sha = f"{rid:064d}"
        _pdf(store, rid, sha, storage_path=str(pdf))
        rec = paper_reextract.reextract(
            store,
            rid,
            corpus_dirs=(tmp_path,),
            extractor=lambda p, c: _fake_body(2),
        )
        assert rec["status"] == "extracted"
        assert rec["chunk_links_removed"] == 1
        ev = _events(store, rid, "body_replaced")
        assert ev[0].payload["chunk_links_removed"] == 1
        # The anchored link really is gone; the new body stands.
        assert _ords(store, rid) == [0, 1]
        assert _meta(store, rid)["reextract"]["chunk_links_removed"] == 1

    def test_content_hash_identifier_swapped(self, store: Store, tmp_path: Any) -> None:
        """The old extraction's content_hash leaves with the body; the
        new one lands in the same tx (gr517423 r3 finding E)."""
        from precis.workers.job_types import paper_reextract

        pdf = tmp_path / "hash.pdf"
        _write_pdf(pdf, text="Title of rex-hash — body text.")
        rid = _seed(store, "rex-hash")
        _body(store, rid, 1)
        sha = f"{rid:064d}"
        _pdf(store, rid, sha, storage_path=str(pdf))
        with store.pool.connection() as conn:
            conn.execute(
                "INSERT INTO ref_identifiers (id_kind, id_value, ref_id, "
                "source) VALUES ('content_hash', 'stale-old-hash', %s, 'test')",
                (rid,),
            )
            conn.commit()
        rec = paper_reextract.reextract(
            store,
            rid,
            corpus_dirs=(tmp_path,),
            extractor=lambda p, c: _fake_body(2),
        )
        assert rec["status"] == "extracted"
        with store.pool.connection() as conn:
            rows = conn.execute(
                "SELECT id_value FROM ref_identifiers WHERE ref_id = %s "
                "AND id_kind = 'content_hash'",
                (rid,),
            ).fetchall()
        values = {str(r[0]) for r in rows}
        assert "stale-old-hash" not in values
        assert len(values) == 1

    def test_dispatch_failed_records_failure(self, store: Store, tmp_path: Any) -> None:
        """A failed run ends the job failed (gr517423 finding 5) — a
        quiet 'done' reads as success to whoever watches the job."""
        from precis.workers.executors._context import DispatchContext
        from precis.workers.job_types import paper_reextract

        rid = _seed(store, "rex-dispatch")
        _body(store, rid, 1)
        sha = f"{rid:064d}"
        _pdf(store, rid, sha, storage_path=str(tmp_path / "gone.pdf"), held=False)
        store.record_pdf_location(sha, "testnode", "")
        calls: dict[str, Any] = {"chunks": [], "failed": [], "meta": {}}
        ctx = DispatchContext(
            store=store,
            ref_id=999999,
            title="job",
            meta={"params": {"ref_id": rid, "pdf_sha256": sha}},
            set_status=lambda v: None,
            append_chunk=lambda k, t: calls["chunks"].append((k, t)),
            set_meta=lambda **kw: calls["meta"].update(kw),
            record_failure=lambda msg, **kw: calls["failed"].append(msg),
            is_cancel_requested=lambda: False,
        )
        paper_reextract._dispatch(ctx, paper_reextract.SPEC)
        assert len(calls["failed"]) == 1
        assert "missing_file" in calls["failed"][0]
        assert calls["meta"] == {}

    def test_dispatch_deferred_parks(self, store: Store, tmp_path: Any) -> None:
        from precis.workers.executors._context import DispatchContext
        from precis.workers.job_types import paper_reextract

        rid = _seed(store, "rex-defer")
        _body(store, rid, 1)
        sha = f"{rid:064d}"
        _pdf(store, rid, sha, storage_path="/nonexistent-mount/gone.pdf")
        calls: dict[str, Any] = {"deferred": [], "failed": [], "meta": {}}
        ctx = DispatchContext(
            store=store,
            ref_id=999999,
            title="job",
            meta={"params": {"ref_id": rid, "pdf_sha256": sha}},
            set_status=lambda v: None,
            append_chunk=lambda k, t: None,
            set_meta=lambda **kw: calls["meta"].update(kw),
            record_failure=lambda msg, **kw: calls["failed"].append(msg),
            is_cancel_requested=lambda: False,
            defer=lambda wake, note: calls["deferred"].append(note) or True,
        )
        paper_reextract._dispatch(ctx, paper_reextract.SPEC)
        assert len(calls["deferred"]) == 1
        assert calls["failed"] == []
        # Placement parks reset the shared pause counter but count under
        # their own key (gr517423 r3 findings A+B).
        assert calls["meta"]["pause_parks"] == 0
        assert calls["meta"]["placement_parks"] == 1

    def test_dispatch_deferred_past_placement_cap_fails(
        self, store: Store, tmp_path: Any
    ) -> None:
        """A held-but-never-claimed file can't park forever: past
        ``_PLACEMENT_PARK_CAP`` the job fails naming the holders
        (gr517423 r3 review finding A)."""
        from precis.workers.executors._context import DispatchContext
        from precis.workers.job_types import paper_reextract

        rid = _seed(store, "rex-cap")
        _body(store, rid, 1)
        sha = f"{rid:064d}"
        _pdf(store, rid, sha, storage_path="/nonexistent-mount/gone.pdf")
        calls: dict[str, Any] = {"deferred": [], "failed": [], "meta": {}}
        ctx = DispatchContext(
            store=store,
            ref_id=999999,
            title="job",
            meta={
                "params": {"ref_id": rid, "pdf_sha256": sha},
                "placement_parks": paper_reextract._PLACEMENT_PARK_CAP,
            },
            set_status=lambda v: None,
            append_chunk=lambda k, t: None,
            set_meta=lambda **kw: calls["meta"].update(kw),
            record_failure=lambda msg, **kw: calls["failed"].append(msg),
            is_cancel_requested=lambda: False,
            defer=lambda wake, note: calls["deferred"].append(note) or True,
        )
        paper_reextract._dispatch(ctx, paper_reextract.SPEC)
        assert calls["deferred"] == []
        assert len(calls["failed"]) == 1
        assert "placement retry cap" in calls["failed"][0]
        assert "testnode" in calls["failed"][0]
        assert "next=" in calls["failed"][0]
        assert _meta(store, rid)["reextract"]["status"] == "failed"
        assert len(_events(store, rid, "reextract_failed")) == 1

    def test_idem_key_shape(self) -> None:
        from precis.workers.job_types import paper_reextract

        assert paper_reextract.idem_key(42, "ab" * 32) == ("reextract:42:" + "ab" * 32)

    def test_job_type_registered(self) -> None:
        from precis.workers.job_types import get_job_type, known_job_types

        spec = get_job_type("paper_reextract")
        assert spec is not None
        assert spec.compatible_executors == frozenset({"job_inproc"})
        assert "paper_reextract" in known_job_types()


def _pdf_sha(store: Store, ref_id: int) -> str | None:
    with store.pool.connection() as conn:
        row = conn.execute(
            "SELECT pdf_sha256 FROM refs WHERE ref_id = %s", (ref_id,)
        ).fetchone()
    return str(row[0]) if row and row[0] else None


def _pdf_id_values(store: Store, ref_id: int) -> list[str]:
    with store.pool.connection() as conn:
        rows = conn.execute(
            "SELECT id_value FROM ref_identifiers WHERE ref_id = %s "
            "AND id_kind = 'pdf_sha256' ORDER BY id_value",
            (ref_id,),
        ).fetchall()
    return [str(r[0]).strip() for r in rows]


def _reextract_jobs(store: Store) -> list[dict[str, Any]]:
    with store.pool.connection() as conn:
        rows = conn.execute(
            "SELECT ref_id, parent_id, meta->'params', meta->>'idem_key' "
            "FROM refs WHERE kind = 'job' "
            "AND meta->>'job_type' = 'paper_reextract'",
        ).fetchall()
    return [
        {"id": r[0], "parent_id": r[1], "params": r[2], "idem_key": r[3]} for r in rows
    ]


class TestReextractDoor:
    """``edit(args={'reextract': True})``: validates (live ref, reason,
    an attached pdf some node holds) and enqueues — never touches the
    body; the swap lives in the job (Opus ruling, gr517423)."""

    @pytest.fixture
    def handler(self, store: Store) -> PaperHandler:
        # Registered the way boot does it: the job handler's parent
        # guard asks the hub which kinds opted into ``can_own_jobs``.
        hub = Hub(store=store)
        handler = PaperHandler(hub=hub)
        handler.hub = hub
        hub.register_handler("paper", handler)
        hub.register_ability("paper", "edit", None, lambda **kw: None)
        return handler

    def test_happy_path_body_untouched(
        self, store: Store, handler: PaperHandler
    ) -> None:
        rid = _seed(store, "rex-door", meta={"source_format": "pdf"})
        _body(store, rid, 3)
        sha = f"{rid:064d}"
        _pdf(store, rid, sha)
        resp = handler.edit(
            id=rid, args={"reextract": True, "reason": "bad extraction"}
        )
        assert "queued" in resp.body
        # The door writes nothing: body, pointer and identifiers all stay.
        assert _ords(store, rid) == [0, 1, 2]
        assert _emb_count(store, rid) == 3
        assert _pdf_sha(store, rid) == sha
        assert "source_format" in _meta(store, rid)
        jobs = _reextract_jobs(store)
        assert len(jobs) == 1
        job = jobs[0]
        assert job["parent_id"] == rid
        assert job["params"] == {
            "ref_id": rid,
            "pdf_sha256": sha,
            "reason": "bad extraction",
        }
        assert job["idem_key"] == f"reextract:{rid}:{sha}"
        assert f"get(kind='job', id={job['id']})" in resp.body
        # No drop — so no drop event either.
        assert _events(store, rid, "body_dropped") == []

    def test_dry_run_writes_nothing(self, store: Store, handler: PaperHandler) -> None:
        rid = _seed(store, "rex-dry")
        other = _seed(store, "rex-dry-other")
        _body(store, rid, 2)
        store.add_link(
            src_ref_id=rid, dst_ref_id=other, relation="related-to", src_pos=0
        )
        sha = f"{rid:064d}"
        _pdf(store, rid, sha)
        resp = handler.edit(
            id=rid,
            args={"reextract": True, "reason": "x"},
            dry_run=True,
        )
        assert "DRY RUN" in resp.body
        assert sha in resp.body
        assert f"reextract:{rid}:{sha}" in resp.body
        assert "swap" in resp.body
        assert "1 chunk link(s) removed" in resp.body
        assert "undo:" in resp.body
        assert _ords(store, rid) == [0, 1]
        assert _reextract_jobs(store) == []

    def test_no_pdf_refused(self, store: Store, handler: PaperHandler) -> None:
        rid = _seed(store, "rex-nopdf")
        _body(store, rid, 1)
        with pytest.raises(BadInput, match="no pdf") as exc:
            handler.edit(id=rid, args={"reextract": True, "reason": "x"})
        assert "pdf_url" in str(exc.value.next)
        assert _ords(store, rid) == [0]
        assert _reextract_jobs(store) == []

    def test_unheld_pdf_refused(self, store: Store, handler: PaperHandler) -> None:
        """The door-side presence gate: a sha the ledger has swept and
        found absent everywhere is refused before a job is queued
        (gr517423 finding 2's cheap half; r3 finding D moves it to
        ``pdf_missing``)."""
        rid = _seed(store, "rex-unheld")
        _body(store, rid, 1)
        sha = f"{rid:064d}"
        _pdf(store, rid, sha, held=False)
        store.record_pdf_location(sha, "testnode", "")
        with pytest.raises(BadInput, match="held on no node") as exc:
            handler.edit(id=rid, args={"reextract": True, "reason": "x"})
        assert "pdf_url" in str(exc.value.next)
        assert _reextract_jobs(store) == []

    def test_unswept_pdf_queues(self, store: Store, handler: PaperHandler) -> None:
        """A sha with no ledger verdict at all is unknown, not missing —
        the door lets it through; the job defers or fails on the sweep's
        verdict (gr517423 r3 finding D)."""
        rid = _seed(store, "rex-unsweptdoor")
        _body(store, rid, 1)
        _pdf(store, rid, f"{rid:064d}", held=False)
        resp = handler.edit(id=rid, args={"reextract": True, "reason": "x"})
        assert "queued" in resp.body
        assert len(_reextract_jobs(store)) == 1

    def test_bare_stub_refused(self, store: Store, handler: PaperHandler) -> None:
        rid = _seed(store, "rex-bare")
        with pytest.raises(BadInput, match="no pdf") as exc:
            handler.edit(id=rid, args={"reextract": True, "reason": "x"})
        assert "pdf_url" in str(exc.value.next)

    def test_retired_refused(self, store: Store, handler: PaperHandler) -> None:
        rid = _seed(store, "rex-dead")
        _body(store, rid, 1)
        _pdf(store, rid, f"{rid:064d}")
        store.retire_ref(rid)
        with pytest.raises(BadInput, match="retired") as exc:
            handler.edit(id=rid, args={"reextract": True, "reason": "x"})
        assert "retire" in str(exc.value.next)
        assert _ords(store, rid) == [0]
        assert _reextract_jobs(store) == []

    def test_missing_reason_refused(self, store: Store, handler: PaperHandler) -> None:
        rid = _seed(store, "rex-noreason")
        _body(store, rid, 1)
        _pdf(store, rid, f"{rid:064d}")
        with pytest.raises(BadInput, match="reason") as exc:
            handler.edit(id=rid, args={"reextract": True})
        assert exc.value.next is not None
        assert _ords(store, rid) == [0]
        assert _reextract_jobs(store) == []

    def test_second_call_dedups_the_job(
        self, store: Store, handler: PaperHandler
    ) -> None:
        """The idem_key collapses a repeat call onto the still-queued
        job — and with no door-side drop there is no window that could
        strand the ref (gr517423 finding 4)."""
        rid = _seed(store, "rex-twice")
        _body(store, rid, 1)
        sha = f"{rid:064d}"
        _pdf(store, rid, sha)
        first = handler.edit(id=rid, args={"reextract": True, "reason": "x"})
        assert "queued" in first.body and "already" not in first.body
        second = handler.edit(id=rid, args={"reextract": True, "reason": "x"})
        assert "already queued" in second.body
        assert len(_reextract_jobs(store)) == 1
        assert _ords(store, rid) == [0]

    def test_bodiless_with_pdf_still_queues(
        self, store: Store, handler: PaperHandler
    ) -> None:
        """A ref already bodiless but holding its pdf is the heal's
        shape — the job writes the body it extracts."""
        rid = _seed(store, "rex-bodiless")
        sha = f"{rid:064d}"
        _pdf(store, rid, sha)
        resp = handler.edit(id=rid, args={"reextract": True, "reason": "x"})
        assert "queued" in resp.body
        assert len(_reextract_jobs(store)) == 1
        assert _pdf_sha(store, rid) == sha

    def test_named_sha_ack_mentions_pending_switch(
        self, store: Store, handler: PaperHandler
    ) -> None:
        """Naming a non-pointer sha queues a job that will re-point —
        the ack says so up front (gr517423 finding 6)."""
        rid = _seed(store, "rex-named2")
        _body(store, rid, 1)
        sha_a = f"{rid:064d}"
        sha_b = "b" * 64
        _pdf(store, rid, sha_a)
        _pdf(store, rid, sha_b, storage_path="b.pdf")
        with store.pool.connection() as conn:
            conn.execute(
                "UPDATE refs SET pdf_sha256 = %s WHERE ref_id = %s",
                (sha_a, rid),
            )
            conn.commit()
        resp = handler.edit(
            id=rid,
            args={
                "reextract": True,
                "pdf_sha256": sha_b,
                "reason": "pointer was the wrong file",
            },
        )
        assert "re-point" in resp.body
        assert sha_b in resp.body
        # Nothing switched yet — the swap is the job's.
        assert _pdf_sha(store, rid) == sha_a
        jobs = _reextract_jobs(store)
        assert jobs[0]["params"]["pdf_sha256"] == sha_b
        assert jobs[0]["idem_key"] == f"reextract:{rid}:{sha_b}"

    def test_several_attached_unnamed_refused(
        self, store: Store, handler: PaperHandler
    ) -> None:
        rid = _seed(store, "rex-amb")
        _body(store, rid, 1)
        _pdf(store, rid, f"{rid:064d}")
        _pdf(store, rid, "b" * 64, storage_path="b.pdf")
        with pytest.raises(BadInput, match="pdf_sha256") as exc:
            handler.edit(id=rid, args={"reextract": True, "reason": "x"})
        assert exc.value.next is not None
        assert _ords(store, rid) == [0]
        assert _reextract_jobs(store) == []

    def test_pdf_sha256_off_doors_refused(
        self, store: Store, handler: PaperHandler
    ) -> None:
        """``pdf_sha256`` modifies drop_body/reextract only — a bare
        edit carrying it must refuse, not drop silently."""
        rid = _seed(store, "rex-modgate")
        _body(store, rid, 1)
        with pytest.raises(BadInput, match="pdf_sha256"):
            handler.edit(
                id=rid,
                args={"pdf_sha256": "a" * 64, "reason": "x"},
            )
        with pytest.raises(BadInput, match="pdf_sha256"):
            handler.edit(
                id=rid,
                args={
                    "clear_abstract": True,
                    "pdf_sha256": "a" * 64,
                    "reason": "x",
                },
            )
        assert _ords(store, rid) == [0]

    def test_keep_pdf_off_drop_body_still_refused(
        self, store: Store, handler: PaperHandler
    ) -> None:
        """The gate split keeps keep_pdf drop_body-only — it must not
        ride reextract."""
        rid = _seed(store, "rex-keepgate")
        _body(store, rid, 1)
        _pdf(store, rid, f"{rid:064d}")
        with pytest.raises(BadInput, match="keep_pdf"):
            handler.edit(
                id=rid,
                args={"reextract": True, "keep_pdf": True, "reason": "x"},
            )
        assert _ords(store, rid) == [0]
