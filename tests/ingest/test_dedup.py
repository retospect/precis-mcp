"""Unit tests for duplicate-reconciliation logic.

The DB-mutating merge (``merge_duplicate`` / ``reconcile_by_pdf_sha256``
apply path) is exercised by the dry-run against prod; here we pin the
survivor-selection rule — the part that, done wrong (the deleted
``dedupe-papers`` "keep lowest id"), would delete the canonical in favour
of the junk duplicate.
"""

from __future__ import annotations

from precis.ingest.dedup import ReconcileOutcome, _Cand, pick_survivor


def _c(ref_id, *, title="A real title about graphene", n_authors=3, has_ext_id=True):
    return _Cand(ref_id=ref_id, title=title, n_authors=n_authors, has_ext_id=has_ext_id)


class TestPickSurvivor:
    def test_prefers_ref_with_external_id_even_if_higher_id(self):
        # The junk dup (#31) is older/lower-id; the canonical (#5891) has a
        # DOI. Must keep the canonical — this is the exact case the old
        # dedupe-papers "lowest id" rule got backwards.
        junk = _c(31, title="", n_authors=0, has_ext_id=False)
        canonical = _c(5891, has_ext_id=True)
        assert pick_survivor([junk, canonical]) == 5891

    def test_prefers_non_junk_title_when_neither_has_id(self):
        junk = _c(10, title="No Job Name", n_authors=0, has_ext_id=False)
        good = _c(99, title="The rise of graphene", n_authors=2, has_ext_id=False)
        assert pick_survivor([junk, good]) == 99

    def test_prefers_more_authors_when_tied(self):
        a = _c(5, n_authors=1, has_ext_id=False, title="Same kind of title")
        b = _c(6, n_authors=7, has_ext_id=False, title="Same kind of title")
        assert pick_survivor([a, b]) == 6

    def test_lowest_id_only_as_final_tiebreak(self):
        a = _c(42, n_authors=3, has_ext_id=True)
        b = _c(7, n_authors=3, has_ext_id=True)
        assert pick_survivor([a, b]) == 7

    def test_id_beats_authors(self):
        # has_ext_id dominates author count.
        with_id = _c(50, n_authors=1, has_ext_id=True)
        no_id = _c(8, n_authors=20, has_ext_id=False)
        assert pick_survivor([with_id, no_id]) == 50


class TestReconcileOutcomeLine:
    def test_line(self):
        o = ReconcileOutcome(
            survivor_ref_id=5891, duplicate_ref_ids=[31, 32], key="abcdef0123456789"
        )
        line = o.line()
        assert "survivor #5891" in line
        assert "#31" in line and "#32" in line
