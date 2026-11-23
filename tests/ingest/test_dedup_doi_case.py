"""DOI-case reconciliation + the DOI storage guards (migrations 0049, 0358).

Covers the bug where a chase/dream stub minted with a lowercase DOI
(`.../d19-1371`) never met an ingested PDF stored with the publisher's
verbatim case (`.../D19-1371`): the exact-match stub upgrade missed, the paper
landed as a second ref, and the stub stayed on the "papers we still need to
get" backlog forever. Migration 0358 broadens the same guard to trailing
citation punctuation — `10.1149/2.1221816jes]` vs `10.1149/2.1221816jes`
held one paper as two live full-text records (gr763049).

Real-PG only (needs the trigger + constraints). Skips without a DB.
"""

from __future__ import annotations

from psycopg import Connection

from precis.dispatch import Hub
from precis.handlers.paper import PaperHandler
from precis.ingest.add import _reconcile_orphan_stub
from precis.ingest.dedup import audit_doi_trailing_junk, reconcile_by_doi_case
from precis.store import ChunkInsert, Store

_SHA = "a" * 64  # a fake but well-formed pdf_sha256


def _suspend_doi_guards(conn: Connection) -> None:
    """Drop the storage-layer DOI canonicalisers so a verbatim (dirty)
    DOI can be planted — reproduces the pre-guard rows prod still holds."""
    conn.execute("ALTER TABLE ref_identifiers DISABLE TRIGGER USER")
    conn.execute("ALTER TABLE ref_identifiers DROP CONSTRAINT ref_identifiers_doi_lc")
    conn.execute(
        "ALTER TABLE ref_identifiers "
        "DROP CONSTRAINT IF EXISTS ref_identifiers_doi_clean"
    )


def _restore_doi_guards(conn: Connection) -> None:
    conn.execute(
        "ALTER TABLE ref_identifiers ADD CONSTRAINT ref_identifiers_doi_lc "
        "CHECK (id_kind <> 'doi' OR id_value = lower(id_value)) NOT VALID"
    )
    conn.execute(
        "ALTER TABLE ref_identifiers ADD CONSTRAINT ref_identifiers_doi_clean "
        "CHECK (id_kind <> 'doi' OR id_value = "
        "ref_identifiers_doi_canonical(id_value)) NOT VALID"
    )
    conn.execute("ALTER TABLE ref_identifiers ENABLE TRIGGER USER")


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
        _suspend_doi_guards(conn)
        try:
            conn.execute(
                "INSERT INTO ref_identifiers (id_kind, id_value, ref_id, source) "
                "VALUES ('doi', %s, %s, 'test')",
                (doi, ref.id),
            )
        finally:
            _restore_doi_guards(conn)
    return ref.id


def _mk_stub(store: Store, *, slug: str, doi: str, verbatim: bool = False) -> int:
    """Insert a PDF-less stub carrying a DOI.

    ``verbatim=True`` plants the DOI with its publisher case intact — bypassing
    the storage guards the same way :func:`_mk_pdf_paper` does — to reproduce
    the *production* orientation where the stub holds the mixed-case (or
    punctuation-tailed) DOI and the ingested survivor holds the canonical
    one. The default lets the trigger canonicalise it.
    """
    with store.tx() as conn:
        ref = store.insert_ref(kind="paper", slug=slug, title="SciBERT", conn=conn)
        if verbatim:
            _suspend_doi_guards(conn)
        try:
            conn.execute(
                "INSERT INTO ref_identifiers (id_kind, id_value, ref_id, source) "
                "VALUES ('doi', %s, %s, 'test')",
                (doi, ref.id),
            )
        finally:
            if verbatim:
                _restore_doi_guards(conn)
    return ref.id


def _is_deleted(store: Store, ref_id: int) -> bool:
    with store.pool.connection() as conn:
        row = conn.execute(
            "SELECT retired_at IS NOT NULL FROM refs WHERE ref_id = %s", (ref_id,)
        ).fetchone()
    assert row is not None
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
            val_row = conn.execute(
                "SELECT id_value FROM ref_identifiers "
                "WHERE ref_id = %s AND id_kind = 'doi'",
                (ref.id,),
            ).fetchone()
        assert val_row is not None
        assert val_row[0] == "10.18653/v1/d19-1371"

    def test_non_doi_identifier_is_untouched(self, store: Store):
        with store.tx() as conn:
            ref = store.insert_ref(kind="paper", slug="trig2", title="T", conn=conn)
            conn.execute(
                "INSERT INTO ref_identifiers (id_kind, id_value, ref_id, source) "
                "VALUES ('s2', 'ABC123def', %s, 'test')",
                (ref.id,),
            )
        with store.pool.connection() as conn:
            val_row = conn.execute(
                "SELECT id_value FROM ref_identifiers "
                "WHERE ref_id = %s AND id_kind = 's2'",
                (ref.id,),
            ).fetchone()
        assert val_row is not None
        assert val_row[0] == "ABC123def"


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

    def test_merges_when_stub_holds_mixed_case_doi(self, store: Store):
        # Production orientation (the mirror of the case above): the ingested
        # paper holds the *lowercase* DOI and the stub carries the publisher's
        # mixed-case one. Moving the stub's DOI onto the survivor re-fires the
        # 0049 lowercase trigger, so without the drop-then-migrate guard in
        # merge_duplicate the write collides with the survivor's existing
        # lowercase row (the prod UniqueViolation that stalled 109 groups).
        paper = _mk_pdf_paper(store, slug="beltagy19", doi="10.18653/v1/d19-1371")
        stub = _mk_stub(
            store, slug="scibert19", doi="10.18653/v1/D19-1371", verbatim=True
        )

        outcomes = reconcile_by_doi_case(store, dry_run=False)

        assert len(outcomes) == 1
        assert outcomes[0].survivor_ref_id == paper
        assert outcomes[0].duplicate_ref_ids == [stub]
        assert _is_deleted(store, stub)
        assert not _is_deleted(store, paper)

        with store.pool.connection() as conn:
            rows = conn.execute(
                "SELECT id_value, ref_id FROM ref_identifiers WHERE id_kind = 'doi'"
            ).fetchall()
        doi_rows = [(v, r) for v, r in rows if v.endswith("d19-1371")]
        assert doi_rows == [("10.18653/v1/d19-1371", paper)]

    def test_validates_constraint_after_clean_pass(self, store: Store):
        # A successful reconcile that leaves zero mixed-case DOIs promotes the
        # NOT-VALID guard, so the nightly sweep finishes the post-deploy step.
        _mk_pdf_paper(store, slug="beltagy19", doi="10.18653/v1/d19-1371")
        _mk_stub(store, slug="scibert19", doi="10.18653/v1/D19-1371", verbatim=True)

        reconcile_by_doi_case(store, dry_run=False)

        with store.pool.connection() as conn:
            validated_row = conn.execute(
                "SELECT convalidated FROM pg_constraint "
                "WHERE conname = 'ref_identifiers_doi_lc'"
            ).fetchone()
        assert validated_row is not None
        assert validated_row[0] is True

    def test_dry_run_writes_nothing(self, store: Store):
        paper = _mk_pdf_paper(store, slug="beltagy19", doi="10.18653/v1/D19-1371")
        stub = _mk_stub(store, slug="scibert19", doi="10.18653/v1/d19-1371")

        outcomes = reconcile_by_doi_case(store, dry_run=True)

        assert len(outcomes) == 1
        assert outcomes[0].survivor_ref_id == paper
        assert not _is_deleted(store, stub)
        assert not _is_deleted(store, paper)

    def test_body_bearing_ref_beats_bodiless_pdf_ref(self, store: Store):
        # gr453860: the PDF-carrying ref here has NO body chunk (a promoted
        # stub whose extraction wrote nothing), while the "stub" was ingested
        # from markup — no PDF, but real text. Keep the text, not the bytes.
        bodiless = _mk_pdf_paper(store, slug="beltagy19", doi="10.18653/v1/D19-1371")
        markup = _mk_stub(store, slug="scibert19", doi="10.18653/v1/d19-1371")
        with store.tx() as conn:
            conn.execute(
                "INSERT INTO chunks (ref_id, ord, chunk_kind, text) "
                "VALUES (%s, 0, 'paragraph', 'A body paragraph from JATS.')",
                (markup,),
            )

        outcomes = reconcile_by_doi_case(store, dry_run=False)

        assert len(outcomes) == 1
        assert outcomes[0].survivor_ref_id == markup
        assert outcomes[0].duplicate_ref_ids == [bodiless]
        assert _is_deleted(store, bodiless)
        assert not _is_deleted(store, markup)

    def test_noop_when_no_case_collision(self, store: Store):
        # Two genuinely different DOIs — no group, nothing merged.
        _mk_stub(store, slug="a", doi="10.1/aaa")
        _mk_stub(store, slug="b", doi="10.1/bbb")
        assert reconcile_by_doi_case(store, dry_run=False) == []

    def test_uncorroborated_group_skipped_and_todo_raised(self, store: Store):
        # gr501446: pa42498 was merged into pa44470 on a case-variant DOI
        # alone — same year, entirely different titles (a DOI typo or
        # publisher reuse). The gate must refuse the merge and file a
        # review todo instead.
        paper = _mk_pdf_paper_titled(
            store,
            slug="cnt-graphene",
            doi="10.1000/CNT.GRAPH",
            title="Electronic Structure of Carbon Nanotubes on Graphene Substrates",
            year=2019,
        )
        stub = _mk_stub_titled(
            store,
            slug="cholesteric",
            doi="10.1000/cnt.graph",
            title="Pitch tuning induced by optical torque in heliconical "
            "cholesteric liquid crystals",
            year=2019,
        )

        outcomes = reconcile_by_doi_case(store, dry_run=False)

        assert len(outcomes) == 1
        assert outcomes[0].skipped is not None
        assert not _is_deleted(store, stub) and not _is_deleted(store, paper)

        todos = _review_todos(store)
        assert len(todos) == 1
        assert {paper, stub} == set(todos[0]["meta"]["ref_ids"])
        assert todos[0]["meta"]["doi"] == "10.1000/cnt.graph"

        # A second pass must not re-file the standing todo.
        reconcile_by_doi_case(store, dry_run=False)
        assert len(_review_todos(store)) == 1

    def test_uncorroborated_group_skipped_in_dry_run_no_todo(self, store: Store):
        _mk_pdf_paper_titled(
            store,
            slug="cnt-graphene",
            doi="10.1000/CNT.GRAPH",
            title="Electronic Structure of Carbon Nanotubes on Graphene Substrates",
        )
        _mk_stub_titled(
            store,
            slug="cholesteric",
            doi="10.1000/cnt.graph",
            title="Pitch tuning induced by optical torque in heliconical "
            "cholesteric liquid crystals",
        )

        outcomes = reconcile_by_doi_case(store, dry_run=True)

        assert len(outcomes) == 1 and outcomes[0].skipped is not None
        assert _review_todos(store) == []

    def test_corroborated_by_author_and_year_merges(self, store: Store):
        # Titles differ in wording but the same first author + year is the
        # same corroboration the other merge paths accept — merge proceeds.
        paper = _mk_pdf_paper_titled(
            store,
            slug="cnt-graphene",
            doi="10.1000/CNT.GRAPH",
            title="Electronic Structure of Carbon Nanotubes on Graphene",
            year=2019,
            authors=[{"name": "Beltagy, I."}],
        )
        stub = _mk_stub_titled(
            store,
            slug="cnt-graphene-stub",
            doi="10.1000/cnt.graph",
            title="Electronic structure of graphene-supported carbon nanotubes",
            year=2019,
            authors=[{"name": "Beltagy, I."}],
        )

        outcomes = reconcile_by_doi_case(store, dry_run=False)

        assert len(outcomes) == 1 and outcomes[0].skipped is None
        assert _is_deleted(store, stub) and not _is_deleted(store, paper)
        assert _review_todos(store) == []


def _mk_pdf_paper_titled(
    store: Store,
    *,
    slug: str,
    doi: str,
    title: str,
    year: int | None = None,
    authors: list | None = None,
) -> int:
    """``_mk_pdf_paper`` with the identity fields the gr501446 gate reads."""
    import hashlib

    sha = hashlib.sha256(slug.encode()).hexdigest()
    with store.tx() as conn:
        conn.execute(
            "INSERT INTO pdfs (pdf_sha256, content_hash, page_count, "
            "size_bytes, storage_path) VALUES (%s, %s, 1, 1, '') "
            "ON CONFLICT (pdf_sha256) DO NOTHING",
            (sha, sha),
        )
        ref = store.insert_ref(
            kind="paper",
            slug=slug,
            title=title,
            year=year,
            authors=authors,
            conn=conn,
        )
        conn.execute("UPDATE refs SET pdf_sha256 = %s WHERE ref_id = %s", (sha, ref.id))
        _suspend_doi_guards(conn)
        try:
            conn.execute(
                "INSERT INTO ref_identifiers (id_kind, id_value, ref_id, source) "
                "VALUES ('doi', %s, %s, 'test')",
                (doi, ref.id),
            )
        finally:
            _restore_doi_guards(conn)
    return ref.id


def _mk_stub_titled(
    store: Store,
    *,
    slug: str,
    doi: str,
    title: str,
    year: int | None = None,
    authors: list | None = None,
) -> int:
    """A PDF-less stub with an explicit title/year/authors (lowercase DOI)."""
    with store.tx() as conn:
        ref = store.insert_ref(
            kind="paper",
            slug=slug,
            title=title,
            year=year,
            authors=authors,
            conn=conn,
        )
        conn.execute(
            "INSERT INTO ref_identifiers (id_kind, id_value, ref_id, source) "
            "VALUES ('doi', %s, %s, 'test')",
            (doi, ref.id),
        )
    return ref.id


def _review_todos(store: Store) -> list[dict]:
    with store.pool.connection() as conn:
        rows = conn.execute(
            "SELECT ref_id, meta FROM refs WHERE kind = 'todo' "
            "AND retired_at IS NULL AND meta->>'source' = 'reconcile:doi-case-review'"
        ).fetchall()
    return [{"ref_id": int(r[0]), "meta": r[1]} for r in rows]


class TestDoiTrailingPunctGuard:
    """gr763049 — trailing citation punctuation is not a DOI tail."""

    def test_insert_strips_trailing_bracket(self, store: Store) -> None:
        # The 0358 trigger canonicalises on write: a pasted `[doi:…]` tail
        # can no longer be stored.
        with store.tx() as conn:
            ref = store.insert_ref(kind="paper", slug="trig3", title="T", conn=conn)
            conn.execute(
                "INSERT INTO ref_identifiers (id_kind, id_value, ref_id, source) "
                "VALUES ('doi', '10.1149/2.1221816jes]', %s, 'test')",
                (ref.id,),
            )
        with store.pool.connection() as conn:
            val_row = conn.execute(
                "SELECT id_value FROM ref_identifiers "
                "WHERE ref_id = %s AND id_kind = 'doi'",
                (ref.id,),
            ).fetchone()
        assert val_row is not None
        assert val_row[0] == "10.1149/2.1221816jes"

    def test_punctuation_collisions_do_not_block_case_merge(self, store: Store) -> None:
        first = _mk_stub(store, slug="dirty-first", doi="10.1000/x]", verbatim=True)
        second = _mk_stub(store, slug="dirty-second", doi="10.1000/x,", verbatim=True)
        paper = _mk_pdf_paper(store, slug="held-a", doi="10.1000/CASE")
        stub = _mk_stub(store, slug="stub-a", doi="10.1000/case")

        outcomes = reconcile_by_doi_case(store, dry_run=False)

        assert len(outcomes) == 1 and outcomes[0].skipped is None
        assert _is_deleted(store, stub) and not _is_deleted(store, paper)
        assert {row.ref_id for row in audit_doi_trailing_junk(store)} == {first, second}

    def test_merge_into_full_text_bracket_twin(self, store: Store, hub: Hub) -> None:
        doi = "10.1149/2.1221816jes"
        title = (
            "Design Principles of Bipolar Electrochemical Co-Electrolysis Cells "
            "for Efficient Reduction of Carbon Dioxide from Gas Phase at Low Temperature"
        )
        winner = _mk_stub(store, slug="patru19", doi=doi)
        loser = _mk_stub(store, slug="patru19a", doi=doi + "]", verbatim=True)
        with store.tx() as conn:
            conn.execute(
                "UPDATE refs SET title = %s, year = 2019 WHERE ref_id = ANY(%s)",
                (title, [winner, loser]),
            )
        store.chunks.insert_chunks(winner, [ChunkInsert(ord=0, text="survivor body")])
        store.chunks.insert_chunks(loser, [ChunkInsert(ord=0, text="duplicate body")])

        handler = PaperHandler(hub=hub)
        handler.edit(
            id=f"pa{loser}",
            args={
                "merge_into": f"pa{winner}",
                "reason": "gr763049 confirmed duplicate",
            },
        )

        assert _is_deleted(store, loser) and not _is_deleted(store, winner)
        resolved = store.resolve_handle(f"pa{loser}")
        assert resolved is not None and resolved.ref_id == winner
        with store.pool.connection() as conn:
            assert conn.execute(
                "SELECT id_value, ref_id FROM ref_identifiers WHERE id_kind = 'doi'"
            ).fetchall() == [(doi, winner)]
            assert conn.execute(
                "SELECT text FROM chunks WHERE ref_id = %s AND ord >= 0",
                (winner,),
            ).fetchall() == [("survivor body",)]

    def test_orphan_stub_fold_handles_doi_twin(self, store: Store) -> None:
        doi = "10.1000/orphan"
        winner = _mk_stub(store, slug="orphan-winner", doi=doi)
        loser = _mk_stub(store, slug="orphan-loser", doi=doi + "]", verbatim=True)
        with store.tx() as conn:
            conn.execute(
                "UPDATE refs SET title = %s WHERE ref_id = ANY(%s)",
                ("Spectroscopy of metalloproteins in solution", [winner, loser]),
            )
            store.set_ref_identifier(
                loser, "cite_key", "orphan19", source="test", conn=conn
            )
            assert (
                _reconcile_orphan_stub(
                    store, survivor_ref_id=winner, file_stem="orphan19", conn=conn
                )
                == loser
            )

        assert _is_deleted(store, loser)
        assert store.find_ref_by_identifier("doi", doi) == winner

    def test_drop_removes_legacy_doi_and_suppresses_canonical_readd(
        self, store: Store
    ) -> None:
        doi = "10.1000/drop"
        ref = _mk_stub(store, slug="legacy-drop", doi=doi + "]", verbatim=True)
        assert store.drop_ref_identifier(ref, "doi", doi + "]") == doi + "]"
        store.stamp_ref_meta(ref, {"dropped_identifiers": {"doi": {doi + "]": {}}}})
        store.insert_ref_identifiers(ref, [("doi", doi, "system")])
        assert store.find_ref_by_identifier("doi", doi) is None

    def test_merge_propagates_legacy_clear_to_identifierless_winner(
        self, store: Store, hub: Hub
    ) -> None:
        title = "Electrochemical reactions on platinum crystal surfaces"
        winner = store.insert_ref(
            kind="paper", title=title, slug="clear-win", year=2020
        )
        loser = store.insert_ref(
            kind="paper", title=title, slug="clear-lose", year=2020
        )
        marker = {"doi": {"values": ["10.1000/clear]"], "by": "web-edit"}}
        store.stamp_ref_meta(loser.id, {"cleared_identifiers": marker})

        PaperHandler(hub=hub).edit(
            id=loser.id,
            args={"merge_into": f"pa{winner.id}", "reason": "confirmed duplicate"},
        )

        assert (
            store.fetch_refs_by_ids([winner.id])[winner.id].meta["cleared_identifiers"]
            == marker
        )

    def test_lone_dirty_doi_canonicalised(self, store: Store) -> None:
        # No twin anywhere: the post-merge sweep scrubs the stored row.
        stub = _mk_stub(store, slug="lone", doi="10.5555/solo.doi]", verbatim=True)

        reconcile_by_doi_case(store, dry_run=False)

        with store.pool.connection() as conn:
            val_row = conn.execute(
                "SELECT id_value FROM ref_identifiers "
                "WHERE ref_id = %s AND id_kind = 'doi'",
                (stub,),
            ).fetchone()
        assert val_row is not None
        assert val_row[0] == "10.5555/solo.doi"
        assert not _is_deleted(store, stub)

    def test_audit_lists_stored_trailing_junk(self, store: Store) -> None:
        paper = _mk_pdf_paper(store, slug="held", doi="10.1149/2.1221816jes")
        stub = _mk_stub(store, slug="dup", doi="10.1149/2.1221816jes]", verbatim=True)
        lone = _mk_stub(store, slug="lone2", doi="10.7777/trail,", verbatim=True)

        case_only = _mk_stub(store, slug="case-only", doi="10.7777/CASE", verbatim=True)
        store.retire_ref(lone)

        rows = audit_doi_trailing_junk(store)

        by_ref = {r.ref_id: r for r in rows}
        assert by_ref[stub].id_value == "10.1149/2.1221816jes]"
        assert by_ref[stub].canonical == "10.1149/2.1221816jes"
        assert by_ref[stub].twin_ref_id == paper
        assert by_ref[lone].canonical == "10.7777/trail"
        assert by_ref[lone].twin_ref_id is None
        assert paper not in by_ref
        assert case_only not in by_ref

    def test_clean_constraint_validates_after_canonical_pass(
        self, store: Store
    ) -> None:
        # Once no non-canonical DOI remains, the sweep promotes the 0358
        # CHECK alongside 0049's.
        _mk_pdf_paper(store, slug="beltagy19", doi="10.18653/v1/d19-1371")

        reconcile_by_doi_case(store, dry_run=False)

        with store.pool.connection() as conn:
            row = conn.execute(
                "SELECT convalidated FROM pg_constraint "
                "WHERE conname = 'ref_identifiers_doi_clean'"
            ).fetchone()
        assert row is not None
        assert row[0] is True
