"""The gr720473 repair replaces only the observed study-type assignments."""

from pathlib import Path

import pytest

from precis.store import Store


@pytest.mark.db
def test_computational_studytype_repair(store: Store) -> None:
    sql = next(
        (Path(__file__).parents[1] / "src/precis/migrations").glob(
            "*_repair_computational_studytypes.sql"
        )
    ).read_text(encoding="utf-8")
    with store.pool.connection() as conn:
        conn.execute(
            "CREATE TEMP TABLE refs (ref_id bigint PRIMARY KEY, kind text,"
            " retired_at timestamptz)"
        )
        conn.execute(
            "CREATE TEMP TABLE tags (tag_id bigint GENERATED ALWAYS AS IDENTITY,"
            " namespace text, value text, UNIQUE (namespace, value))"
        )
        conn.execute(
            "CREATE TEMP TABLE ref_tags (ref_id bigint, tag_id bigint,"
            " set_by text, PRIMARY KEY (ref_id, tag_id))"
        )
        conn.execute(
            "INSERT INTO refs (ref_id, kind) VALUES"
            " (3984, 'paper'), (2857, 'paper'), (3838, 'paper'), (2973, 'paper'),"
            " (5000, 'paper')"
        )
        conn.execute(
            "INSERT INTO tags (namespace, value) VALUES"
            " ('STUDYTYPE', 'experimental-ensemble'), ('STUDYTYPE', 'synthesis'),"
            " ('STUDYTYPE', 'computational'), ('DOMAIN', 'physics')"
        )
        conn.execute(
            "INSERT INTO ref_tags VALUES (3984, 1, 'agent'), (2857, 1, 'agent'),"
            " (3838, 1, 'agent'), (2973, 2, 'agent'), (3984, 3, 'user'),"
            " (3984, 4, 'agent'), (5000, 1, 'agent')"
        )
        conn.commit()
        try:
            conn.execute(sql)
            conn.execute(sql)
            assert conn.execute(
                "SELECT rt.ref_id, t.value, rt.set_by"
                " FROM ref_tags rt JOIN tags t USING (tag_id)"
                " WHERE t.namespace = 'STUDYTYPE' ORDER BY rt.ref_id"
            ).fetchall() == [
                (2857, "computational", "system"),
                (2973, "computational", "system"),
                (3838, "computational", "system"),
                (3984, "computational", "user"),
                (5000, "experimental-ensemble", "agent"),
            ]
            assert conn.execute(
                "SELECT ref_id FROM ref_tags JOIN tags USING (tag_id)"
                " WHERE namespace = 'DOMAIN'"
            ).fetchall() == [(3984,)]
        finally:
            conn.rollback()
            conn.execute("DROP TABLE pg_temp.ref_tags, pg_temp.tags, pg_temp.refs")
            conn.commit()


@pytest.mark.db
def test_pa2857_single_entity_correction(store: Store) -> None:
    """gr720473 follow-up: 0337 corrects pa2857 (MD) left as single-entity."""
    sql = next(
        (Path(__file__).parents[1] / "src/precis/migrations").glob(
            "*_correct_simulated_studytype_pa2857.sql"
        )
    ).read_text(encoding="utf-8")
    with store.pool.connection() as conn:
        conn.execute(
            "CREATE TEMP TABLE refs (ref_id bigint PRIMARY KEY, kind text,"
            " retired_at timestamptz)"
        )
        conn.execute(
            "CREATE TEMP TABLE tags (tag_id bigint GENERATED ALWAYS AS IDENTITY,"
            " namespace text, value text, UNIQUE (namespace, value))"
        )
        conn.execute(
            "CREATE TEMP TABLE ref_tags (ref_id bigint, tag_id bigint,"
            " set_by text, PRIMARY KEY (ref_id, tag_id))"
        )
        conn.execute(
            "INSERT INTO refs (ref_id, kind) VALUES (2857, 'paper'), (3984, 'paper')"
        )
        conn.execute(
            "INSERT INTO tags (namespace, value) VALUES"
            " ('STUDYTYPE', 'experimental-single-entity'),"
            " ('STUDYTYPE', 'experimental-ensemble'),"
            " ('STUDYTYPE', 'computational')"
        )
        conn.execute(
            "INSERT INTO ref_tags VALUES (2857, 1, 'agent'), (3984, 2, 'agent')"
        )
        conn.commit()
        try:
            conn.execute(sql)
            conn.execute(sql)
            assert conn.execute(
                "SELECT rt.ref_id, t.value, rt.set_by"
                " FROM ref_tags rt JOIN tags t USING (tag_id)"
                " ORDER BY rt.ref_id"
            ).fetchall() == [
                (2857, "computational", "system"),
                (3984, "experimental-ensemble", "agent"),
            ]
        finally:
            conn.rollback()
            conn.execute("DROP TABLE pg_temp.ref_tags, pg_temp.tags, pg_temp.refs")
            conn.commit()
