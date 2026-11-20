"""gr681475: replay the targeted repair without touching corpus records."""

from datetime import UTC, datetime
from pathlib import Path

import pytest

from precis.handlers._paper_format import _render_citation
from precis.store import Ref, Store


@pytest.mark.db
@pytest.mark.parametrize("mismatch", [None, "body", "doi", "kind"])
def test_zeolite_repair_is_guarded_and_idempotent(
    store: Store, mismatch: str | None
) -> None:
    sql = next(
        (Path(__file__).parents[1] / "src/precis/migrations").glob(
            "*_repair_zeolite_metadata.sql"
        )
    ).read_text(encoding="utf-8")
    title = (
        "Advances in theory and their application within the field of zeolite chemistry"
    )
    with store.pool.connection() as conn:
        conn.execute(
            "CREATE TEMP TABLE refs (ref_id bigint PRIMARY KEY, kind text, title text,"
            " year integer, authors jsonb, meta jsonb, updated_at timestamptz)"
        )
        conn.execute(
            "CREATE TEMP TABLE ref_identifiers (id_kind text, id_value text,"
            " ref_id bigint, source text, PRIMARY KEY (id_kind, id_value))"
        )
        conn.execute("CREATE TEMP TABLE chunks (ref_id bigint, ord integer, text text)")
        conn.execute(
            "CREATE TEMP TABLE paper_authors (ref_id bigint, position integer,"
            " given text, family text, name_raw text, source text)"
        )
        conn.execute(
            "INSERT INTO refs VALUES (5022, 'paper', 'Compressed sensing', 2009,"
            " '[]', '{\"keep\":true}', NULL),"
            " (5023, 'paper', 'Unrelated', 2009, '[]', '{}', NULL)"
        )
        conn.execute(
            "INSERT INTO ref_identifiers VALUES"
            " ('doi', '10.1039/c5cs00029g', 5022, 'test'),"
            " ('cite_key', 'wang09c', 5022, 'test')"
        )
        conn.execute(
            "INSERT INTO chunks VALUES (5022, 0, %s)",
            ("Another PDF" if mismatch == "body" else title,),
        )
        if mismatch == "doi":
            conn.execute("DELETE FROM ref_identifiers WHERE id_kind = 'doi'")
        if mismatch == "kind":
            conn.execute("UPDATE refs SET kind = 'book' WHERE ref_id = 5022")
        conn.commit()
        try:
            conn.execute(sql)
            conn.execute(sql)
            row = conn.execute(
                "SELECT title, year, meta, authors FROM refs WHERE ref_id = 5022"
            ).fetchone()
            assert row is not None
            if mismatch is None:
                assert row[0:2] == (title, 2015)
                assert row[2]["keep"] is True
                assert len(row[3]) == 6
                assert row[3][0]["family"] == "Van Speybroeck"
                assert conn.execute(
                    "SELECT id_kind FROM ref_identifiers WHERE id_value = 'wang09c'"
                ).fetchone() == ("cite_key_alias",)
                assert conn.execute(
                    "SELECT count(*) FROM paper_authors"
                ).fetchone() == (6,)
                key = conn.execute(
                    "SELECT id_value FROM ref_identifiers WHERE id_kind = 'cite_key'"
                ).fetchone()
                assert key == ("vanspeybroeck15-pa5022",)
                now = datetime.now(UTC)
                ref = Ref(
                    id=5022,
                    kind="paper",
                    slug=key[0],
                    title=row[0],
                    year=row[1],
                    meta=row[2],
                    authors=row[3],
                    provider=None,
                    created_at=now,
                    updated_at=now,
                    retired_at=None,
                )
                bib = _render_citation(ref, style="bibtex", doi="10.1039/c5cs00029g")
                assert bib.startswith("@article{vanspeybroeck15-pa5022,")
                assert title in bib
                assert "Van Speybroeck, Veronique" in bib
                assert "year = {2015}" in bib
                assert "Wang" not in bib
            else:
                assert row[0:2] == ("Compressed sensing", 2009)
            assert conn.execute(
                "SELECT title FROM refs WHERE ref_id = 5023"
            ).fetchone() == ("Unrelated",)
            assert conn.execute("SELECT text FROM chunks").fetchone() == (
                "Another PDF" if mismatch == "body" else title,
            )
        finally:
            conn.execute("DROP TABLE paper_authors, chunks, ref_identifiers, refs")
            conn.commit()
