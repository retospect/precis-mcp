"""Presentation refresh: real PDF + PG, with failure and dispatch boundaries."""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import Mock

import pytest

from precis.dispatch import Hub
from precis.errors import BadInput
from precis.handlers.presentation import PresentationHandler
from precis.ingest.add import PresInput, precis_add
from precis.ingest.pres import extract_pres
from precis.runtime import PrecisRuntime
from precis.store import Store, Tag
from precis.workers.executors._context import DispatchContext
from precis.workers.job_types import get_job_type, known_job_types, pres_reextract


def _seed(store: Store, tmp_path: Path, *, text: str = "") -> tuple[int, str, Path]:
    import fitz

    path = tmp_path / "stored-deck.pdf"
    with fitz.open() as doc:
        page = doc.new_page()
        if text:
            page.insert_text((72, 72), text)
        doc.save(str(path))
    result = precis_add(PresInput(pdf_path=path), store=store)
    assert result is not None and result.pdf_sha256 is not None
    return result.ref_id, result.pdf_sha256, path


def _slides(store: Store, ref_id: int) -> list[Any]:
    with store.pool.connection() as conn:
        return conn.execute(
            "SELECT chunk_id, ord, text, meta FROM chunks "
            "WHERE ref_id = %s AND ord >= 0 ORDER BY ord",
            (ref_id,),
        ).fetchall()


def _handler(store: Store) -> PresentationHandler:
    hub = Hub(store=store)
    handler = PresentationHandler(hub=hub)
    handler._register_with(hub)
    return handler


def test_job_registration_and_open_tag() -> None:
    assert "pres_reextract" in known_job_types()
    assert get_job_type("pres_reextract") is pres_reextract.SPEC
    assert pres_reextract.SPEC.compatible_executors == frozenset({"job_inproc"})
    assert Tag.parse_strict("ocr:needed", kind="pres").namespace == "open"


def test_ingest_overview_and_unchanged_refresh(store: Store, tmp_path: Path) -> None:
    ref_id, sha, _ = _seed(store, tmp_path)
    handler = _handler(store)
    body = handler.get(id=ref_id).body
    assert "1 pages have no or little extractable text" in body
    assert "OCR is needed" in body
    assert "ocr:needed" in body
    before = _slides(store, ref_id)
    target = store.insert_ref(kind="pres", slug="linked-deck", title="Linked")
    store.add_link(
        src_ref_id=ref_id, dst_ref_id=target.id, relation="related-to", src_pos=0
    )
    with store.pool.connection() as conn:
        link_ids = conn.execute(
            "SELECT link_id FROM links WHERE src_ref_id = %s", (ref_id,)
        ).fetchall()
    record = pres_reextract.reextract(store, ref_id, pdf_sha256=sha)
    assert record["status"] == "extracted"
    assert record["image_only_pages"] == 1
    assert record["body_changed"] is False
    assert _slides(store, ref_id) == before
    with store.pool.connection() as conn:
        assert (
            conn.execute(
                "SELECT link_id FROM links WHERE src_ref_id = %s", (ref_id,)
            ).fetchall()
            == link_ids
        )


def test_refresh_replaces_changed_body_clears_flag_and_keeps_metadata(
    store: Store, tmp_path: Path
) -> None:
    ref_id, sha, _ = _seed(store, tmp_path, text="Readable stored slide with real text")
    handler = _handler(store)
    # Simulate an old broken extraction while keeping the correct source PDF.
    handler.put(
        id=store.fetch_refs_by_ids([ref_id])[ref_id].slug,
        text="Old broken extraction",
        pos=0,
    )
    store.update_ref(ref_id, meta_patch={"image_only_pages": 1, "venue": "Class"})
    store.add_tag(ref_id, Tag.open("ocr:needed"))
    before = _slides(store, ref_id)
    result = pres_reextract.reextract(store, ref_id, pdf_sha256=sha)
    assert result["body_changed"] is True
    assert result["image_only_pages"] == 0
    after = _slides(store, ref_id)
    assert after[0][0] != before[0][0]
    assert "Readable stored slide" in after[0][2]
    ref = store.fetch_refs_by_ids([ref_id])[ref_id]
    assert ref.meta["venue"] == "Class"
    assert ref.meta["reextract"]["status"] == "extracted"
    assert "ocr:needed" not in {str(t) for t in store.tags_for(ref_id)}
    assert "0 pages have no or little extractable text" in handler.get(id=ref_id).body


def test_refresh_restores_missing_flag(store: Store, tmp_path: Path) -> None:
    ref_id, sha, _ = _seed(store, tmp_path)
    store.remove_tag(ref_id, Tag.open("ocr:needed"))
    pres_reextract.reextract(store, ref_id, pdf_sha256=sha)
    assert "ocr:needed" in {str(t) for t in store.tags_for(ref_id)}


@pytest.mark.parametrize(
    "failure", ["corrupt", "wrong_bytes", "source_changed", "retired"]
)
def test_failed_extraction_preserves_body(
    store: Store, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    ref_id, sha, path = _seed(store, tmp_path, text="Keep this slide on failure")
    before = _slides(store, ref_id)
    if failure == "corrupt":
        path.write_bytes(b"not a PDF")
    elif failure == "wrong_bytes":
        # A valid PDF with different bytes must not become this deck's body.
        path.write_bytes(path.read_bytes() + b"\n% changed bytes\n")
    else:

        def change_source(pdf: Path, **kwargs: Any) -> Any:
            result = extract_pres(pdf, **kwargs)
            if failure == "retired":
                _handler(store).delete(id=ref_id)
            else:
                with store.pool.connection() as conn:
                    conn.execute(
                        "UPDATE refs SET pdf_sha256 = NULL WHERE ref_id = %s", (ref_id,)
                    )
            return result

        monkeypatch.setattr(pres_reextract, "extract_pres", change_source)
    result = pres_reextract.reextract(store, ref_id, pdf_sha256=sha)
    assert result["status"] == "failed"
    assert _slides(store, ref_id) == before


def test_swap_failure_rolls_back_body_and_metadata(
    store: Store, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ref_id, sha, _ = _seed(store, tmp_path, text="Recover the original PDF text")
    _handler(store).put(
        id=store.fetch_refs_by_ids([ref_id])[ref_id].slug, text="Old text", pos=0
    )
    before = _slides(store, ref_id)
    meta_before = store.fetch_refs_by_ids([ref_id])[ref_id].meta
    monkeypatch.setattr(
        pres_reextract,
        "write_pres_slides",
        Mock(side_effect=RuntimeError("insert failed")),
    )
    with pytest.raises(RuntimeError, match="insert failed"):
        pres_reextract.reextract(store, ref_id, pdf_sha256=sha)
    assert _slides(store, ref_id) == before
    assert store.fetch_refs_by_ids([ref_id])[ref_id].meta == meta_before


def test_missing_local_file_defers_then_missing_ledger_fails(
    store: Store, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ref_id, sha, path = _seed(store, tmp_path)
    path.unlink()
    before = _slides(store, ref_id)
    monkeypatch.setattr(store, "pdf_missing", lambda _: False)
    result = pres_reextract.reextract(store, ref_id, pdf_sha256=sha, corpus_dirs=())
    assert result["status"] == "deferred"
    monkeypatch.setattr(store, "pdf_missing", lambda _: True)
    result = pres_reextract.reextract(store, ref_id, pdf_sha256=sha, corpus_dirs=())
    assert result["status"] == "failed"
    assert "restore" in result["reason"]
    assert _slides(store, ref_id) == before


def test_edit_queues_deduplicated_job_and_preserves_body(
    store: Store, tmp_path: Path, runtime_with_store: PrecisRuntime
) -> None:
    ref_id, sha, _ = _seed(store, tmp_path)
    before = _slides(store, ref_id)
    for _ in range(2):
        body, error = runtime_with_store.dispatch_with_status(
            "edit",
            {"kind": "pres", "id": f"pr{ref_id}", "__extras__": {"reextract": True}},
        )
        assert not error, body
        assert "fitz only" in body
        assert "count only" in body
    assert "already queued" in body
    with store.pool.connection() as conn:
        jobs = conn.execute(
            "SELECT parent_id, meta FROM refs WHERE kind = 'job' "
            "AND meta->>'job_type' = 'pres_reextract'"
        ).fetchall()
    assert len(jobs) == 1
    assert jobs[0][0] == ref_id
    assert jobs[0][1]["params"] == {"ref_id": ref_id, "pdf_sha256": sha}
    assert _slides(store, ref_id) == before


def test_edit_without_stored_source_refuses(store: Store) -> None:
    ref = store.insert_ref(kind="pres", slug="manual-deck", title="Manual")
    with pytest.raises(BadInput, match="no stored source") as error:
        _handler(store).edit(id=ref.id, args={"reextract": True})
    assert "ingest" in str(error.value)


def test_edit_known_missing_source_refuses(
    store: Store, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ref_id, _, _ = _seed(store, tmp_path)
    monkeypatch.setattr(store, "pdf_missing", lambda _: True)
    with pytest.raises(BadInput, match="no stored source"):
        _handler(store).edit(id=ref_id, args={"reextract": True})


@pytest.mark.parametrize("value", [False, "true", 1])
def test_edit_rejects_non_boolean_request(store: Store, value: Any) -> None:
    ref = store.insert_ref(kind="pres", slug="manual-deck", title="Manual")
    with pytest.raises(BadInput, match="must be true"):
        _handler(store).edit(id=ref.id, args={"reextract": value})


def test_reextract_rejects_mixed_metadata_edit(store: Store) -> None:
    ref = store.insert_ref(kind="pres", slug="manual-deck", title="Manual")
    with pytest.raises(BadInput, match="cannot be combined"):
        _handler(store).edit(id=ref.id, title="Changed", args={"reextract": True})
    assert store.fetch_refs_by_ids([ref.id])[ref.id].title == "Manual"


def test_edit_args_preserve_metadata_routing_and_reject_unknown_keys(
    store: Store, runtime_with_store: PrecisRuntime
) -> None:
    ref = store.insert_ref(kind="pres", slug="metadata-deck", title="Manual")
    body, error = runtime_with_store.dispatch_with_status(
        "edit",
        {
            "kind": "pres",
            "id": f"pr{ref.id}",
            "__extras__": {"meta": {"venue": "Class"}},
        },
    )
    assert not error, body
    assert store.fetch_refs_by_ids([ref.id])[ref.id].meta["venue"] == "Class"
    body, error = runtime_with_store.dispatch_with_status(
        "edit",
        {"kind": "pres", "id": f"pr{ref.id}", "__extras__": {"reextrct": True}},
    )
    assert error
    assert "reextrct" in body


@pytest.mark.parametrize(
    ("status", "parks", "can_defer", "fails"),
    [
        ("extracted", 0, True, False),
        ("failed", 0, True, True),
        ("deferred", 0, True, False),
        ("deferred", 24, True, True),
        ("deferred", 0, False, True),
    ],
)
def test_dispatch_failure_and_bounded_placement_retry(
    monkeypatch: pytest.MonkeyPatch,
    status: str,
    parks: int,
    can_defer: bool,
    fails: bool,
) -> None:
    monkeypatch.setattr(
        pres_reextract,
        "reextract",
        lambda *a, **kw: {"status": status, "reason": "retry"},
    )
    failure = Mock()
    metadata = Mock()
    defer = Mock(return_value=True) if can_defer else None
    ctx = DispatchContext(
        store=Mock(spec=Store),
        ref_id=99,
        title="Refresh",
        meta={
            "params": {"ref_id": 42, "pdf_sha256": "a" * 64},
            "placement_parks": parks,
        },
        set_status=Mock(),
        append_chunk=Mock(),
        set_meta=metadata,
        record_failure=failure,
        is_cancel_requested=lambda: False,
        defer=defer,
    )
    pres_reextract._dispatch(ctx, pres_reextract.SPEC)
    assert failure.called is fails
    if status == "deferred" and parks == 0:
        metadata.assert_called_with(pause_parks=0, placement_parks=1)
    if status == "extracted":
        metadata.assert_called_with(pres_ref_id=42, reextract_status="extracted")
