"""Notice identities, per-block decisions, and document labels."""

from datetime import datetime
from pathlib import Path
from zipfile import ZipFile

from precis.export.docx import export_docx
from precis.export.latex import build_bib, render_body
from precis.ingest._provenance_rw import _merge_crossref_and_rw_notices, _RWCacheRow
from precis.ingest.provenance import crossref_update_notices, persist_retraction_notices
from precis.store import Store
from precis.store._draft_ops import content_sha
from precis_web.smartdraft import retraction_issues_in


def test_later_rw_notice_keeps_its_identity() -> None:
    crossref = crossref_update_notices(
        {
            "update-to": [{"DOI": "10.1234/notice-one", "type": "retraction"}],
        }
    )
    rows = [_RWCacheRow(None, "Retraction", [], None, None, None, record_id=72)]
    merged, _ = _merge_crossref_and_rw_notices(crossref, rows)
    assert [(n.notice_doi, n.rw_record_id) for n in merged] == [
        ("10.1234/notice-one", None),
        ("", 72),
    ]


def test_notice_lifecycle_in_viewer_and_exports(store: Store, tmp_path: Path) -> None:
    paper = store.insert_ref(kind="paper", slug="paper-test-2026", title="Cited paper")
    draft, _ = store.drafts.create_draft(name="notice-review", title="Notice review")
    a, b = store.drafts.add_chunks(
        ref_id=draft.id,
        chunk_kind="paragraph",
        text=f"Claim one [pa{paper.id}].\n\nClaim two [pa{paper.id}].",
    )
    rows = [
        _RWCacheRow(
            None, "Retraction", [], datetime(2025, 1, 1), None, None, record_id=73
        )
    ]
    notices, _ = _merge_crossref_and_rw_notices([], rows)
    assert paper.slug is not None
    with store.tx() as conn:
        first = persist_retraction_notices(conn, store, paper.id, paper.slug, notices)
        replay = persist_retraction_notices(conn, store, paper.id, paper.slug, notices)
    notice_id = first[0].persisted_ref_id
    assert notice_id is not None and replay[0].persisted_ref_id == notice_id
    assert len(store.retraction_notices(paper.id)) == 1

    assert not store.judge_retraction_use(
        notice_id,
        paper.id,
        a.chunk_id,
        content_sha(a.text),
        "unaffected",
        "Reason",
        "reviewer",
    )
    store.analyse_retraction(notice_id, "The affected assay was withdrawn.", "analyst")
    assert store.judge_retraction_use(
        notice_id,
        paper.id,
        a.chunk_id,
        content_sha(a.text),
        "unaffected",
        "Different assay",
        "reviewer",
    )
    assert store.judge_retraction_use(
        notice_id,
        paper.id,
        b.chunk_id,
        content_sha(b.text),
        "affects",
        "Same assay",
        "reviewer",
    )
    assert not retraction_issues_in(store, a.text, {}, dc=a.dc)
    assert (
        "affects this claim"
        in retraction_issues_in(store, b.text, {}, dc=b.dc)[0]["text"]
    )
    assert retraction_issues_in(store, a.text + " New claim.", {}, dc=a.dc)
    assert render_body(store, draft).body.count(f"retracted (pa{notice_id})") == 1

    output = tmp_path / "notice.docx"
    export_docx(store, draft, target_path=output)
    with ZipFile(output) as document:
        text = document.read("word/document.xml").decode("utf-8")
    assert text.count(f"retracted (pa{notice_id})") == 1
    assert "(retracted 2025)" in text

    assert store.close_retraction_use(
        notice_id,
        paper.id,
        b.chunk_id,
        content_sha(b.text),
        "The affected claim was addressed.",
        "reviewer",
    )
    assert f"retracted (pa{notice_id})" not in render_body(store, draft).body
    assert "(retracted 2025)" in build_bib(store, [paper.slug], [])

    later, _ = _merge_crossref_and_rw_notices(
        [],
        [_RWCacheRow(None, "Correction", [], None, None, None, record_id=74)],
    )
    with store.tx() as conn:
        new = persist_retraction_notices(conn, store, paper.id, paper.slug, later)
    new_id = new[0].persisted_ref_id
    assert new_id is not None and new_id != notice_id
    assert render_body(store, draft).body.count(f"corrected (pa{new_id})") == 2
