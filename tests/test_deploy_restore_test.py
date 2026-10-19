"""``restore_test.sh.j2``: clean up on ANY exit, never drop the live DB.

gripe 496659: the script ran ``set -euo pipefail`` with no trap, so the
~03:00Z nightly reboot killed ``nice -n 19 pg_restore -j1`` mid-HNSW-build
(>21 h on ~50 GB ``chunk_embeddings``) and the abort skipped the final
``dropdb`` — ~43 GB of ``precis_prod_restore_test`` stranded every week since
2026-08-17.

The fix, pinned here:

* an ``EXIT`` trap drops the scratch DB and scratch dir on every exit;
* every ``dropdb`` goes through ``restore_test_drop``, which refuses (nonzero,
  drops nothing) the live DB name and any name outside the scratch prefix —
  no glob or stray ``TEST_DB`` expansion can reach the live DB;
* the restore runs ``-j4`` un-niced and filters the vector HNSW indexes out
  of the pg_restore TOC — the test validates the dump, not the ANN index.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import jinja2
import pytest

pytestmark = pytest.mark.skipif(
    sys.platform == "win32", reason="the shipped bash scripts are POSIX-only"
)

_TEMPLATE = (
    Path(__file__).resolve().parent.parent
    / "deploy"
    / "roles"
    / "backups"
    / "templates"
    / "restore_test.sh.j2"
)

_VARS: dict[str, object] = {
    "ansible_managed": "managed",
    "shared_mount": "/mnt/cluster",
    "host_local_root": "/var/local",
    "nas_root": "/nas",
    "os_family": "linux",
    "pg_user": "postgres",
}


def _render(**extra: object) -> str:
    env = jinja2.Environment(undefined=jinja2.ChainableUndefined)
    env.filters["bool"] = bool
    return env.from_string(_TEMPLATE.read_text(encoding="utf-8")).render(
        **{**_VARS, **extra}
    )


def _fn(rendered: str, name: str) -> str:
    """One ``name() { ... }`` definition, verbatim, from the rendered script."""
    _, sep, rest = rendered.partition(f"{name}() {{")
    assert sep, f"{name}() not found in rendered script"
    body = rest.split("\n}", 1)[0]
    return f"{name}() {{{body}\n}}"


def _bash(script: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", "-c", script],
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=30,
    )


# ── render-level pins ──────────────────────────────────────────────────────


def test_exit_trap_and_guard_render() -> None:
    out = _render()
    assert "trap on_exit EXIT" in out
    assert "restore_test_drop() {" in out
    # The guard checks the live name AND the scratch prefix, in that order.
    assert 'if [ "${db}" = "${LIVE_DB}" ]' in out
    assert '"${TEST_DB_PREFIX}"*) ;;' in out
    # on_exit drops through the guard; a failed drop warns (naming the
    # scratch DB) rather than hiding behind a silent || true.
    assert 'if ! restore_test_drop "${TEST_DB}"' in out
    assert "WARNING: cleanup could not drop scratch DB" in out


def test_every_dropdb_goes_through_the_guard() -> None:
    """Exactly one ``dropdb`` invocation exists — inside the guard. The stale-
    DB drop and the EXIT-trap drop both call ``restore_test_drop``, so editing
    in a second bare dropdb (the 43 GB-leak shape) reddens here."""
    out = _render()
    for flag in (True, False):
        body = _render(pg_backup_local_keep_verified_only=flag)
        assert body.count("dropdb -U") == 1
        assert 'dropdb -U "${PG_USER}" --if-exists "${db}"' in body
    assert out.count("restore_test_drop") >= 3  # def + stale-drop + trap call


def test_restore_is_parallel_unniced_and_hnsw_free() -> None:
    out = _render()
    assert "nice " not in out
    assert "-j4" in out and "-L " in out
    # The TOC filter drops HNSW index entries only, and logs each skip.
    assert "pg_restore -l" in out
    assert "INDEX [^ ]+ [^ ]*hnsw" in out
    assert "toc.skip" in out
    assert "Restore test skips these HNSW index entries:" in out


# ── the guard, run for real in bash with dropdb stubbed ────────────────────

_GUARD_HARNESS = """
set -u
DROP_LOG="{log}"
dropdb() {{ printf '%s\\n' "$*" >> "${{DROP_LOG}}"; }}
PG_USER="pg"
TEST_DB_PREFIX="precis_prod_restore_test"
TEST_DB="${{TEST_DB_PREFIX}}"
LIVE_DB="precis_prod"
{fn}
"""


def _guard(tmp_path: Path, call: str) -> tuple[subprocess.CompletedProcess[str], str]:
    log = tmp_path / "drop.log"
    fn = _fn(_render(), "restore_test_drop")
    script = _GUARD_HARNESS.format(log=log, fn=fn) + f"\n{call}\n"
    r = _bash(script)
    dropped = log.read_text(encoding="utf-8") if log.exists() else ""
    return r, dropped


def test_guard_refuses_the_live_db_name(tmp_path: Path) -> None:
    r, dropped = _guard(tmp_path, 'restore_test_drop "precis_prod"')
    assert r.returncode != 0 and dropped == ""
    assert "REFUSING" in r.stderr


def test_guard_refuses_live_name_via_test_db_variable(tmp_path: Path) -> None:
    """No expansion path: even if TEST_DB itself were corrupted to the live
    name, the trap's call still refuses."""
    r, dropped = _guard(tmp_path, 'TEST_DB="precis_prod"; restore_test_drop')
    assert r.returncode != 0 and dropped == ""


def test_guard_refuses_off_prefix_names(tmp_path: Path) -> None:
    for name in ("postgres", "precis_prod_restore", "", "precis_prod_evil"):
        call = f'restore_test_drop "{name}"' if name else "TEST_DB= restore_test_drop"
        r, dropped = _guard(tmp_path, call)
        assert r.returncode != 0, f"{name!r} unexpectedly accepted"
        assert dropped == "", f"{name!r} reached dropdb: {dropped}"


def test_guard_drops_the_scratch_name(tmp_path: Path) -> None:
    r, dropped = _guard(tmp_path, 'restore_test_drop "precis_prod_restore_test"')
    assert r.returncode == 0, r.stderr
    assert dropped == "-U pg --if-exists precis_prod_restore_test\n"


def test_on_exit_drops_scratch_db_and_removes_scratch_dir(tmp_path: Path) -> None:
    """The trap composition: scratch dir under BACKUP_DIR is removed, the
    scratch DB is dropped through the guard."""
    rendered = _render()
    scratch = tmp_path / "backups" / "postgres" / "restore_test_abc123"
    scratch.mkdir(parents=True)
    log = tmp_path / "drop.log"
    script = (
        _GUARD_HARNESS.format(
            log=log,
            fn=_fn(rendered, "restore_test_drop") + "\n" + _fn(rendered, "on_exit"),
        )
        + f'\nBACKUP_DIR="{scratch.parent}"'
        + f'\nSCRATCH_DIR="{scratch}"'
        + "\non_exit\n"
    )
    r = _bash(script)
    assert r.returncode == 0, r.stderr
    assert not scratch.exists()
    assert "precis_prod_restore_test" in log.read_text(encoding="utf-8")


def test_on_exit_warns_but_keeps_status_when_drop_fails(tmp_path: Path) -> None:
    """dropdb failing in the trap (e.g. active connections at SIGTERM) must
    surface a one-line warning naming the scratch DB — and NOT change the
    script's exit status."""
    rendered = _render()
    scratch = tmp_path / "backups" / "postgres" / "restore_test_abc123"
    scratch.mkdir(parents=True)
    script = (
        "set -u\n"
        "dropdb() { return 1; }\n"
        'PG_USER="pg"\n'
        'TEST_DB_PREFIX="precis_prod_restore_test"\n'
        'TEST_DB="${TEST_DB_PREFIX}"\n'
        'LIVE_DB="precis_prod"\n'
        f'BACKUP_DIR="{scratch.parent}"\n'
        f'SCRATCH_DIR="{scratch}"\n'
        + _fn(rendered, "restore_test_drop")
        + "\n"
        + _fn(rendered, "on_exit")
        + "\non_exit\n"
    )
    r = _bash(script)
    assert r.returncode == 0
    assert "WARNING" in r.stderr and "precis_prod_restore_test" in r.stderr
    assert not scratch.exists()


_TOC_SAMPLE = (
    "215; 1259 16396 TABLE public chunks postgres\n"
    "300; 0 0 INDEX public chunks_keywords_gin postgres\n"
    "3446; 0 0 INDEX public chunk_embeddings_vec_hnsw_idx postgres\n"
    "3450; 0 0 INDEX public tag_embeddings_vector_hnsw postgres\n"
    "400; 0 0 CONSTRAINT public chunks_pkey postgres\n"
)


def test_toc_filter_drops_hnsw_keeps_rest(tmp_path: Path) -> None:
    """Drive the real grep pipeline on a synthetic pg_restore -l listing."""
    out = _render()
    grep_line = next(ln for ln in out.splitlines() if "grep -vE" in ln)
    # The rendered line is `| grep -vE '<pattern>' \`; pull out the pattern.
    pattern = grep_line.split("grep -vE ", 1)[1].strip().split("'")[1]
    toc = tmp_path / "toc"
    toc.write_text(_TOC_SAMPLE, encoding="utf-8")
    r = _bash(f"grep -vE '{pattern}' '{toc}'")
    assert r.returncode == 0, r.stderr
    lines = r.stdout.splitlines()
    assert len(lines) == 3
    assert all("hnsw" not in ln for ln in lines)
    assert any("chunks_keywords_gin" in ln for ln in lines)
    assert any("CONSTRAINT" in ln for ln in lines)


def test_skip_block_logs_each_excluded_index_by_name(tmp_path: Path) -> None:
    """Drive the rendered skip block verbatim, with pg_restore stubbed to
    emit a synthetic TOC: every hnsw entry is echoed to the log and removed
    from toc.list, the rest is kept."""
    out = _render()
    start = out.index('pg_restore -l "${LATEST_DUMP}" > "${SCRATCH_DIR}/toc.full"')
    end = out.index('> "${SCRATCH_DIR}/toc.list"') + len('> "${SCRATCH_DIR}/toc.list"')
    block = out[start:end]
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    toc = tmp_path / "toc"
    toc.write_text(_TOC_SAMPLE, encoding="utf-8")
    script = f"""
set -euo pipefail
SCRATCH_DIR="{scratch}"
pg_restore() {{ cat "{toc}"; }}
LATEST_DUMP="stub"
{block}
"""
    r = _bash(script)
    assert r.returncode == 0, r.stderr
    assert "Restore test skips these HNSW index entries:" in r.stdout
    assert "chunk_embeddings_vec_hnsw_idx" in r.stdout
    assert "tag_embeddings_vector_hnsw" in r.stdout
    kept = (scratch / "toc.list").read_text(encoding="utf-8")
    assert "hnsw" not in kept and "chunks_keywords_gin" in kept


def test_skip_block_with_no_hnsw_indexes_is_quiet(tmp_path: Path) -> None:
    """No hnsw entries → nothing logged, toc.list equals the full TOC."""
    out = _render()
    start = out.index('pg_restore -l "${LATEST_DUMP}" > "${SCRATCH_DIR}/toc.full"')
    end = out.index('> "${SCRATCH_DIR}/toc.list"') + len('> "${SCRATCH_DIR}/toc.list"')
    block = out[start:end]
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    toc = tmp_path / "toc"
    toc.write_text(
        "215; 1259 16396 TABLE public chunks postgres\n"
        "300; 0 0 INDEX public chunks_keywords_gin postgres\n",
        encoding="utf-8",
    )
    script = f"""
set -euo pipefail
SCRATCH_DIR="{scratch}"
pg_restore() {{ cat "{toc}"; }}
LATEST_DUMP="stub"
{block}
"""
    r = _bash(script)
    assert r.returncode == 0, r.stderr
    assert "skips" not in r.stdout
    kept = (scratch / "toc.list").read_text(encoding="utf-8")
    assert "chunks_keywords_gin" in kept
