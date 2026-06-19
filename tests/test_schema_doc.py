"""Tests for ``precis schema-doc`` rendering (the pure, no-DB half).

The psycopg path (``_fetch_via_dsn``) is exercised by the live
``scripts/gen-schema`` regen; here we pin the parse + Mermaid render
against fixture rows so the artifact shape can't regress.
"""

from __future__ import annotations

from precis.cli.schema_doc import _safe_type, parse_tsv, render_mermaid

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
