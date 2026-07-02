"""DOI-case reconciliation + the lowercase storage guard (migration 0046).

Covers the bug where a chase/dream stub minted with a lowercase DOI
(`.../d19-1371`) never met an ingested PDF stored with the publisher's
verbatim case (`.../D19-1371`): the exact-match stub upgrade missed, the paper
landed as a second ref, and the stub stayed on the "papers we still need to
get" backlog forever.

Real-PG only (needs the 0046 trigger + constraint). Skips without a DB.
"""

from __future__ import annotations

from precis.ingest.dedup import reconcile_by_doi_case
from precis.store import Store

_SHA = "a" * 64  # a fake but well-formed pdf_sha256


def _mk_pdf_paper(store: Store, *, slug: str, doi: str) -> int:
    """Insert an 'ingested' paper: a refs row with a real pdf_sha256 + DOI.

    The DOI row is planted with a verbatim (non-lowercase) value to reproduce
    the pre-0046 legacy state the reconcile must clean. Both the lowercase
    trigger and the CHECK constraint are bypassed for that one insert (the
    constraint enforces on new rows even though it's added NOT VALID), then
    restored, so the rest of the DB behaves exactly as in production.
    """
    with store.tx() as conn:
        conn.execute(
            "INSERT INTO pdfs (pdf_sha256, content_hash, page_count, "
            "size_bytes, storage_path) VALUES (%s, %s, 1, 1, '') "
            "ON CONFLICT (pdf_sha256) DO NOTHING",
            (_SHA, _SHA),
        )
        ref = store.insert_ref(kind="paper", slug=slug, title="SciBERT", conn=conn)
        conn.execute(
            "UPDATE refs SET pdf_sha256 = %s WHERE ref_id = %s", (_SHA, ref.id)
        )
        conn.execute("ALTER TABLE ref_identifiers DISABLE TRIGGER USER")
        conn.execute(
            "ALTER TABLE ref_identifiers DROP CONSTRAINT ref_identifiers_doi_lc"
        )
        try:
            conn.execute(
                "INSERT INTO ref_identifiers (id_kind, id_value, ref_id, source) "
                "VALUES ('doi', %s, %s, 'test')",
                (doi, ref.id),
            )
        finally:
            conn.execute(
                "ALTER TABLE ref_identifiers ADD CONSTRAINT ref_identifiers_doi_lc "
                "CHECK (id_kind <> 'doi' OR id_value = lower(id_value)) NOT VALID"
            )
            conn.execute("ALTER TABLE ref_identifiers ENABLE TRIGGER USER")
    return ref.id


def _mk_stub(store: Store, *, slug: str, doi: str) -> int:
    """Insert a PDF-less stub carrying a (lowercase) DOI."""
    with store.tx() as conn:
        ref = store.insert_ref(kind="paper", slug=slug, title="SciBERT", conn=conn)
        conn.execute(
            "INSERT INTO ref_identifiers (id_kind, id_value, ref_id, source) "
            "VALUES ('doi', %s, %s, 'test')",
            (doi, ref.id),
        )
    return ref.id


def _is_deleted(store: Store, ref_id: int) -> bool:
    with store.pool.connection() as conn:
        row = conn.execute(
            "SELECT deleted_at IS NOT NULL FROM refs WHERE ref_id = %s", (ref_id,)
        ).fetchone()
    return bool(row[0])


class TestDoiLowercaseTrigger:
    def test_insert_lowercases_doi(self, store: Store):
        with store.tx() as conn:
            ref = store.insert_ref(kind="paper", slug="trig1", title="T", conn=conn)
            conn.execute(
                "INSERT INTO ref_identifiers (id_kind, id_value, ref_id, source) "
                "VALUES ('doi', '10.18653/v1/D19-1371', %s, 'test')",
                (ref.id,),
            )
        with store.pool.connection() as conn:
            val = conn.execute(
                "SELECT id_value FROM ref_identifiers "
                "WHERE ref_id = %s AND id_kind = 'doi'",
                (ref.id,),
            ).fetchone()[0]
        assert val == "10.18653/v1/d19-1371"

    def test_non_doi_identifier_is_untouched(self, store: Store):
        with store.tx() as conn:
            ref = store.insert_ref(kind="paper", slug="trig2", title="T", conn=conn)
            conn.execute(
                "INSERT INTO ref_identifiers (id_kind, id_value, ref_id, source) "
                "VALUES ('s2', 'ABC123def', %s, 'test')",
                (ref.id,),
            )
        with store.pool.connection() as conn:
            val = conn.execute(
                "SELECT id_value FROM ref_identifiers "
                "WHERE ref_id = %s AND id_kind = 's2'",
                (ref.id,),
            ).fetchone()[0]
        assert val == "ABC123def"


class TestReconcileByDoiCase:
    def test_merges_stub_into_chunked_and_lowercases(self, store: Store):
        # Legacy split: stub has the lowercase DOI, ingested paper has the
        # publisher-cased one. Same paper, two refs.
        paper = _mk_pdf_paper(store, slug="beltagy19", doi="10.18653/v1/D19-1371")
        stub = _mk_stub(store, slug="scibert19", doi="10.18653/v1/d19-1371")

        outcomes = reconcile_by_doi_case(store, dry_run=False)

        # Exactly one group collapsed, keeping the chunked paper.
        assert len(outcomes) == 1
        assert outcomes[0].survivor_ref_id == paper
        assert outcomes[0].duplicate_ref_ids == [stub]

        # Stub retired, paper survives.
        assert _is_deleted(store, stub)
        assert not _is_deleted(store, paper)

        # The DOI now lives once, lowercased, on the survivor.
        with store.pool.connection() as conn:
            rows = conn.execute(
                "SELECT id_value, ref_id FROM ref_identifiers WHERE id_kind = 'doi'"
            ).fetchall()
        doi_rows = [(v, r) for v, r in rows if v.endswith("d19-1371")]
        assert doi_rows == [("10.18653/v1/d19-1371", paper)]

    def test_dry_run_writes_nothing(self, store: Store):
        paper = _mk_pdf_paper(store, slug="beltagy19", doi="10.18653/v1/D19-1371")
        stub = _mk_stub(store, slug="scibert19", doi="10.18653/v1/d19-1371")

        outcomes = reconcile_by_doi_case(store, dry_run=True)

        assert len(outcomes) == 1
        assert outcomes[0].survivor_ref_id == paper
        assert not _is_deleted(store, stub)
        assert not _is_deleted(store, paper)

    def test_noop_when_no_case_collision(self, store: Store):
        # Two genuinely different DOIs — no group, nothing merged.
        _mk_stub(store, slug="a", doi="10.1/aaa")
        _mk_stub(store, slug="b", doi="10.1/bbb")
        assert reconcile_by_doi_case(store, dry_run=False) == []
