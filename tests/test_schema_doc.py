"""Tests for ``precis schema-doc`` rendering (the pure, no-DB half).

The psycopg path (``_fetch_via_dsn``) is exercised by the live
``scripts/gen-schema`` regen; here we pin the parse + Mermaid render
against fixture rows so the artifact shape can't regress.
"""

from __future__ import annotations

from precis.cli.schema_doc import (
    INTROSPECT_SQL,
    _safe_type,
    parse_tsv,
    render_mermaid,
)
from precis.store import Store

# A tiny two-table fixture in the same tab-separated shape the
# introspection query emits: 'col' rows then an 'fk' row.
_TSV = "\n".join(
    [
        "col\trefs\t1\tref_id\tbigint\tPK\t",
        "col\trefs\t2\tkind\ttext\t\t",
        "col\trefs\t3\tparent_id\tbigint\t\tFK",
        "col\tchunks\t1\tchunk_id\tbigint\tPK\t",
        "col\tchunks\t2\tref_id\tbigint\t\tFK",
        "col\tchunks\t3\tcreated_at\ttimestamp with time zone\t\t",
        "fk\tchunks\t0\tref_id\trefs\tref_id\t",
    ]
)


def test_parse_tsv_splits_col_and_fk_rows() -> None:
    cols, fks = parse_tsv(_TSV)
    assert len(cols) == 6
    assert len(fks) == 1
    assert fks[0][1] == "chunks" and fks[0][4] == "refs"


def test_parse_tsv_skips_blank_and_short_lines() -> None:
    cols, fks = parse_tsv("\n\ncol\tx\t1\ta\ttext\t\t\ngarbage\n")
    assert len(cols) == 1
    assert fks == []


def test_safe_type_collapses_spaces() -> None:
    # Mermaid attribute types must be one token.
    assert _safe_type("timestamp with time zone") == "timestamp_with_time_zone"
    assert _safe_type("double precision") == "double_precision"
    assert _safe_type("USER-DEFINED") == "USER_DEFINED"
    assert _safe_type("bigint") == "bigint"


def test_render_mermaid_shape() -> None:
    out = render_mermaid(*parse_tsv(_TSV), snapshot="testdb @ 2026-06-19")

    # Banner + provenance.
    assert "DO NOT EDIT" in out
    assert "testdb @ 2026-06-19" in out
    assert "2 base tables · 1 foreign keys" in out

    # A fenced mermaid erDiagram with both tables.
    assert "```mermaid" in out
    assert "erDiagram" in out
    assert "    refs {" in out
    assert "    chunks {" in out

    # PK / FK markers + a space-sanitised type.
    assert "bigint ref_id PK" in out
    assert "bigint parent_id FK" in out
    assert "timestamp_with_time_zone created_at" in out

    # The relationship edge points parent -> child with the fk column.
    assert 'refs ||--o{ chunks : "ref_id"' in out

    # The compact table listing is present.
    assert "## Tables" in out
    assert "| `refs` | 3 |" in out


def test_render_mermaid_names_job_and_alert_queues() -> None:
    # gr506051: ops agents grep the schema doc for the queues — the
    # banner must name them and point at ``refs.kind``.
    out = render_mermaid(*parse_tsv(_TSV))
    assert "jobs" in out and "alerts" in out
    assert "kind='job'" in out and "kind='alert'" in out
    assert "refs.kind" in out


def test_render_mermaid_no_columns_is_empty_table_section() -> None:
    # Defensive: empty input still renders a well-formed (if empty) doc.
    out = render_mermaid([], [], snapshot="empty")
    assert "erDiagram" in out
    assert "0 base tables · 0 foreign keys" in out


def test_self_reference_edge_is_marked() -> None:
    tsv = "\n".join(
        [
            "col\ttodos\t1\tid\tbigint\tPK\t",
            "col\ttodos\t2\tparent\tbigint\t\tFK",
            "fk\ttodos\t0\tparent\ttodos\tid\t",
        ]
    )
    out = render_mermaid(*parse_tsv(tsv))
    assert 'todos ||--o{ todos : "parent (self)"' in out


def test_render_emits_searchable_column_listing() -> None:
    """gr513884: a per-table column table (name, type, key, comment) under
    the diagram — the greppable form the mermaid block cannot be."""
    tsv = "\n".join(
        [
            "col\tlinks\t1\tlink_id\tbigint\tPK\t",
            "col\tlinks\t2\ttrust_set\tinteger\t\t\ttrust origin (migration 0214)",
            "col\tlinks\t3\tnote\ttext\t\t\tfree | text",
        ]
    )
    out = render_mermaid(*parse_tsv(tsv))
    assert "## Columns" in out
    assert "### `links`" in out
    assert "| `link_id` | bigint | PK |" in out
    assert "trust origin (migration 0214)" in out
    # A pipe inside a comment is escaped so the table stays well-formed.
    assert "free \\| text" in out


def test_column_comment_field_is_optional_in_tsv() -> None:
    """Older TSV streams (no 8th field) still parse and render."""
    cols, _ = parse_tsv("col\tt\t1\ta\ttext\t\t\n")
    assert cols[0][:7] == ["col", "t", "1", "a", "text", "", ""]
    out = render_mermaid(cols, [])
    assert "| `a` | text | — |" in out


def test_introspect_sql_plans_on_the_test_db(store: Store) -> None:
    """The introspection query plans on the migrated test DB — a UNION
    column-count, join or column-name typo fails at plan time here, not
    at the prod ``scripts/gen-schema`` run (the miss gr513884's own run
    hit). EXPLAIN, not a run: executing it took over 600 s on CI runners
    (the constraint views go through _pg_expandarray; r100). The parse +
    render path is pinned by the fixture tests above."""
    import psycopg

    assert store.dsn is not None
    with psycopg.connect(store.dsn) as conn:
        plan = conn.execute("EXPLAIN " + INTROSPECT_SQL).fetchall()
    assert plan
