"""scripts/prod-psql's discovery affordances (gr514333): ``--help`` prints the
usage + schema cheat sheet, and a `column/relation … does not exist` on
psql's stderr is answered with the schema.md pointer and the ``\\d`` recipe
(interactive shell — scripted SQL never reaches the meta lexer, gr521223).

``ssh`` is faked on PATH: it drains the caller's SQL from stdin — the text
the remote shell reads into ``-c "$(cat)"`` — and emits ``FAKE_PSQL_ERR``
on stderr with ``FAKE_PSQL_RC``. No network, no prod coordinates.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="POSIX-only: bash")

_SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "prod-psql"

_FAKE_SSH = """\
#!/bin/sh
cat >"${FAKE_LOG:-/dev/null}"
if [ -n "${FAKE_PSQL_ERR:-}" ]; then
    printf '%s\\n' "$FAKE_PSQL_ERR" >&2
    exit "${FAKE_PSQL_RC:-3}"
fi
exit 0
"""


def _run(tmp_path: Path, *args: str, **env: str) -> subprocess.CompletedProcess[str]:
    fake = tmp_path / "bin"
    fake.mkdir(exist_ok=True)
    ssh = fake / "ssh"
    if not ssh.exists():
        ssh.write_text(_FAKE_SSH, encoding="utf-8")
        ssh.chmod(0o755)
    return subprocess.run(
        ["bash", str(_SCRIPT), *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        stdin=subprocess.DEVNULL,
        env={
            "PATH": f"{fake}:{os.environ['PATH']}",
            "HOME": str(tmp_path),
            "PGB_HOST": "fake-host",
            **env,
        },
        check=False,
    )


def test_help_prints_usage_and_the_schema_cheat_sheet(tmp_path: Path) -> None:
    for flag in ("--help", "-h"):
        r = _run(tmp_path, flag)
        assert r.returncode == 0
        assert "docs/reference/schema.md" in r.stdout
        assert "ref_id" in r.stdout and "\\d <table>" in r.stdout
        assert "chunk_embeddings" in r.stdout


_SCHEMA_MD = Path(__file__).resolve().parents[1] / "docs" / "reference" / "schema.md"


def test_help_cheat_sheet_names_only_tables_in_the_generated_schema(
    tmp_path: Path,
) -> None:
    """gr514901: the "Names that bite" block is hand-maintained while
    docs/reference/schema.md is regenerated from prod. The sync pin is that
    every table the cheat sheet names is still a table in the generated doc —
    a regen that drops or renames one fails here."""
    r = _run(tmp_path, "--help")
    assert r.returncode == 0
    cheat = r.stdout.split("Names that bite:", 1)[-1]
    named = re.findall(r"^  ([a-z_][a-z0-9_]*)\s", cheat, re.M)
    assert named, "the --help cheat sheet block was not found"
    schema_tables = set(
        re.findall(
            r"^    ([a-z_][a-z0-9_]*) \{$",
            _SCHEMA_MD.read_text(encoding="utf-8"),
            re.M,
        )
    )
    assert schema_tables
    assert set(named) <= schema_tables, set(named) - schema_tables


def test_unknown_column_gets_the_schema_pointer(tmp_path: Path) -> None:
    r = _run(
        tmp_path,
        "--ro",
        "SELECT id FROM refs",
        FAKE_PSQL_ERR='ERROR:  column "id" does not exist\nLINE 1: SELECT id FROM refs',
    )
    assert r.returncode == 3
    # The server's own error is relayed first, then the pointer.
    assert 'column "id" does not exist' in r.stderr
    assert "prod-psql: hint" in r.stderr
    assert "docs/reference/schema.md" in r.stderr and "\\d <table>" in r.stderr


def test_unknown_relation_gets_the_schema_pointer(tmp_path: Path) -> None:
    r = _run(
        tmp_path,
        "--ro",
        "SELECT * FROM jobs",
        FAKE_PSQL_ERR='ERROR:  relation "jobs" does not exist',
    )
    assert r.returncode == 3
    assert "docs/reference/schema.md" in r.stderr


def test_newer_qualified_column_error_shape_is_matched(tmp_path: Path) -> None:
    r = _run(
        tmp_path,
        "--ro",
        "SELECT embedding FROM chunks",
        FAKE_PSQL_ERR="ERROR:  column chunks.embedding does not exist",
    )
    assert "prod-psql: hint" in r.stderr


# ── gr513856: the specific-name map — "did you mean ref_id" ──────────────


def test_refs_id_column_hints_ref_id(tmp_path: Path) -> None:
    """The gripe's own case: `WHERE id=497185` on refs gets no hint from the
    server; prod-psql names the real PK."""
    r = _run(
        tmp_path,
        "--ro",
        "SELECT * FROM refs WHERE id=497185",
        FAKE_PSQL_ERR='ERROR:  column "id" does not exist',
    )
    assert r.returncode == 3
    assert "`refs` has no `id` column" in r.stderr and "`ref_id`" in r.stderr
    assert "docs/reference/schema.md" in r.stderr


def test_per_table_id_hints(tmp_path: Path) -> None:
    for table, pk in (
        ("chunks", "chunk_id"),
        ("links", "link_id"),
        ("tags", "tag_id"),
    ):
        r = _run(
            tmp_path,
            "--ro",
            f"SELECT * FROM {table} WHERE id=1",
            FAKE_PSQL_ERR='ERROR:  column "id" does not exist',
        )
        assert pk in r.stderr, (table, r.stderr)
        assert "docs/reference/schema.md" in r.stderr, table


def test_links_id_hint_names_the_endpoints(tmp_path: Path) -> None:
    r = _run(
        tmp_path,
        "--ro",
        "SELECT * FROM links WHERE id=1",
        FAKE_PSQL_ERR='ERROR:  column "id" does not exist',
    )
    assert "src_ref_id" in r.stderr and "dst_ref_id" in r.stderr


def test_chunks_embedding_hints_chunk_embeddings(tmp_path: Path) -> None:
    r = _run(
        tmp_path,
        "--ro",
        "SELECT embedding FROM chunks",
        FAKE_PSQL_ERR="ERROR:  column chunks.embedding does not exist",
    )
    assert "chunk_embeddings" in r.stderr


def test_jobs_relation_hints_the_refs_kind(tmp_path: Path) -> None:
    for table, kind in (("jobs", "job"), ("alerts", "alert")):
        r = _run(
            tmp_path,
            "--ro",
            f"SELECT * FROM {table}",
            FAKE_PSQL_ERR=f'ERROR:  relation "{table}" does not exist',
        )
        assert f"kind = '{kind}'" in r.stderr, table
        assert f"no `{table}` table" in r.stderr


def test_id_on_an_unmapped_table_gets_the_generic_pk_hint(tmp_path: Path) -> None:
    r = _run(
        tmp_path,
        "--ro",
        "SELECT id FROM ref_events",
        FAKE_PSQL_ERR='ERROR:  column "id" does not exist',
    )
    assert "ref_id/chunk_id/link_id/tag_id/event_id" in r.stderr


def test_other_errors_get_no_schema_hint(tmp_path: Path) -> None:
    r = _run(
        tmp_path,
        "--ro",
        "SELECT 1/0",
        FAKE_PSQL_ERR="ERROR:  division by zero",
    )
    assert r.returncode == 3
    assert "division by zero" in r.stderr
    assert "prod-psql: hint" not in r.stderr


def test_clean_run_prints_no_hint(tmp_path: Path) -> None:
    r = _run(tmp_path, "--ro", "SELECT 1")
    assert r.returncode == 0 and r.stderr == ""


# ── gr513882: --explain — a read-only EXPLAIN agent_ro can actually run ────


def _sent(tmp_path: Path) -> str:
    log = tmp_path / "sent.sql"
    return log.read_text(encoding="utf-8") if log.exists() else ""


def test_explain_strips_for_update_and_wraps_in_explain(tmp_path: Path) -> None:
    """The claim-query shape: FOR UPDATE … inside a subselect, SKIP LOCKED
    tail — both dropped before EXPLAIN, inside BEGIN READ ONLY."""
    log = tmp_path / "sent.sql"
    r = _run(
        tmp_path,
        "--explain",
        "SELECT * FROM refs WHERE ref_id = "
        "(SELECT ref_id FROM refs WHERE kind='job' FOR UPDATE SKIP LOCKED)",
        FAKE_LOG=str(log),
    )
    assert r.returncode == 0, r.stderr
    sent = _sent(tmp_path)
    # The caller's -c text is the rewritten query; BEGIN READ ONLY/COMMIT are
    # the fixed wrapper -c args around it.
    assert sent.startswith("EXPLAIN SELECT")
    assert "FOR UPDATE" not in sent and "SKIP LOCKED" not in sent


def test_explain_drops_a_caller_supplied_explain(tmp_path: Path) -> None:
    r = _run(
        tmp_path,
        "--explain",
        "EXPLAIN (ANALYZE, BUFFERS) SELECT 1 FOR UPDATE",
        FAKE_LOG=str(tmp_path / "sent.sql"),
    )
    assert r.returncode == 0, r.stderr
    sent = _sent(tmp_path)
    assert "EXPLAIN (ANALYZE, BUFFERS) SELECT 1" in sent
    assert "FOR UPDATE" not in sent


def test_explain_refuses_a_write_with_the_recipe(tmp_path: Path) -> None:
    log = tmp_path / "sent.sql"
    r = _run(
        tmp_path,
        "--explain",
        "INSERT INTO refs_archive SELECT * FROM refs",
        FAKE_LOG=str(log),
    )
    assert r.returncode == 2
    assert "cannot EXPLAIN a data-modifying statement" in r.stderr
    assert "driving SELECT" in r.stderr
    assert _sent(tmp_path) == ""


def test_explain_refuses_a_data_modifying_cte(tmp_path: Path) -> None:
    r = _run(
        tmp_path,
        "--explain",
        "WITH c AS (UPDATE refs SET title='x' RETURNING ref_id) "
        "INSERT INTO t SELECT * FROM c",
        FAKE_LOG=str(tmp_path / "sent.sql"),
    )
    assert r.returncode == 2 and _sent(tmp_path) == ""


def test_explain_implies_ro_in_either_flag_order(tmp_path: Path) -> None:
    r = _run(
        tmp_path,
        "--ro",
        "--explain",
        "SELECT 1 FOR UPDATE NOWAIT",
        FAKE_LOG=str(tmp_path / "sent.sql"),
    )
    assert r.returncode == 0, r.stderr
    sent = _sent(tmp_path)
    assert sent.startswith("EXPLAIN SELECT 1")
    assert "FOR UPDATE" not in sent


def test_explain_without_a_lock_clause_is_just_explained(tmp_path: Path) -> None:
    r = _run(
        tmp_path,
        "--explain",
        "SELECT count(*) FROM refs WHERE kind='job'",
        FAKE_LOG=str(tmp_path / "sent.sql"),
    )
    assert r.returncode == 0, r.stderr
    assert "EXPLAIN SELECT count(*)" in _sent(tmp_path)
