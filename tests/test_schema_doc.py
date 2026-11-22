"""Tests for ``precis schema-doc`` rendering (the pure, no-DB half).

The psycopg path (``_fetch_via_dsn``) is exercised by the live
``scripts/gen-schema`` regen; here we pin the parse + Mermaid render
against fixture rows so the artifact shape can't regress.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

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


# A migrations-directory listing: numbered files (one number used twice,
# as in the real tree) plus the entries that are not migrations.
_MIGRATIONS = [
    "0001_initial.sql",
    "0037_draft_list_chunks.sql",
    "0037_plots_relation.sql",
    "0323_consideration_verdicts.sql",
    "0324_scope_sha_excludes_logbook.sql",
    "0325_links_passive_spellings_flip.sql",
    "archive",
    "baseline",
    "README.md",
]

# docs/reference/schema.md's Source line as committed before the marker
# existed (gr676943).
_UNMARKED = "> Source: precis_prod @ 2026-10-24 · 149 base tables · 203 foreign keys."


def test_snapshot_label_is_rendered_in_header() -> None:
    """gr676943: the header names the newest migration the doc reflects,
    and that marker reads back out of the rendered header."""
    from precis.cli.schema_doc import schema_marker, snapshot_label

    label = snapshot_label("testdb", "2026-11-22", _MIGRATIONS)
    assert label == (
        "testdb @ 2026-11-22 · migrations through 0325_links_passive_spellings_flip"
    )
    out = render_mermaid(*parse_tsv(_TSV), snapshot=label)
    assert f"> Source: {label} · 2 base tables · 1 foreign keys." in out.splitlines()
    assert schema_marker(out) == "0325_links_passive_spellings_flip"
    # No marker in the header reads as unknown, not as an error.
    assert schema_marker(_UNMARKED) is None
    assert schema_marker(render_mermaid(*parse_tsv(_TSV))) is None


def test_schema_lag_counts_newer_migrations() -> None:
    """gr676943: the lag is the number of migration files newer than the
    header's marker; a header with no marker lags by all of them."""
    from precis.cli.schema_doc import schema_lag

    marked = _UNMARKED.replace(
        " · 149", " · migrations through 0323_consideration_verdicts · 149"
    )
    assert schema_lag(marked, _MIGRATIONS) == 2
    # Paths count the same as bare names, in any order.
    paths = [f"src/precis/migrations/{name}" for name in reversed(_MIGRATIONS)]
    assert schema_lag(marked, paths) == 2
    current = marked.replace(
        "0323_consideration_verdicts", "0325_links_passive_spellings_flip"
    )
    assert schema_lag(current, _MIGRATIONS) == 0
    # Unknown, lagging: every numbered migration counts, the rest do not.
    assert schema_lag(_UNMARKED, _MIGRATIONS) == 6
    # Only the Source line carries the marker: the same phrase in a column
    # comment further down is not one.
    doc = (
        f"{_UNMARKED}\n\n| `x` | text | — | backfilled by migrations through "
        "0324_scope_sha_excludes_logbook |"
    )
    assert schema_lag(doc, _MIGRATIONS) == 6


_GEN_SCHEMA = Path(__file__).resolve().parents[1] / "scripts" / "gen-schema"

# ssh leaves a file behind when it is called, so the test can prove it was not.
_FAKE_SSH = """\
#!/bin/sh
: >"$FAKE_SSH_CALLED"
exit 97
"""

# `uv run precis <args>` on the test's own interpreter: no env sync, no
# network. Anything else the script asks uv for is refused.
_FAKE_UV = """\
#!/bin/sh
if [ "$1" != run ] || [ "$2" != precis ]; then
    echo "fake uv: unexpected: $*" >&2
    exit 64
fi
shift 2
exec "$FAKE_UV_PYTHON" -m precis "$@"
"""


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX-only: bash")
def test_gen_schema_check_reports_lag_without_ssh(tmp_path: Path) -> None:
    """gr676943: ``scripts/gen-schema --check`` reads the doc's header and
    the tree's migrations, prints the lag, exits 1 when the doc is stale or
    its age is unknown — and never opens ssh."""
    fake = tmp_path / "bin"
    fake.mkdir()
    for name, body in (("ssh", _FAKE_SSH), ("uv", _FAKE_UV)):
        (fake / name).write_text(body, encoding="utf-8")
        (fake / name).chmod(0o755)
    called = tmp_path / "ssh-called"
    doc = tmp_path / "schema.md"

    def check(source_line: str) -> subprocess.CompletedProcess[str]:
        doc.write_text(
            f"# Database schema (generated)\n\n{source_line}\n", encoding="utf-8"
        )
        return subprocess.run(
            ["bash", str(_GEN_SCHEMA), "--check", str(doc)],
            capture_output=True,
            text=True,
            encoding="utf-8",
            stdin=subprocess.DEVNULL,
            env={
                "PATH": f"{fake}:{os.environ['PATH']}",
                "HOME": str(tmp_path),
                "PRECIS_SCHEMA_SSH_HOST": "fake-host",
                "PRECIS_SCHEMA_DB_HOST": "fake-host",
                "FAKE_SSH_CALLED": str(called),
                "FAKE_UV_PYTHON": sys.executable,
            },
            timeout=120,
            check=False,
        )

    # The header as committed today: no marker, so unknown and lagging.
    unknown = check(_UNMARKED)
    assert unknown.returncode == 1, unknown.stderr
    assert "schema.md lags" in unknown.stdout
    assert "unknown" in unknown.stdout

    # A marker far behind the tree: more than the allowed lag.
    stale = check(
        _UNMARKED.replace(" · 149", " · migrations through 0001_initial · 149")
    )
    assert stale.returncode == 1, stale.stderr
    assert "schema.md lags" in stale.stdout

    # A marker no migration in the tree is newer than: nothing to flag.
    fresh = check(
        _UNMARKED.replace(" · 149", " · migrations through 9999_none_newer · 149")
    )
    assert fresh.returncode == 0, fresh.stderr
    assert "schema.md lags 0 migrations" in fresh.stdout

    assert not called.exists(), "scripts/gen-schema --check opened ssh"


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
