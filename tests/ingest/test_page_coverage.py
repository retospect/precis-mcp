"""The ingest check and legacy scan use the same page evidence."""

from precis.ingest.db_writer import ChunkToWrite, PaperToWrite, write_paper
from precis.ingest.page_coverage import stamp_page_mismatches
from precis.store import Store


def test_ingest_and_backfill_flag_out_of_bounds_marker_span(store: Store) -> None:
    sha = "ac" * 32
    with store.tx() as conn:
        ids = [
            write_paper(
                PaperToWrite(
                    title=f"Page coverage {page_index}",
                    authors=[],
                    year=None,
                    paper_id=f"page-coverage-{page_index}",
                    cite_key_prefix="page-coverage",
                    pdf_sha256=sha,
                    pdf_page_count=17,
                    chunks=[
                        ChunkToWrite(
                            ord=0,
                            chunk_kind="paragraph",
                            text=f'<span id="page-{page_index}-0">Appendix text</span>',
                            page_first=0,
                            page_last=0,
                        )
                    ],
                ),
                conn=conn,
            ).ref_id
            for page_index in (16, 17)
        ]
    refs = store.fetch_refs_by_ids(ids)
    assert "needs_refinement" not in refs[ids[0]].meta
    evidence = {
        "last_body_page": 17,
        "pdf_page_count": 17,
        "pdf_sha256": sha,
    }
    assert refs[ids[1]].meta["needs_refinement"] == evidence
    with store.tx() as conn:
        conn.execute(
            "UPDATE refs SET meta = meta - 'needs_refinement' WHERE ref_id = %s",
            (ids[1],),
        )
        assert stamp_page_mismatches(conn, ids[0], ids[1]) == 1
        assert stamp_page_mismatches(conn, ids[0], ids[1]) == 0
    assert store.fetch_refs_by_ids(ids)[ids[1]].meta["needs_refinement"] == evidence
