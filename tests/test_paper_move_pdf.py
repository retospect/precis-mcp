"""``edit(args={'move_pdf_to': …})`` / ``{'attach_pdf': …}`` and the
``pdf_attach`` job (gr517423 slice 5): re-home a stored PDF between refs.

The doors validate — donor bodiless, target live+bodiless+pdf-less,
caller reason — and the identity gate (the file's leading pages against
the *target's* title/DOI) runs from the donor's stored page-1-3 chunk
text when it exists, else defers to the ``pdf_attach`` job which reads
the file on a corpus host. On a ``doi``/``title`` verdict the move
lands in one transaction (pointer + identifier row + ``pdf_moved``
events on both refs) and a ``paper_reextract`` is queued. The job's
``reader``/``enqueue`` kwargs are the test seams — PyMuPDF and the job
mint never run in tests.
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
) -> None:
    """A corpus ``pdfs`` row + the ref's pointer + identifier row."""
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


def _orphan_pdf(store: Store, sha: str, storage_path: str = "orphan.pdf") -> None:
    """A ``pdfs`` row no ref owns — the orphan ``attach_pdf`` adopts."""
    with store.pool.connection() as conn:
        conn.execute(
            "INSERT INTO pdfs (pdf_sha256, content_hash, page_count, "
            "size_bytes, storage_path) VALUES (%s, %s, 3, 10, %s) "
            "ON CONFLICT DO NOTHING",
            (sha, sha, storage_path),
        )
        conn.commit()


def _retired_pages(store: Store, ref_id: int, texts: list[str]) -> None:
    """Retired page-1..N body chunks — the identity evidence a
    re-ingested donor keeps as history for ``leading_texts``."""
    store.chunks.insert_chunks(
        ref_id,
        [ChunkInsert(ord=i, text=t) for i, t in enumerate(texts)],
    )
    with store.pool.connection() as conn:
        conn.execute(
            "UPDATE chunks SET retired_at = now(), page_first = ord + 1 "
            "WHERE ref_id = %s AND ord >= 0",
            (ref_id,),
        )
        conn.commit()


def _ords(store: Store, ref_id: int) -> list[int]:
    with store.pool.connection() as conn:
        rows = conn.execute(
            "SELECT ord FROM chunks WHERE ref_id = %s AND ord >= 0 "
            "AND retired_at IS NULL ORDER BY ord",
            (ref_id,),
        ).fetchall()
    return [int(r[0]) for r in rows]


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


def _meta(store: Store, ref_id: int) -> dict[str, Any]:
    ref = store.fetch_refs_by_ids([ref_id], include_deleted=True)[ref_id]
    return dict(ref.meta or {})


def _events(store: Store, ref_id: int, event: str) -> list[Any]:
    return [e for e in store.events_for(ref_id) if e.event == event]


def _jobs(store: Store, job_type: str) -> list[dict[str, Any]]:
    with store.pool.connection() as conn:
        rows = conn.execute(
            "SELECT ref_id, parent_id, meta->'params', meta->>'idem_key' "
            "FROM refs WHERE kind = 'job' AND meta->>'job_type' = %s",
            (job_type,),
        ).fetchall()
    return [
        {"id": r[0], "parent_id": r[1], "params": r[2], "idem_key": r[3]} for r in rows
    ]


@pytest.fixture
def handler(store: Store) -> PaperHandler:
    # Registered the way boot does it: the job handler's parent guard
    # asks the hub which kinds opted into ``can_own_jobs``.
    hub = Hub(store=store)
    handler = PaperHandler(hub=hub)
    handler.hub = hub
    hub.register_handler("paper", handler)
    hub.register_ability("paper", "edit", None, lambda **kw: None)
    return handler


class TestMovePdfDoor:
    """``edit(args={'move_pdf_to': <target>})`` — re-home a stored pdf
    from a bodiless donor to a bodiless target."""

    def test_happy_move_doi_verdict(self, store: Store, handler: PaperHandler) -> None:
        """The donor's retired page-1 chunks carry the target's DOI —
        the door judges inline and writes the move itself."""
        donor = _seed(store, "mv-donor")
        sha = "a" * 64
        _pdf(store, donor, sha)
        _retired_pages(store, donor, ["doi:10.9/mvtarget — front page text"])
        tgt = _seed(store, "mv-target")
        store.set_ref_identifier(tgt, "doi", "10.9/mvtarget", source="test")
        resp = handler.edit(
            id=donor,
            args={"move_pdf_to": f"pa{tgt}", "reason": "file parked on wrong ref"},
        )
        assert "moved pdf" in resp.body
        assert sha in resp.body
        # Pointer + identifier row moved; donor cleared.
        assert _pdf_sha(store, donor) is None
        assert _pdf_sha(store, tgt) == sha
        assert _pdf_id_values(store, donor) == []
        assert _pdf_id_values(store, tgt) == [sha]
        # pdf_moved journals on both refs.
        ev_t = _events(store, tgt, "pdf_moved")
        ev_d = _events(store, donor, "pdf_moved")
        assert len(ev_t) == 1 and len(ev_d) == 1
        assert ev_t[0].payload["verdict"] == "doi"
        assert ev_t[0].payload["from"] == donor
        assert ev_t[0].payload["to"] == tgt
        # The target's body comes from the moved file.
        jobs = _jobs(store, "paper_reextract")
        assert len(jobs) == 1
        assert jobs[0]["parent_id"] == tgt
        assert jobs[0]["params"] == {
            "ref_id": tgt,
            "pdf_sha256": sha,
            "reason": "file parked on wrong ref",
        }
        assert jobs[0]["idem_key"] == f"reextract:{tgt}:{sha}"

    def test_happy_move_title_verdict(
        self, store: Store, handler: PaperHandler
    ) -> None:
        """No DOI: >= 80% of the target's significant title words in the
        stored pages is a 'title' verdict — same write."""
        donor = _seed(store, "mvt-donor")
        sha = "b" * 64
        _pdf(store, donor, sha)
        _retired_pages(
            store, donor, ["Quantum widgets lattice arrays — the first page"]
        )
        tgt = _seed(store, "mvt-target", title="Quantum Widgets in Lattice Arrays")
        resp = handler.edit(
            id=donor,
            args={"move_pdf_to": f"pa{tgt}", "reason": "x"},
        )
        assert "verdict title" in resp.body
        assert _pdf_sha(store, tgt) == sha
        assert _events(store, tgt, "pdf_moved")[0].payload["verdict"] == "title"

    def test_title_mismatch_refuses_nothing_written(
        self, store: Store, handler: PaperHandler
    ) -> None:
        donor = _seed(store, "mm-donor")
        sha = "c" * 64
        _pdf(store, donor, sha)
        _retired_pages(
            store, donor, ["completely unrelated chemistry proceedings text"]
        )
        tgt = _seed(store, "mm-target", title="Quantum Widgets in Lattice Arrays")
        with pytest.raises(BadInput, match="do not look like") as exc:
            handler.edit(
                id=donor,
                args={"move_pdf_to": f"pa{tgt}", "reason": "x"},
            )
        assert exc.value.next is not None
        assert _pdf_sha(store, donor) == sha
        assert _pdf_sha(store, tgt) is None
        assert _pdf_id_values(store, tgt) == []
        assert _events(store, tgt, "pdf_moved") == []
        assert _jobs(store, "paper_reextract") == []
        assert _jobs(store, "pdf_attach") == []

    def test_donor_with_body_refuses_toward_move_body_to(
        self, store: Store, handler: PaperHandler
    ) -> None:
        donor = _seed(store, "mb-donor")
        _body(store, donor, 2)
        _pdf(store, donor, "d" * 64)
        tgt = _seed(store, "mb-target")
        with pytest.raises(BadInput, match="body and pdf move together") as exc:
            handler.edit(
                id=donor,
                args={"move_pdf_to": f"pa{tgt}", "reason": "x"},
            )
        assert "move_body_to" in str(exc.value.next)
        assert _pdf_sha(store, tgt) is None
        assert _jobs(store, "pdf_attach") == []

    def test_no_chunks_defers_the_gate_to_the_job(
        self, store: Store, handler: PaperHandler
    ) -> None:
        """A keep_pdf drop leaves no page-1-3 chunks — the verdict moves
        to the pdf_attach job, which reads the file on a corpus host."""
        donor = _seed(store, "def-donor")
        sha = "e" * 64
        _pdf(store, donor, sha)
        tgt = _seed(store, "def-target")
        resp = handler.edit(
            id=donor,
            args={"move_pdf_to": f"pa{tgt}", "reason": "orphan after drop"},
        )
        assert "pdf_attach job" in resp.body
        jobs = _jobs(store, "pdf_attach")
        assert len(jobs) == 1
        assert jobs[0]["parent_id"] == tgt
        assert jobs[0]["params"] == {
            "target_ref_id": tgt,
            "donor_ref_id": donor,
            "pdf_sha256": sha,
            "reason": "orphan after drop",
        }
        assert jobs[0]["idem_key"] == f"pdf_attach:{tgt}:{sha}"
        # Nothing moved — the job owns the write.
        assert _pdf_sha(store, donor) == sha
        assert _pdf_sha(store, tgt) is None

    def test_dry_run_writes_nothing(self, store: Store, handler: PaperHandler) -> None:
        donor = _seed(store, "dry-donor")
        sha = "f" * 64
        _pdf(store, donor, sha)
        _retired_pages(store, donor, ["doi:10.9/drytgt front page"])
        tgt = _seed(store, "dry-target")
        store.set_ref_identifier(tgt, "doi", "10.9/drytgt", source="test")
        resp = handler.edit(
            id=donor,
            args={"move_pdf_to": f"pa{tgt}", "reason": "x"},
            dry_run=True,
        )
        assert "DRY RUN" in resp.body
        assert sha in resp.body
        assert "undo:" in resp.body
        assert _pdf_sha(store, donor) == sha
        assert _pdf_sha(store, tgt) is None
        assert _events(store, tgt, "pdf_moved") == []
        assert _events(store, donor, "pdf_moved") == []
        assert _jobs(store, "paper_reextract") == []
        assert _jobs(store, "pdf_attach") == []

    def test_dry_run_says_deferred_without_chunks(
        self, store: Store, handler: PaperHandler
    ) -> None:
        donor = _seed(store, "dryd-donor")
        _pdf(store, donor, "1" * 64)
        tgt = _seed(store, "dryd-target")
        resp = handler.edit(
            id=donor,
            args={"move_pdf_to": f"pa{tgt}", "reason": "x"},
            dry_run=True,
        )
        assert "DEFERRED" in resp.body
        assert _jobs(store, "pdf_attach") == []

    def test_target_with_body_refuses(
        self, store: Store, handler: PaperHandler
    ) -> None:
        donor = _seed(store, "tb-donor")
        _pdf(store, donor, "2" * 64)
        tgt = _seed(store, "tb-target")
        _body(store, tgt, 1)
        with pytest.raises(BadInput, match="already holds .* body") as exc:
            handler.edit(
                id=donor,
                args={"move_pdf_to": f"pa{tgt}", "reason": "x"},
            )
        assert "drop_body" in str(exc.value.next)
        assert _pdf_sha(store, tgt) is None

    def test_target_with_pdf_refuses(self, store: Store, handler: PaperHandler) -> None:
        donor = _seed(store, "tp-donor")
        _pdf(store, donor, "3" * 64)
        tgt = _seed(store, "tp-target")
        _pdf(store, tgt, "4" * 64)
        with pytest.raises(BadInput, match="already holds a pdf") as exc:
            handler.edit(
                id=donor,
                args={"move_pdf_to": f"pa{tgt}", "reason": "x"},
            )
        assert "reextract" in str(exc.value.next)
        assert _pdf_sha(store, donor) == "3" * 64

    def test_donor_without_pdf_is_a_no_change(
        self, store: Store, handler: PaperHandler
    ) -> None:
        donor = _seed(store, "np-donor")
        tgt = _seed(store, "np-target")
        resp = handler.edit(
            id=donor,
            args={"move_pdf_to": f"pa{tgt}", "reason": "x"},
        )
        assert "no change" in resp.body
        assert _jobs(store, "pdf_attach") == []

    def test_self_move_refused(self, store: Store, handler: PaperHandler) -> None:
        donor = _seed(store, "self-donor")
        _pdf(store, donor, "5" * 64)
        with pytest.raises(BadInput, match="record itself"):
            handler.edit(
                id=donor,
                args={"move_pdf_to": f"pa{donor}", "reason": "x"},
            )

    def test_retired_target_refused(self, store: Store, handler: PaperHandler) -> None:
        donor = _seed(store, "rt-donor")
        _pdf(store, donor, "6" * 64)
        tgt = _seed(store, "rt-target")
        store.retire_ref(tgt)
        with pytest.raises(BadInput, match="retired"):
            handler.edit(
                id=donor,
                args={"move_pdf_to": f"pa{tgt}", "reason": "x"},
            )
        assert _pdf_sha(store, donor) == "6" * 64

    def test_named_sha_not_attached_refuses(
        self, store: Store, handler: PaperHandler
    ) -> None:
        donor = _seed(store, "nm-donor")
        _pdf(store, donor, "7" * 64)
        tgt = _seed(store, "nm-target")
        with pytest.raises(BadInput, match="not attached"):
            handler.edit(
                id=donor,
                args={
                    "move_pdf_to": f"pa{tgt}",
                    "pdf_sha256": "8" * 64,
                    "reason": "x",
                },
            )

    def test_missing_reason_refused(self, store: Store, handler: PaperHandler) -> None:
        donor = _seed(store, "nr-donor")
        _pdf(store, donor, "9" * 64)
        tgt = _seed(store, "nr-target")
        with pytest.raises(BadInput, match="reason") as exc:
            handler.edit(id=donor, args={"move_pdf_to": f"pa{tgt}"})
        assert exc.value.next is not None


class TestAttachPdfDoor:
    """``edit(args={'attach_pdf': '<sha>'})`` — adopt an orphan ``pdfs``
    row; the identity gate always defers to the job."""

    def test_orphan_sha_queues_the_job(
        self, store: Store, handler: PaperHandler
    ) -> None:
        tgt = _seed(store, "at-target")
        sha = "ab" * 32
        _orphan_pdf(store, sha)
        resp = handler.edit(
            id=tgt, args={"attach_pdf": sha, "reason": "adopt the orphan"}
        )
        assert "pdf_attach job" in resp.body
        jobs = _jobs(store, "pdf_attach")
        assert len(jobs) == 1
        assert jobs[0]["parent_id"] == tgt
        assert jobs[0]["params"] == {
            "target_ref_id": tgt,
            "donor_ref_id": None,
            "pdf_sha256": sha,
            "reason": "adopt the orphan",
        }
        assert jobs[0]["idem_key"] == f"pdf_attach:{tgt}:{sha}"
        # The door writes nothing — the job applies the attach.
        assert _pdf_sha(store, tgt) is None

    def test_owned_sha_refuses_naming_owner(
        self, store: Store, handler: PaperHandler
    ) -> None:
        owner = _seed(store, "at-owner")
        sha = "ac" * 32
        _pdf(store, owner, sha)
        tgt = _seed(store, "at-tgt2")
        with pytest.raises(BadInput, match="already belongs") as exc:
            handler.edit(id=tgt, args={"attach_pdf": sha, "reason": "x"})
        assert f"pa{owner}" in str(exc.value)
        assert "move_pdf_to" in str(exc.value.next)
        assert _jobs(store, "pdf_attach") == []

    def test_no_pdfs_row_refuses(self, store: Store, handler: PaperHandler) -> None:
        tgt = _seed(store, "at-norow")
        with pytest.raises(BadInput, match="no pdfs row") as exc:
            handler.edit(id=tgt, args={"attach_pdf": "ad" * 32, "reason": "x"})
        assert "pdf_url" in str(exc.value.next)

    def test_blank_sha_refuses(self, store: Store, handler: PaperHandler) -> None:
        tgt = _seed(store, "at-blank")
        with pytest.raises(BadInput, match="needs a sha") as exc:
            handler.edit(id=tgt, args={"attach_pdf": "  ", "reason": "x"})
        assert exc.value.next is not None

    def test_bodied_target_refuses(self, store: Store, handler: PaperHandler) -> None:
        tgt = _seed(store, "at-bodied")
        _body(store, tgt, 1)
        sha = "ae" * 32
        _orphan_pdf(store, sha)
        with pytest.raises(BadInput, match="already holds .* body"):
            handler.edit(id=tgt, args={"attach_pdf": sha, "reason": "x"})
        assert _jobs(store, "pdf_attach") == []

    def test_dry_run_queues_nothing(self, store: Store, handler: PaperHandler) -> None:
        tgt = _seed(store, "at-dry")
        sha = "af" * 32
        _orphan_pdf(store, sha)
        resp = handler.edit(
            id=tgt, args={"attach_pdf": sha, "reason": "x"}, dry_run=True
        )
        assert "DRY RUN" in resp.body
        assert "undo:" in resp.body
        assert _jobs(store, "pdf_attach") == []
        assert _pdf_sha(store, tgt) is None


class TestPdfAttachJob:
    """``pdf_attach.attach`` — the enqueued identity gate + write."""

    def test_move_applies_on_doi_verdict(self, store: Store, tmp_path: Any) -> None:
        from precis.workers.job_types import pdf_attach

        pdf = tmp_path / "donor.pdf"
        pdf.write_bytes(b"%PDF-1.4\nfake")
        donor = _seed(store, "job-donor")
        sha = "ba" * 32
        _pdf(store, donor, sha, storage_path=str(pdf))
        tgt = _seed(store, "job-target")
        store.set_ref_identifier(tgt, "doi", "10.9/jobtgt", source="test")
        calls: dict[str, Any] = {}

        def fake_enqueue(tid: int, s: str) -> int:
            calls["reextract"] = (tid, s)
            return 4242

        rec = pdf_attach.attach(
            store,
            target_ref_id=tgt,
            donor_ref_id=donor,
            pdf_sha256=sha,
            reason="wrong ref",
            corpus_dirs=(tmp_path,),
            reader=lambda p: (3, ["the file's doi is 10.9/jobtgt"]),
            enqueue=fake_enqueue,
        )
        assert rec["status"] == "applied"
        assert rec["verdict"] == "doi"
        assert rec["reextract_job"] == 4242
        assert calls["reextract"] == (tgt, sha)
        assert _pdf_sha(store, donor) is None
        assert _pdf_sha(store, tgt) == sha
        assert _pdf_id_values(store, tgt) == [sha]
        assert len(_events(store, tgt, "pdf_moved")) == 1
        assert len(_events(store, donor, "pdf_moved")) == 1
        assert _meta(store, tgt)["pdf_attach"]["status"] == "applied"

    def test_orphan_attach_applies_title_verdict(
        self, store: Store, tmp_path: Any
    ) -> None:
        """An orphan sha (``drop_body``'s leftover) attaches on a title
        verdict; the event lands on the target only."""
        from precis.workers.job_types import pdf_attach

        pdf = tmp_path / "orphan.pdf"
        pdf.write_bytes(b"%PDF-1.4\nfake")
        sha = "bb" * 32
        _orphan_pdf(store, sha, storage_path=str(pdf))
        tgt = _seed(store, "job-orphan", title="Quantum Widgets in Lattice Arrays")
        rec = pdf_attach.attach(
            store,
            target_ref_id=tgt,
            donor_ref_id=None,
            pdf_sha256=sha,
            corpus_dirs=(tmp_path,),
            reader=lambda p: (3, ["quantum widgets lattice arrays first page"]),
            enqueue=lambda tid, s: None,
        )
        assert rec["status"] == "applied"
        assert rec["verdict"] == "title"
        assert _pdf_sha(store, tgt) == sha
        assert _pdf_id_values(store, tgt) == [sha]
        evs = _events(store, tgt, "pdf_moved")
        assert len(evs) == 1
        assert evs[0].payload["from"] is None
        assert evs[0].payload["to"] == tgt

    def test_orphan_with_retired_former_owner_journals_it(
        self, store: Store, tmp_path: Any
    ) -> None:
        """The identifier row can still name a retired former owner —
        the move re-homes the row and journals that ref too."""
        from precis.workers.job_types import pdf_attach

        pdf = tmp_path / "former.pdf"
        pdf.write_bytes(b"%PDF-1.4\nfake")
        sha = "bc" * 32
        former = _seed(store, "job-former")
        _pdf(store, former, sha, storage_path=str(pdf))
        store.retire_ref(former)
        tgt = _seed(store, "job-formtgt")
        store.set_ref_identifier(tgt, "doi", "10.9/formtgt", source="test")
        rec = pdf_attach.attach(
            store,
            target_ref_id=tgt,
            donor_ref_id=None,
            pdf_sha256=sha,
            corpus_dirs=(tmp_path,),
            reader=lambda p: (1, ["doi 10.9/formtgt here"]),
            enqueue=lambda tid, s: None,
        )
        assert rec["status"] == "applied"
        assert _pdf_sha(store, tgt) == sha
        assert _pdf_id_values(store, tgt) == [sha]
        assert _pdf_id_values(store, former) == []
        evs = _events(store, former, "pdf_moved")
        assert len(evs) == 1
        assert evs[0].payload["from"] == former

    def test_mismatch_fails_event_on_target_only(
        self, store: Store, tmp_path: Any
    ) -> None:
        from precis.workers.job_types import pdf_attach

        pdf = tmp_path / "wrong.pdf"
        pdf.write_bytes(b"%PDF-1.4\nfake")
        donor = _seed(store, "job-mmdonor")
        sha = "bd" * 32
        _pdf(store, donor, sha, storage_path=str(pdf))
        tgt = _seed(store, "job-mmtgt", title="Quantum Widgets in Lattice Arrays")
        rec = pdf_attach.attach(
            store,
            target_ref_id=tgt,
            donor_ref_id=donor,
            pdf_sha256=sha,
            corpus_dirs=(tmp_path,),
            reader=lambda p: (1, ["completely unrelated chemistry proceedings"]),
            enqueue=lambda tid, s: None,
        )
        assert rec["status"] == "failed"
        assert "identity_mismatch" in rec["reason"]
        # Nothing moved; the failure lands on the target only.
        assert _pdf_sha(store, donor) == sha
        assert _pdf_sha(store, tgt) is None
        assert len(_events(store, tgt, "pdf_attach_failed")) == 1
        assert _events(store, tgt, "pdf_moved") == []
        assert _events(store, donor, "pdf_attach_failed") == []
        assert _meta(store, tgt)["pdf_attach"]["status"] == "failed"

    def test_unchecked_fails_naming_the_metadata_fix(
        self, store: Store, tmp_path: Any
    ) -> None:
        from precis.workers.job_types import pdf_attach

        pdf = tmp_path / "unc.pdf"
        pdf.write_bytes(b"%PDF-1.4\nfake")
        sha = "be" * 32
        _orphan_pdf(store, sha, storage_path=str(pdf))
        # A placeholder title + no DOI = nothing to judge against.
        tgt = _seed(store, "job-unc", title="(no title)")
        rec = pdf_attach.attach(
            store,
            target_ref_id=tgt,
            donor_ref_id=None,
            pdf_sha256=sha,
            corpus_dirs=(tmp_path,),
            reader=lambda p: (1, ["any text at all"]),
            enqueue=lambda tid, s: None,
        )
        assert rec["status"] == "failed"
        assert "unchecked" in rec["reason"]
        assert "title=…|doi=…" in rec["next"]
        assert _pdf_sha(store, tgt) is None

    def test_target_gained_body_skips(self, store: Store, tmp_path: Any) -> None:
        from precis.workers.job_types import pdf_attach

        pdf = tmp_path / "skip.pdf"
        pdf.write_bytes(b"%PDF-1.4\nfake")
        donor = _seed(store, "job-skipdonor")
        sha = "bf" * 32
        _pdf(store, donor, sha, storage_path=str(pdf))
        tgt = _seed(store, "job-skiptgt")
        _body(store, tgt, 1)
        rec = pdf_attach.attach(
            store,
            target_ref_id=tgt,
            donor_ref_id=donor,
            pdf_sha256=sha,
            corpus_dirs=(tmp_path,),
            reader=lambda p: (1, ["anything"]),
            enqueue=lambda tid, s: None,
        )
        assert rec["status"] == "skipped"
        assert "gained a body" in rec["reason"]
        assert _pdf_sha(store, donor) == sha
        assert _ords(store, tgt) == [0]

    def test_donor_gained_body_skips(self, store: Store, tmp_path: Any) -> None:
        """A body that arrived after enqueue means body+pdf move
        together — ``move_body_to`` is the door."""
        from precis.workers.job_types import pdf_attach

        pdf = tmp_path / "dbody.pdf"
        pdf.write_bytes(b"%PDF-1.4\nfake")
        donor = _seed(store, "job-dbdonor")
        sha = "c0" * 32
        _pdf(store, donor, sha, storage_path=str(pdf))
        _body(store, donor, 1)
        tgt = _seed(store, "job-dbtgt")
        rec = pdf_attach.attach(
            store,
            target_ref_id=tgt,
            donor_ref_id=donor,
            pdf_sha256=sha,
            corpus_dirs=(tmp_path,),
            reader=lambda p: (1, ["anything"]),
            enqueue=lambda tid, s: None,
        )
        assert rec["status"] == "skipped"
        assert "donor gained a body" in rec["reason"]
        assert _pdf_sha(store, donor) == sha
        assert _pdf_sha(store, tgt) is None

    def test_sha_detached_from_donor_skips(self, store: Store, tmp_path: Any) -> None:
        """The queued sha left the donor after enqueue — never move
        whatever the pointer now names (the reextract ruling)."""
        from precis.workers.job_types import pdf_attach

        pdf = tmp_path / "det.pdf"
        pdf.write_bytes(b"%PDF-1.4\nfake")
        donor = _seed(store, "job-detdonor")
        _pdf(store, donor, "c1" * 32, storage_path=str(pdf))
        tgt = _seed(store, "job-dettgt")
        rec = pdf_attach.attach(
            store,
            target_ref_id=tgt,
            donor_ref_id=donor,
            pdf_sha256="c2" * 32,
            corpus_dirs=(tmp_path,),
            reader=lambda p: (1, ["anything"]),
            enqueue=lambda tid, s: None,
        )
        assert rec["status"] == "skipped"
        assert "no longer attached" in rec["reason"]

    def test_orphan_owned_mid_run_skips(self, store: Store, tmp_path: Any) -> None:
        """A live owner appeared between the door check and the job —
        a skip, not a steal."""
        from precis.workers.job_types import pdf_attach

        pdf = tmp_path / "owned.pdf"
        pdf.write_bytes(b"%PDF-1.4\nfake")
        owner = _seed(store, "job-own")
        sha = "c3" * 32
        _pdf(store, owner, sha, storage_path=str(pdf))
        tgt = _seed(store, "job-owntgt")
        rec = pdf_attach.attach(
            store,
            target_ref_id=tgt,
            donor_ref_id=None,
            pdf_sha256=sha,
            corpus_dirs=(tmp_path,),
            reader=lambda p: (1, ["anything"]),
            enqueue=lambda tid, s: None,
        )
        assert rec["status"] == "skipped"
        assert f"owned by live ref {owner}" in rec["reason"]

    def test_swept_missing_file_fails(self, store: Store, tmp_path: Any) -> None:
        """Swept-and-absent everywhere: terminal missing_file."""
        from precis.workers.job_types import pdf_attach

        donor = _seed(store, "job-missdonor")
        sha = "c4" * 32
        _pdf(store, donor, sha, storage_path=str(tmp_path / "gone.pdf"))
        store.record_pdf_location(sha, "testnode", "")
        tgt = _seed(store, "job-misstgt")
        rec = pdf_attach.attach(
            store,
            target_ref_id=tgt,
            donor_ref_id=donor,
            pdf_sha256=sha,
            corpus_dirs=(tmp_path,),
            reader=lambda p: (1, ["anything"]),
            enqueue=lambda tid, s: None,
        )
        assert rec["status"] == "failed"
        assert "missing_file" in rec["reason"]
        assert _pdf_sha(store, donor) == sha
        assert len(_events(store, tgt, "pdf_attach_failed")) == 1

    def test_held_elsewhere_defers(self, store: Store, tmp_path: Any) -> None:
        """No local copy but a holder node: deferred = retry."""
        from precis.workers.job_types import pdf_attach

        donor = _seed(store, "job-helddonor")
        sha = "c5" * 32
        _pdf(store, donor, sha, storage_path="/nonexistent-mount/gone.pdf")
        store.record_pdf_location(sha, "testnode", "/nonexistent-mount/gone.pdf")
        tgt = _seed(store, "job-heldtgt")
        rec = pdf_attach.attach(
            store,
            target_ref_id=tgt,
            donor_ref_id=donor,
            pdf_sha256=sha,
            corpus_dirs=(tmp_path,),
            reader=lambda p: (1, ["anything"]),
            enqueue=lambda tid, s: None,
        )
        assert rec["status"] == "deferred"
        assert rec["holders"] == ["testnode"]
        assert _pdf_sha(store, donor) == sha
        assert _events(store, tgt, "pdf_attach_failed") == []

    def test_unswept_sha_defers(self, store: Store, tmp_path: Any) -> None:
        """No ledger verdict at all: unknown, not missing — wait for the
        sweep."""
        from precis.workers.job_types import pdf_attach

        donor = _seed(store, "job-unswept")
        sha = "c6" * 32
        _pdf(store, donor, sha, storage_path=str(tmp_path / "gone.pdf"))
        tgt = _seed(store, "job-unswepttgt")
        rec = pdf_attach.attach(
            store,
            target_ref_id=tgt,
            donor_ref_id=donor,
            pdf_sha256=sha,
            corpus_dirs=(tmp_path,),
            reader=lambda p: (1, ["anything"]),
            enqueue=lambda tid, s: None,
        )
        assert rec["status"] == "deferred"
        assert "no pdf_locations verdict" in rec["reason"]

    def test_retired_target_skips(self, store: Store, tmp_path: Any) -> None:
        from precis.workers.job_types import pdf_attach

        sha = "c7" * 32
        _orphan_pdf(store, sha, storage_path=str(tmp_path / "x.pdf"))
        tgt = _seed(store, "job-rettgt")
        store.retire_ref(tgt)
        rec = pdf_attach.attach(
            store,
            target_ref_id=tgt,
            donor_ref_id=None,
            pdf_sha256=sha,
            corpus_dirs=(tmp_path,),
            reader=lambda p: (1, ["anything"]),
            enqueue=lambda tid, s: None,
        )
        assert rec["status"] == "skipped"
        assert "retired or merged" in rec["reason"]

    def test_dispatch_failed_records_failure(self, store: Store, tmp_path: Any) -> None:
        """A failed run ends the job failed — a quiet 'done' reads as
        success to whoever watches (the paper_reextract ruling)."""
        from precis.workers.executors._context import DispatchContext
        from precis.workers.job_types import pdf_attach

        donor = _seed(store, "job-dispdonor")
        sha = "c8" * 32
        _pdf(store, donor, sha, storage_path=str(tmp_path / "gone.pdf"))
        store.record_pdf_location(sha, "testnode", "")
        tgt = _seed(store, "job-disptgt")
        calls: dict[str, Any] = {"failed": [], "meta": {}}
        ctx = DispatchContext(
            store=store,
            ref_id=999999,
            title="job",
            meta={
                "params": {
                    "target_ref_id": tgt,
                    "donor_ref_id": donor,
                    "pdf_sha256": sha,
                }
            },
            set_status=lambda v: None,
            append_chunk=lambda k, t: None,
            set_meta=lambda **kw: calls["meta"].update(kw),
            record_failure=lambda msg, **kw: calls["failed"].append(msg),
            is_cancel_requested=lambda: False,
        )
        pdf_attach._dispatch(ctx, pdf_attach.SPEC)
        assert len(calls["failed"]) == 1
        assert "missing_file" in calls["failed"][0]

    def test_dispatch_deferred_parks(self, store: Store, tmp_path: Any) -> None:
        """A held-elsewhere file parks the job for a retry, counted
        under placement_parks."""
        from precis.workers.executors._context import DispatchContext
        from precis.workers.job_types import pdf_attach

        donor = _seed(store, "job-parkdonor")
        sha = "c9" * 32
        _pdf(store, donor, sha, storage_path="/nonexistent-mount/gone.pdf")
        store.record_pdf_location(sha, "testnode", "/nonexistent-mount/gone.pdf")
        tgt = _seed(store, "job-parktgt")
        calls: dict[str, Any] = {"deferred": [], "failed": [], "meta": {}}
        ctx = DispatchContext(
            store=store,
            ref_id=999999,
            title="job",
            meta={
                "params": {
                    "target_ref_id": tgt,
                    "donor_ref_id": donor,
                    "pdf_sha256": sha,
                }
            },
            set_status=lambda v: None,
            append_chunk=lambda k, t: None,
            set_meta=lambda **kw: calls["meta"].update(kw),
            record_failure=lambda msg, **kw: calls["failed"].append(msg),
            is_cancel_requested=lambda: False,
            defer=lambda wake, note: calls["deferred"].append(note) or True,
        )
        pdf_attach._dispatch(ctx, pdf_attach.SPEC)
        assert len(calls["deferred"]) == 1
        assert calls["failed"] == []
        assert calls["meta"]["pause_parks"] == 0
        assert calls["meta"]["placement_parks"] == 1

    def test_idem_key_shape(self) -> None:
        from precis.workers.job_types import pdf_attach

        assert pdf_attach.idem_key(42, "ab" * 32) == ("pdf_attach:42:" + "ab" * 32)

    def test_job_type_registered(self) -> None:
        from precis.workers.job_types import get_job_type, known_job_types

        spec = get_job_type("pdf_attach")
        assert spec is not None
        assert spec.compatible_executors == frozenset({"job_inproc"})
        assert "pdf_attach" in known_job_types()


class TestLeadingTexts:
    """``pdf_migrate.leading_texts`` — live chunks win, retired stand in."""

    def test_live_chunks_win_over_retired(self, store: Store) -> None:
        from precis.ingest import pdf_migrate

        rid = _seed(store, "lt-ref")
        store.chunks.insert_chunks(
            rid,
            [
                ChunkInsert(ord=0, text="retired page one"),
                ChunkInsert(ord=1, text="retired page two"),
            ],
        )
        with store.pool.connection() as conn:
            conn.execute(
                "UPDATE chunks SET page_first = ord + 1, retired_at = now() "
                "WHERE ref_id = %s AND ord = 0",
                (rid,),
            )
            conn.execute(
                "UPDATE chunks SET page_first = 1 WHERE ref_id = %s AND ord = 1",
                (rid,),
            )
            conn.commit()
        # ord 0 retired (page 1 history), ord 1 live on page 1: live wins.
        with store.pool.connection() as conn:
            texts = pdf_migrate.leading_texts(conn, rid)
        assert texts is not None
        assert "retired page two" in texts[0]
        assert "retired page one" not in texts[0]

    def test_no_leading_chunks_returns_none(self, store: Store) -> None:
        from precis.ingest import pdf_migrate

        rid = _seed(store, "lt-none")
        with store.pool.connection() as conn:
            assert pdf_migrate.leading_texts(conn, rid) is None


class TestLivePdfShaOwner:
    def test_pointer_and_identifier_owners(self, store: Store) -> None:
        from precis.ingest import pdf_migrate

        owner = _seed(store, "own-ref")
        sha = "d0" * 32
        _pdf(store, owner, sha)
        with store.pool.connection() as conn:
            assert pdf_migrate.live_pdf_sha_owner(conn, sha) == owner
            assert pdf_migrate.live_pdf_sha_owner(conn, "d1" * 32) is None

    def test_retired_owner_is_not_an_owner(self, store: Store) -> None:
        from precis.ingest import pdf_migrate

        owner = _seed(store, "own-ret")
        sha = "d2" * 32
        _pdf(store, owner, sha)
        store.retire_ref(owner)
        with store.pool.connection() as conn:
            assert pdf_migrate.live_pdf_sha_owner(conn, sha) is None
